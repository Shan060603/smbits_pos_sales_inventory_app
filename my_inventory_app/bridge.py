import os
import json
import requests
from flask import Blueprint, render_template, request, jsonify, session
from dotenv import load_dotenv

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

    def __init__(self, url=None, api_key=None, api_secret=None):
        self.url = (url or os.getenv("ERPNEXT_URL") or "").rstrip("/")
        self.api_key = api_key or os.getenv("API_KEY") or os.getenv("ERP_API_KEY")
        self.api_secret = api_secret or os.getenv("API_SECRET") or os.getenv("ERP_API_SECRET")

        self.session = requests.Session()
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Expect": None  # disables 417 error reliably
        }
        if self.api_key and self.api_secret:
            headers["Authorization"] = f"token {self.api_key}:{self.api_secret}"
        self.session.headers.update(headers)

        # Optional: retry for transient network issues
        adapter = requests.adapters.HTTPAdapter(max_retries=3)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

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
            item_name = self._first_value(row, ["item_name", "item_code", "item"], item_code)
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
        """Override UI rates using Item Price lists when available."""
        item_codes = sorted({r.get("item_code") for r in rows if r.get("item_code")})
        if not item_codes:
            return rows

        buying_rows = self.get_resource_list(
            "Item Price",
            filters=[["item_code", "in", item_codes], ["price_list", "=", "Standard Buying"]],
            fields=["item_code", "price_list_rate"],
            start=0,
            page_length=2000
        )
        selling_rows = self.get_resource_list(
            "Item Price",
            filters=[["item_code", "in", item_codes], ["price_list", "=", "Standard Selling"]],
            fields=["item_code", "price_list_rate"],
            start=0,
            page_length=2000
        )
        buying_map = {r.get("item_code"): float(r.get("price_list_rate") or 0) for r in buying_rows}
        selling_map = {r.get("item_code"): float(r.get("price_list_rate") or 0) for r in selling_rows}

        for row in rows:
            code = row.get("item_code")
            if code in buying_map:
                row["valuation_rate"] = buying_map[code]
            if code in selling_map:
                row["selling_price"] = selling_map[code]
        return rows

    def _get_stock_from_bin(self, company=None, warehouse=None, start=0, page_length=200):
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

        # Remove pure-zero rows so UI isn't filled with noise.
        bins = [b for b in bins if (b.get("actual_qty") or 0) != 0]
        paged = bins[start:start + page_length]
        return self._format_rows_for_ui(paged)

    def get_full_stock_report(self, company=None, warehouse=None, start=0, page_length=200):
        """
        Fetch all Stock Balance data safely with pagination, avoids 417.
        """
        try:
            filters = {"company": company or ""}
            if warehouse:
                filters["warehouse"] = warehouse

            report_endpoint = f"{self.url}/api/method/frappe.desk.query_report.run"
            payload = {"report_name": "Stock Balance", "filters": filters}

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
                    page_length=page_length
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


def get_inventory_engine():
    return SMBITSInventoryBridge(
        url=session.get("erp_url"),
        api_key=session.get("erp_api_key"),
        api_secret=session.get("erp_api_secret")
    )


# ----------------------------------------------------
# ROUTES
# ----------------------------------------------------
@inventory_bp.route("/")
def inventory_home():
    return render_template("inventory_index.html")


@inventory_bp.route("/api/metadata", methods=["GET"])
def get_metadata():
    inventory_engine = get_inventory_engine()
    company = request.args.get("company")
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

    return jsonify({
        "warehouses": warehouses,
        "companies": [c["name"] for c in companies]
    })


@inventory_bp.route("/api/stock_report", methods=["GET"])
def stock_report():
    inventory_engine = get_inventory_engine()
    company = request.args.get("company")
    warehouse = request.args.get("warehouse")
    start = request.args.get("start", 0, type=int)

    data = inventory_engine.get_full_stock_report(
        company=company,
        warehouse=warehouse,
        start=start,
        page_length=20
    )

    return jsonify(data)


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
    result = inventory_engine.upsert_item_price(item_code, price_list, rate)
    if result.get("ok"):
        return jsonify({"status": "success", "message": f"{price_list} updated.", "rate": rate})
    return jsonify({"status": "error", "message": result.get("error") or "Failed to save rate."}), 400
