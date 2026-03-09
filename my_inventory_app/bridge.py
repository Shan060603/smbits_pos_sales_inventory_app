import os
import json
import requests
from datetime import date, timedelta
from urllib.parse import quote
from flask import Blueprint, render_template, request, jsonify, session
from dotenv import load_dotenv
from offline_outbox import save_snapshot, load_snapshot, enqueue_job, is_transient_error

load_dotenv()

inventory_bp = Blueprint(
    "inventory_bp",
    __name__,
    template_folder="templates",
    static_folder="static"
)


# ----------------------------------------------------
# ERP INVENTORY BRIDGE USING STOCK BALANCE REPORT
# ----------------------------------------------------
class SMBITSInventoryBridge:

    def __init__(self, url=None, sid=None, csrf_token=None):
        self.url = (url or os.getenv("ERPNEXT_URL") or "").rstrip("/")
        self.sid = sid
        self.csrf_token = csrf_token or ""

        self.session = requests.Session()
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Expect": None  # disables 417 error reliably
        }
        if self.sid:
            headers["Cookie"] = f"sid={self.sid}"
        if self.csrf_token:
            headers["X-Frappe-CSRF-Token"] = self.csrf_token
        self.session.headers.update(headers)

        # Optional: retry for transient network issues
        adapter = requests.adapters.HTTPAdapter(max_retries=3)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def is_erp_reachable(self, timeout=3):
        if not self.url:
            return False
        try:
            endpoint = f"{self.url}/api/method/frappe.auth.get_logged_user"
            res = self.session.get(endpoint, timeout=timeout)
            return res.status_code in (200, 401, 403)
        except Exception:
            return False

    # -------------------------------
    # GENERIC RESOURCE FETCH
    # -------------------------------
    def get_resource_list(self, doctype, filters=None, fields=None, start=0, page_length=200):
        """
        Fetches resource from ERPNext in paginated batches to avoid 417 errors.
        """
        endpoint = f"{self.url}/api/resource/{doctype}"
        params = {"limit_page_length": page_length, "limit_start": start}
        if fields:
            params["fields"] = json.dumps(fields)
        if filters:
            params["filters"] = json.dumps(filters)

        try:
            res = self.session.get(endpoint, params=params, timeout=20, stream=True)
            res.raise_for_status()
            return res.json().get("data", [])
        except Exception as e:
            print(f"❌ SMBITS Fetch Error [{doctype}]: {e}")
            return []

    # -------------------------------
    # FULL STOCK REPORT
    # -------------------------------
    @staticmethod
    def _extract_report_rows(response_json):
        """
        ERPNext query reports usually return:
        - {"message": {"result": [...], "columns": [...]}}
        - {"message": [...]} (legacy/custom)
        Normalize all supported variants into a list of dict rows.
        """
        message = response_json.get("message")
        parsed = False
        if isinstance(message, dict):
            parsed = True
            rows = message.get("result") or []
            columns = message.get("columns") or []
        elif isinstance(message, list):
            parsed = True
            rows = message
            columns = []
        else:
            return [], parsed

        if not rows:
            return [], parsed

        if isinstance(rows[0], dict):
            return rows, parsed

        # Some reports return list rows; map them using column names.
        if isinstance(rows[0], list) and columns:
            column_names = []
            for col in columns:
                if isinstance(col, dict):
                    column_names.append(col.get("fieldname") or col.get("label"))
                else:
                    column_names.append(str(col))

            mapped_rows = []
            for row in rows:
                if not isinstance(row, list):
                    continue
                mapped_rows.append({
                    column_names[i]: row[i] if i < len(row) else None
                    for i in range(len(column_names))
                    if column_names[i]
                })
            return mapped_rows, parsed

        return [], parsed

    @staticmethod
    def _first_value(row, keys, default=None):
        for key in keys:
            if key in row and row.get(key) not in (None, ""):
                return row.get(key)
        return default

    def _format_rows_for_ui(self, rows):
        formatted = []
        for row in rows:
            item_code = self._first_value(row, ["item_code", "item"])
            item_name = self._first_value(row, ["item_name", "item_name_en", "item_name_local"], "")
            warehouse = self._first_value(row, ["warehouse"])
            qty = self._first_value(row, ["bal_qty", "actual_qty", "qty"], 0)
            valuation_rate = self._first_value(row, ["valuation_rate", "incoming_rate", "basic_rate"], 0)

            formatted.append({
                "item_code": item_code,
                "item_name": item_name,
                "warehouse": warehouse,
                "actual_qty": qty or 0,
                "valuation_rate": valuation_rate or 0,
                "selling_price": valuation_rate or 0
            })
        return formatted

    def _apply_item_prices(self, rows):
        """Override UI rates using the most recently modified Item Price entry."""
        item_codes = sorted({r.get("item_code") for r in rows if r.get("item_code")})
        if not item_codes:
            return rows

        buying_rows = self.get_resource_list(
            "Item Price",
            filters=[["item_code", "in", item_codes], ["price_list", "=", "Standard Buying"]],
            fields=["item_code", "price_list_rate", "modified"],
            start=0,
            page_length=2000
        )
        selling_rows = self.get_resource_list(
            "Item Price",
            filters=[["item_code", "in", item_codes], ["price_list", "=", "Standard Selling"]],
            fields=["item_code", "price_list_rate", "modified"],
            start=0,
            page_length=2000
        )

        def _build_latest_map(price_rows):
            """Build item_code → rate map, keeping the most recently modified entry."""
            m = {}
            for r in price_rows:
                code = r.get("item_code")
                if not code:
                    continue
                rate = float(r.get("price_list_rate") or 0)
                mod = r.get("modified") or ""
                if code not in m or mod > m[code][1]:
                    m[code] = (rate, mod)
            return {code: v[0] for code, v in m.items()}

        buying_map = _build_latest_map(buying_rows)
        selling_map = _build_latest_map(selling_rows)

        for row in rows:
            code = row.get("item_code")
            if code in buying_map:
                row["valuation_rate"] = buying_map[code]
            if code in selling_map:
                row["selling_price"] = selling_map[code]
        return rows

    def _get_stock_from_bin(self, company=None, warehouse=None, start=0, page_length=200, include_zero_stock=False):
        """
        Fallback path when Stock Balance report returns no rows.
        Uses Bin records and company-scoped warehouses.
        """
        wh_filters = [["company", "=", company]] if company else []
        warehouses = self.get_resource_list(
            "Warehouse",
            filters=wh_filters,
            fields=["name"],
            start=0,
            page_length=2000
        )
        allowed_wh = [w.get("name") for w in warehouses if w.get("name")]
        if warehouse:
            allowed_wh = [w for w in allowed_wh if w == warehouse]
        if not allowed_wh:
            return []

        bin_filters = [["warehouse", "in", allowed_wh]]
        bins = self.get_resource_list(
            "Bin",
            filters=bin_filters,
            fields=["item_code", "warehouse", "actual_qty", "valuation_rate"],
            start=0,
            page_length=2000
        )

        if not include_zero_stock:
            bins = [b for b in bins if (b.get("actual_qty") or 0) != 0]

        # Enrich with item_name so UI can display ID + Name clearly.
        item_codes = sorted({b.get("item_code") for b in bins if b.get("item_code")})
        item_map = {}
        if item_codes:
            items = self.get_resource_list(
                "Item",
                filters=[["name", "in", item_codes]],
                fields=["name", "item_name"],
                start=0,
                page_length=2000
            )
            item_map = {it.get("name"): (it.get("item_name") or "") for it in items}
            for row in bins:
                row["item_name"] = item_map.get(row.get("item_code"), "")

        paged = bins[start:start + page_length]
        return self._format_rows_for_ui(paged)

    def get_full_stock_report(self, company=None, warehouse=None, start=0, page_length=200, include_zero_stock=False):
        """
        Fetch all Stock Balance data safely with pagination, avoids 417.
        """
        try:
            filters = {}
            if company:
                filters["company"] = company
            if warehouse:
                filters["warehouse"] = warehouse

            report_endpoint = f"{self.url}/api/method/frappe.desk.query_report.run"
            payload = {
                "report_name": "Stock Balance",
                "filters": filters
            }
            # Handle ERPNext variants for including zero-stock rows.
            if include_zero_stock:
                payload["filters"]["include_zero_stock"] = 1
                payload["filters"]["show_zero_stock_items"] = 1

            res = self.session.post(report_endpoint, json=payload, timeout=30)
            res.raise_for_status()

            data, parsed = self._extract_report_rows(res.json())
            if not parsed:
                print(f"❌ SMBITS Full Stock Fetch Error: Unexpected response format")
                return []

            if not data:
                fallback_data = self._get_stock_from_bin(
                    company=company,
                    warehouse=warehouse,
                    start=start,
                    page_length=page_length,
                    include_zero_stock=include_zero_stock
                )
                print(f"ℹ️ SMBITS Stock Balance rows=0, Bin fallback rows={len(fallback_data)}")
                return self._apply_item_prices(fallback_data)

            # Slice for front-end pagination and normalize keys.
            paged_data = data[start:start + page_length]
            return self._apply_item_prices(self._format_rows_for_ui(paged_data))

        except Exception as e:
            print(f"❌ SMBITS Full Stock Fetch Error: {e}")
            return []

    # -------------------------------
    # CREATE STOCK ENTRY
    # -------------------------------
    def create_stock_entry(self, item_code, warehouse, qty, purpose="Material Receipt"):
        """
        Creates a Stock Entry in ERPNext safely.
        """
        endpoint = f"{self.url}/api/resource/Stock Entry"
        payload = {
            "stock_entry_type": purpose,
            "items": [{
                "item_code": item_code,
                "qty": float(qty),
                "t_warehouse": warehouse if purpose == "Material Receipt" else None,
                "s_warehouse": warehouse if purpose == "Material Issue" else None,
                "uom": "Nos"
            }]
        }
        try:
            res = self.session.post(endpoint, json=payload, timeout=25)
            res.raise_for_status()
            return res.json()
        except Exception as e:
            return {"error": str(e)}

    def upsert_item_price(self, item_code, price_list, rate):
        """Update existing Item Price or create one if missing."""
        try:
            existing = self.get_resource_list(
                "Item Price",
                filters=[["item_code", "=", item_code], ["price_list", "=", price_list]],
                fields=["name"],
                start=0,
                page_length=1
            )
            payload = {"price_list_rate": float(rate)}
            if existing:
                name = existing[0].get("name")
                endpoint = f"{self.url}/api/resource/Item Price/{name}"
                res = self.session.put(endpoint, json=payload, timeout=25)
                res.raise_for_status()
                return {"ok": True, "data": res.json().get("data")}

            create_payload = {
                "item_code": item_code,
                "price_list": price_list,
                "price_list_rate": float(rate)
            }
            endpoint = f"{self.url}/api/resource/Item Price"
            res = self.session.post(endpoint, json=create_payload, timeout=25)
            res.raise_for_status()
            return {"ok": True, "data": res.json().get("data")}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def add_item_price_history(self, item_code, price_list, rate):
        """
        Append a new Item Price row for historical tracking.
        Tries to create without valid_from first. If duplicate, tries with
        incremental dates (today, tomorrow, etc.) until it succeeds.
        """
        try:
            # First try without valid_from
            create_payload = {
                "item_code": item_code,
                "price_list": price_list,
                "price_list_rate": float(rate)
            }
            endpoint = f"{self.url}/api/resource/Item Price"
            res = self.session.post(endpoint, json=create_payload, timeout=25)
            body = res.json() if res.text else {}
            if res.ok and isinstance(body.get("data"), dict):
                return {"ok": True, "data": body.get("data"), "mode": "appended"}

            # If failed (likely duplicate), try with incremental dates
            for days_offset in range(10):  # Try up to 10 days forward
                test_date = (date.today() + timedelta(days=days_offset)).isoformat()
                create_payload["valid_from"] = test_date
                res = self.session.post(endpoint, json=create_payload, timeout=25)
                body = res.json() if res.text else {}
                if res.ok and isinstance(body.get("data"), dict):
                    return {"ok": True, "data": body.get("data"), "mode": f"appended_with_valid_from_{test_date}"}

            # All attempts failed - return the last error
            return {"ok": False, "error": self._extract_error(body) or "Failed to create Item Price entry after multiple attempts."}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def create_item(self, item_code, item_name, stock_uom, item_group=None, is_stock_item=True, sales_price=0, purchase_price=0, barcodes=None):
        """Create Item and optionally seed Standard Buying/Selling prices."""
        item_code = (item_code or "").strip()
        item_name = (item_name or item_code).strip()
        stock_uom = (stock_uom or "").strip()
        item_group = (item_group or "").strip()
        if not item_code or not stock_uom:
            return {"ok": False, "error": "Item code and UOM are required."}

        if not item_group:
            groups = self.get_resource_list("Item Group", fields=["name"], start=0, page_length=1)
            item_group = groups[0]["name"] if groups else "All Item Groups"

        payload = {
            "item_code": item_code,
            "item_name": item_name,
            "item_group": item_group,
            "stock_uom": stock_uom,
            "is_stock_item": 1 if is_stock_item else 0
        }
        cleaned_barcodes = []
        for raw in (barcodes or []):
            code = (raw or "").strip()
            if code and code not in cleaned_barcodes:
                cleaned_barcodes.append(code)
        if cleaned_barcodes:
            payload["barcodes"] = [{"barcode": b} for b in cleaned_barcodes]
        endpoint = f"{self.url}/api/resource/Item"

        try:
            res = self.session.post(endpoint, json=payload, timeout=25)
            body = res.json() if res.text else {}
            created = body.get("data")
            if not res.ok or not isinstance(created, dict):
                msg = body.get("message") if isinstance(body, dict) else None
                if isinstance(msg, dict):
                    msg = msg.get("message")
                return {"ok": False, "error": msg or body.get("error") or f"Failed to create item ({res.status_code})."}

            if float(sales_price or 0) > 0:
                self.upsert_item_price(created.get("name"), "Standard Selling", float(sales_price))
            if float(purchase_price or 0) > 0:
                self.upsert_item_price(created.get("name"), "Standard Buying", float(purchase_price))
            return {"ok": True, "data": created}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def update_item(self, item_code, item_name=None, stock_uom=None, item_group=None, disabled=None, barcodes=None, sales_price=None, purchase_price=None):
        """Update item details and barcode child rows."""
        code = (item_code or "").strip()
        if not code:
            return {"ok": False, "error": "Item code is required."}
        try:
            endpoint = f"{self.url}/api/resource/Item/{quote(code, safe='')}"

            # Step 1: update non-UOM fields directly so they are not blocked by stock_uom validation.
            basic_payload = {}
            if item_name is not None:
                basic_payload["item_name"] = (item_name or "").strip() or code
            if item_group is not None:
                basic_payload["item_group"] = (item_group or "").strip()
            if disabled is not None:
                basic_payload["disabled"] = 1 if bool(disabled) else 0
            if isinstance(barcodes, list):
                cleaned = []
                for raw in barcodes:
                    b = (raw or "").strip()
                    if b and b not in cleaned:
                        cleaned.append(b)
                basic_payload["barcodes"] = [{"doctype": "Item Barcode", "barcode": b} for b in cleaned]

            if basic_payload:
                basic_res = self.session.put(endpoint, json=basic_payload, timeout=25)
                basic_body = basic_res.json() if basic_res.text else {}
                if (not basic_res.ok) or (not isinstance(basic_body.get("data"), dict)):
                    return {"ok": False, "error": self._extract_error(basic_body)}

            # Step 2: update stock_uom with full-doc save while preserving existing UOM rows.
            stock_uom_clean = (stock_uom or "").strip() if stock_uom is not None else ""
            if stock_uom is not None and stock_uom_clean:
                get_res = self.session.get(endpoint, timeout=25)
                get_body = get_res.json() if get_res.text else {}
                doc = get_body.get("data") if isinstance(get_body, dict) else None
                if not get_res.ok or not isinstance(doc, dict):
                    return {"ok": False, "error": self._extract_error(get_body) or "Failed to load Item document."}

                current_stock_uom = (doc.get("stock_uom") or "").strip()
                if current_stock_uom.lower() != stock_uom_clean.lower():
                    uoms = [row for row in (doc.get("uoms") or []) if isinstance(row, dict)]
                    has_stock_uom = any((row.get("uom") or "").strip().lower() == stock_uom_clean.lower() for row in uoms)
                    if not has_stock_uom:
                        uoms.append({
                            "doctype": "UOM Conversion Detail",
                            "uom": stock_uom_clean,
                            "conversion_factor": 1
                        })
                    doc["uoms"] = uoms
                    doc["stock_uom"] = stock_uom_clean

                    save_endpoint = f"{self.url}/api/method/frappe.client.save"
                    save_res = self.session.post(save_endpoint, json={"doc": doc}, timeout=25)
                    save_body = save_res.json() if save_res.text else {}
                    saved_doc = save_body.get("message") if isinstance(save_body, dict) and isinstance(save_body.get("message"), dict) else None
                    if (not save_res.ok) or (not isinstance(saved_doc, dict)):
                        return {"ok": False, "error": self._extract_error(save_body)}

            # Fetch latest doc for response
            final_res = self.session.get(endpoint, timeout=25)
            final_body = final_res.json() if final_res.text else {}
            updated_doc = final_body.get("data") if isinstance(final_body, dict) else None
            if (not final_res.ok) or (not isinstance(updated_doc, dict)):
                return {"ok": False, "error": self._extract_error(final_body) or "Failed to fetch updated item."}

            if updated_doc:
                if sales_price is not None:
                    self.add_item_price_history(code, "Standard Selling", float(sales_price or 0))
                if purchase_price is not None:
                    self.add_item_price_history(code, "Standard Buying", float(purchase_price or 0))
                return {"ok": True, "data": updated_doc}
            return {"ok": False, "error": "Failed to update item."}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_item_list(self):
        """Return item master list with buying/selling rates."""
        items = self.get_resource_list(
            "Item",
            fields=["name", "item_name", "stock_uom", "item_group", "disabled"],
            start=0,
            page_length=4000
        )

        barcode_rows = []
        barcode_forbidden = False
        try:
            endpoint = f"{self.url}/api/method/frappe.client.get_list"
            params = {
                "doctype": "Item Barcode",
                "fields": json.dumps(["parent", "barcode", "parenttype"]),
                "filters": json.dumps([["parenttype", "=", "Item"]]),
                "limit_page_length": 10000
            }
            res = self.session.get(endpoint, params=params, timeout=25)
            if res.ok:
                barcode_rows = res.json().get("message", []) or []
            elif res.status_code in (401, 403):
                barcode_forbidden = True
        except Exception as e:
            print(f"❌ SMBITS Barcode Fetch Error [method]: {e}")

        if not barcode_rows and not barcode_forbidden:
            barcode_rows = self.get_resource_list(
                "Item Barcode",
                filters=[["parenttype", "=", "Item"]],
                fields=["parent", "barcode", "parenttype"],
                start=0,
                page_length=10000
            )

        barcode_map = {}
        for row in barcode_rows:
            parent = (row.get("parent") or "").strip()
            barcode = (row.get("barcode") or "").strip()
            parenttype = (row.get("parenttype") or "Item").strip()
            if parenttype and parenttype != "Item":
                continue
            if not parent or not barcode:
                continue
            barcode_map.setdefault(parent, [])
            if barcode not in barcode_map[parent]:
                barcode_map[parent].append(barcode)

        # Fallback: if child doctype API is restricted (403), read barcodes from Item docs.
        missing_codes = [it.get("name") for it in items if it.get("name") and not barcode_map.get(it.get("name"))]
        if missing_codes:
            fallback_map = self._get_item_barcodes_from_item_docs(missing_codes, max_items=500)
            for code, bars in fallback_map.items():
                barcode_map.setdefault(code, [])
                for b in bars:
                    if b not in barcode_map[code]:
                        barcode_map[code].append(b)

        rows = []
        for it in items:
            item_code = it.get("name")
            item_barcodes = barcode_map.get(item_code, [])
            rows.append({
                "item_code": item_code,
                "item_name": it.get("item_name") or "",
                "stock_uom": it.get("stock_uom") or "",
                "item_group": it.get("item_group") or "",
                "barcodes": item_barcodes,
                "barcode_display": ", ".join(item_barcodes),
                "disabled": int(it.get("disabled") or 0),
                "valuation_rate": 0,
                "selling_price": 0
            })

        rows = self._apply_item_prices(rows)
        rows.sort(key=lambda r: ((r.get("item_name") or "").lower(), (r.get("item_code") or "").lower()))
        return rows

    def _get_item_barcodes_from_item_docs(self, item_codes, max_items=500):
        """
        Read barcodes from each Item document's child table.
        This works even when direct Item Barcode doctype API is restricted.
        """
        result = {}
        for code in (item_codes or [])[:max_items]:
            item_code = (code or "").strip()
            if not item_code:
                continue
            try:
                endpoint = f"{self.url}/api/resource/Item/{quote(item_code, safe='')}"
                res = self.session.get(endpoint, timeout=20)
                if not res.ok:
                    continue
                body = res.json() if res.text else {}
                doc = body.get("data") if isinstance(body, dict) else None
                if not isinstance(doc, dict):
                    continue

                bars = []
                for row in (doc.get("barcodes") or []):
                    barcode = (row.get("barcode") or "").strip() if isinstance(row, dict) else ""
                    if barcode and barcode not in bars:
                        bars.append(barcode)

                # Some customizations store a direct barcode field on Item.
                top_barcode = (doc.get("barcode") or "").strip()
                if top_barcode and top_barcode not in bars:
                    bars.append(top_barcode)

                if bars:
                    result[item_code] = bars
            except Exception:
                continue
        return result

    @staticmethod
    def _extract_error(payload):
        if not isinstance(payload, dict):
            return str(payload)
        server_messages = payload.get("_server_messages")
        if isinstance(server_messages, str) and server_messages:
            try:
                parsed = json.loads(server_messages)
                if isinstance(parsed, list) and parsed:
                    first = parsed[0]
                    if isinstance(first, str):
                        try:
                            first_obj = json.loads(first)
                            if isinstance(first_obj, dict):
                                msg = first_obj.get("message")
                                if msg:
                                    return str(msg)
                        except Exception:
                            return first
            except Exception:
                pass
        msg = payload.get("message")
        if isinstance(msg, dict):
            return msg.get("message") or str(msg)
        if isinstance(msg, str):
            return msg
        return payload.get("error") or payload.get("exc") or "Request failed."

    def create_uom(self, uom_name):
        uom_name = (uom_name or "").strip()
        if not uom_name:
            return {"ok": False, "error": "UOM name is required."}
        try:
            endpoint = f"{self.url}/api/resource/UOM"
            payload = {"uom_name": uom_name}
            res = self.session.post(endpoint, json=payload, timeout=25)
            body = res.json() if res.text else {}
            if res.ok and isinstance(body.get("data"), dict):
                return {"ok": True, "data": body["data"]}
            return {"ok": False, "error": self._extract_error(body)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def create_item_group(self, item_group_name, parent_item_group="All Item Groups"):
        item_group_name = (item_group_name or "").strip()
        parent_item_group = (parent_item_group or "All Item Groups").strip()
        if not item_group_name:
            return {"ok": False, "error": "Item Group name is required."}
        try:
            endpoint = f"{self.url}/api/resource/Item Group"
            payload = {
                "item_group_name": item_group_name,
                "parent_item_group": parent_item_group,
                "is_group": 0
            }
            res = self.session.post(endpoint, json=payload, timeout=25)
            body = res.json() if res.text else {}
            if res.ok and isinstance(body.get("data"), dict):
                return {"ok": True, "data": body["data"]}
            return {"ok": False, "error": self._extract_error(body)}
        except Exception as e:
            return {"ok": False, "error": str(e)}


def get_inventory_engine():
    return SMBITSInventoryBridge(
        url=session.get("erp_url"),
        sid=session.get("erp_sid"),
        csrf_token=session.get("erp_csrf_token")
    )


# ----------------------------------------------------
# ROUTES
# ----------------------------------------------------
@inventory_bp.route("/")
def inventory_home():
    return render_template("inventory_index.html")


@inventory_bp.route("/items")
def inventory_items_page():
    return render_template("inventory_items.html")


@inventory_bp.route("/api/metadata", methods=["GET"])
def get_metadata():
    inventory_engine = get_inventory_engine()
    company = request.args.get("company")
    snapshot_key = f"inventory:metadata:{session.get('erp_url') or ''}:{company or ''}"

    if not inventory_engine.is_erp_reachable(timeout=2):
        snap = load_snapshot(snapshot_key)
        if snap:
            snap["offline"] = True
            snap["from_temp_db"] = True
            return jsonify(snap)
        return jsonify({
            "warehouses": [],
            "companies": [],
            "offline": True,
            "from_temp_db": False
        }), 503

    wh_filters = [["company", "=", company]] if company else []

    warehouses = inventory_engine.get_resource_list(
        "Warehouse",
        filters=wh_filters,
        fields=["name", "company"]
    )

    companies = inventory_engine.get_resource_list(
        "Company",
        fields=["name"]
    )

    payload = {
        "warehouses": warehouses,
        "companies": [c["name"] for c in companies],
        "offline": False,
        "from_temp_db": False
    }
    save_snapshot(snapshot_key, payload)
    return jsonify(payload)


@inventory_bp.route("/api/stock_report", methods=["GET"])
def stock_report():
    inventory_engine = get_inventory_engine()
    company = request.args.get("company")
    warehouse = request.args.get("warehouse")
    include_zero_stock = str(request.args.get("include_zero_stock", "")).lower() in ("1", "true", "yes", "on")
    start = request.args.get("start", 0, type=int)
    snapshot_key = (
        f"inventory:stock_report:{session.get('erp_url') or ''}:"
        f"{company or ''}:{warehouse or ''}:{int(include_zero_stock)}:{start}:20"
    )

    if not inventory_engine.is_erp_reachable(timeout=2):
        snap = load_snapshot(snapshot_key)
        if snap:
            return jsonify(snap)
        return jsonify([]), 503

    data = inventory_engine.get_full_stock_report(
        company=company,
        warehouse=warehouse,
        include_zero_stock=include_zero_stock,
        start=start,
        page_length=20
    )

    save_snapshot(snapshot_key, data)
    return jsonify(data)


@inventory_bp.route("/api/item_list", methods=["GET"])
def item_list():
    inventory_engine = get_inventory_engine()
    snapshot_key = f"inventory:item_list:{session.get('erp_url') or ''}"
    if not inventory_engine.is_erp_reachable(timeout=2):
        snap = load_snapshot(snapshot_key)
        if snap is not None:
            return jsonify(snap)
        return jsonify([])
    rows = inventory_engine.get_item_list()
    save_snapshot(snapshot_key, rows)
    return jsonify(rows)


@inventory_bp.route("/api/item_list_filters", methods=["GET"])
def item_list_filters():
    inventory_engine = get_inventory_engine()
    snapshot_key = f"inventory:item_list_filters:{session.get('erp_url') or ''}"
    if not inventory_engine.is_erp_reachable(timeout=2):
        snap = load_snapshot(snapshot_key)
        if snap:
            snap["offline"] = True
            snap["from_temp_db"] = True
            return jsonify(snap)
        return jsonify({"uoms": [], "item_groups": [], "offline": True, "from_temp_db": False}), 503

    uoms = inventory_engine.get_resource_list(
        "UOM",
        fields=["name"],
        start=0,
        page_length=2000
    )
    item_groups = inventory_engine.get_resource_list(
        "Item Group",
        fields=["name"],
        start=0,
        page_length=2000
    )
    payload = {
        "uoms": [u.get("name") for u in uoms if u.get("name")],
        "item_groups": [g.get("name") for g in item_groups if g.get("name")],
        "offline": False,
        "from_temp_db": False
    }
    save_snapshot(snapshot_key, payload)
    return jsonify(payload)


@inventory_bp.route("/api/items", methods=["POST"])
def create_item():
    inventory_engine = get_inventory_engine()
    data = request.json or {}
    barcodes = data.get("barcodes") or []
    if isinstance(barcodes, str):
        barcodes = [barcodes]
    if not isinstance(barcodes, list):
        barcodes = []
    result = inventory_engine.create_item(
        item_code=data.get("item_code"),
        item_name=data.get("item_name"),
        stock_uom=data.get("stock_uom"),
        item_group=data.get("item_group"),
        is_stock_item=bool(data.get("is_stock_item", True)),
        sales_price=float(data.get("sales_price") or 0),
        purchase_price=float(data.get("purchase_price") or 0),
        barcodes=barcodes
    )
    if result.get("ok"):
        return jsonify({"status": "success", "item": result.get("data")})

    error_msg = result.get("error") or "Failed to create item."
    if is_transient_error(error_msg):
        queue_id = enqueue_job(
            "inventory_create_item",
            payload={
                "item_code": data.get("item_code"),
                "item_name": data.get("item_name"),
                "stock_uom": data.get("stock_uom"),
                "item_group": data.get("item_group"),
                "is_stock_item": bool(data.get("is_stock_item", True)),
                "sales_price": float(data.get("sales_price") or 0),
                "purchase_price": float(data.get("purchase_price") or 0),
                "barcodes": barcodes,
            },
            context={
                "erp_url": session.get("erp_url"),
                "erp_sid": session.get("erp_sid"),
                "erp_csrf_token": session.get("erp_csrf_token"),
            },
        )
        return jsonify({
            "status": "success",
            "queued": True,
            "message": f"ERPNext unreachable. Item saved offline as queue #{queue_id}. It will auto-sync when connection returns."
        })
    return jsonify({"status": "error", "message": error_msg}), 400


@inventory_bp.route("/api/items/<path:item_code>", methods=["PUT"])
def update_item(item_code):
    inventory_engine = get_inventory_engine()
    item_code = (item_code or "").strip()
    data = request.json or {}
    barcodes = data.get("barcodes")
    if isinstance(barcodes, str):
        barcodes = [barcodes]
    if barcodes is not None and not isinstance(barcodes, list):
        barcodes = []

    result = inventory_engine.update_item(
        item_code=item_code,
        item_name=data.get("item_name"),
        stock_uom=data.get("stock_uom"),
        item_group=data.get("item_group"),
        disabled=data.get("disabled"),
        barcodes=barcodes,
        sales_price=data.get("sales_price"),
        purchase_price=data.get("purchase_price")
    )
    if result.get("ok"):
        return jsonify({"status": "success", "item": result.get("data")})
    return jsonify({"status": "error", "message": result.get("error") or "Failed to update item."}), 400


@inventory_bp.route("/api/uoms", methods=["POST"])
def create_uom():
    inventory_engine = get_inventory_engine()
    data = request.json or {}
    result = inventory_engine.create_uom(data.get("uom_name"))
    if result.get("ok"):
        return jsonify({"status": "success", "uom": result.get("data")})
    return jsonify({"status": "error", "message": result.get("error") or "Failed to create UOM."}), 400


@inventory_bp.route("/api/item_groups", methods=["POST"])
def create_item_group():
    inventory_engine = get_inventory_engine()
    data = request.json or {}
    result = inventory_engine.create_item_group(
        item_group_name=data.get("item_group_name"),
        parent_item_group=data.get("parent_item_group") or "All Item Groups"
    )
    if result.get("ok"):
        return jsonify({"status": "success", "item_group": result.get("data")})
    return jsonify({"status": "error", "message": result.get("error") or "Failed to create Item Group."}), 400


@inventory_bp.route("/api/stock_entry", methods=["POST"])
def adjust_stock():
    inventory_engine = get_inventory_engine()
    data = request.json or {}
    result = inventory_engine.create_stock_entry(
        item_code=data.get("item_code"),
        warehouse=data.get("warehouse"),
        qty=data.get("qty"),
        purpose=data.get("purpose", "Material Receipt")
    )
    return jsonify(result)


@inventory_bp.route("/api/item_rate", methods=["POST"])
def update_item_rate():
    """Save buying/selling rate directly to Item Price."""
    inventory_engine = get_inventory_engine()
    data = request.json or {}
    item_code = (data.get("item_code") or "").strip()
    price_type = (data.get("price_type") or "").strip().lower()
    rate = data.get("rate")

    if not item_code:
        return jsonify({"status": "error", "message": "Item code is required."}), 400
    if price_type not in ("buying", "selling"):
        return jsonify({"status": "error", "message": "Invalid price type."}), 400
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Rate must be numeric."}), 400

    price_list = "Standard Buying" if price_type == "buying" else "Standard Selling"
    result = inventory_engine.add_item_price_history(item_code, price_list, rate)
    if result.get("ok"):
        return jsonify({"status": "success", "message": f"{price_list} saved. Old price kept in ERPNext history.", "rate": rate})
    return jsonify({"status": "error", "message": result.get("error") or "Failed to save rate."}), 400
