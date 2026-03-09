import os
import requests
import json
import datetime
from pathlib import Path
from flask import Blueprint, render_template, request, jsonify, session
from dotenv import load_dotenv
from offline_outbox import enqueue_job, is_transient_error, save_snapshot, load_snapshot

# Load secrets from the app's own .env (works regardless of cwd)
load_dotenv(Path(__file__).parent / '.env')

# --- 1. DEFINE THE BLUEPRINT ---
# This name 'purchase_bp' must match the import in your root app.py
purchase_bp = Blueprint(
    'purchase_bp', 
    __name__, 
    template_folder='templates',
    static_folder='static'
)

class SMBITSPurchaseBridge:
    def __init__(self, url=None, sid=None, csrf_token=None):
        # rstrip ensures no double slashes in API calls
        self.url = (url or os.getenv("ERPNEXT_URL") or "").rstrip('/')
        self.sid = sid
        self.csrf_token = csrf_token or ""
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        if self.sid:
            self.headers["Cookie"] = f"sid={self.sid}"
        if self.csrf_token:
            self.headers["X-Frappe-CSRF-Token"] = self.csrf_token

    def is_erp_reachable(self, timeout=3):
        if not self.url:
            return False
        try:
            endpoint = f"{self.url}/api/method/frappe.auth.get_logged_user"
            res = requests.get(endpoint, headers=self.headers, timeout=timeout)
            return res.status_code in (200, 401, 403)
        except Exception:
            return False

    def get_resource_list(self, doctype):
        """Fetches PO-related resources (Items, Suppliers, Warehouses, Companies)."""
        endpoint = f"{self.url}/api/resource/{doctype}"
        
        field_map = {
            "Item": ["name", "item_name"],
            "Warehouse": ["name", "company"],
            "Supplier": ["name", "supplier_name"],
            "Company": ["name"]
        }
        
        fields = field_map.get(doctype, ["name"])
        params = {"fields": json.dumps(fields), "limit_page_length": 2000}
        
        try:
            response = requests.get(endpoint, headers=self.headers, params=params)
            if response.status_code == 200:
                return response.json().get("data", [])
            return []
        except Exception as e:
            print(f"❌ PO Bridge Fetch Error: {str(e)}")
            return []

    def get_purchase_price(self, item_code):
        """Fetches the 'Standard Buying' price for an item."""
        endpoint = f"{self.url}/api/resource/Item Price"
        params = {
            "fields": json.dumps(["price_list_rate"]),
            "filters": json.dumps([
                ["item_code", "=", item_code],
                ["price_list", "=", "Standard Buying"]
            ]),
            "limit_page_length": 1
        }
        try:
            response = requests.get(endpoint, headers=self.headers, params=params)
            if response.status_code == 200:
                data = response.json().get("data", [])
                return data[0]['price_list_rate'] if data else 0.0
            return 0.0
        except Exception as e:
            print(f"❌ Purchase Price Fetch Error: {str(e)}")
            return 0.0

    def find_item_by_barcode(self, barcode):
        """Resolve barcode to item code for purchasing scanner flow."""
        code = (barcode or "").strip()
        if not code:
            return {"ok": False, "error": "Barcode is required."}

        candidates = []
        for c in (code, code.upper(), code.lower()):
            c = (c or "").strip()
            if c and c not in candidates:
                candidates.append(c)

        try:
            endpoint = f"{self.url}/api/method/frappe.client.get_list"
            params = {
                "doctype": "Item Barcode",
                "fields": json.dumps(["parent", "barcode"]),
                "filters": json.dumps([
                    ["barcode", "in", candidates],
                    ["parenttype", "=", "Item"]
                ]),
                "limit_page_length": 1
            }
            response = requests.get(endpoint, headers=self.headers, params=params)
            if response.status_code == 200:
                rows = response.json().get("message", [])
                if rows:
                    item_code = rows[0].get("parent")
                    if item_code:
                        return {"ok": True, "item_code": item_code}
        except Exception:
            pass

        try:
            endpoint = f"{self.url}/api/resource/Item"
            params = {
                "fields": json.dumps(["name", "item_name"]),
                "filters": json.dumps([["barcode", "in", candidates]]),
                "limit_page_length": 1
            }
            response = requests.get(endpoint, headers=self.headers, params=params)
            if response.status_code == 200:
                rows = response.json().get("data", [])
                if rows:
                    row = rows[0]
                    return {
                        "ok": True,
                        "item_code": row.get("name"),
                        "item_name": row.get("item_name") or row.get("name")
                    }
        except Exception:
            pass

        return {"ok": False, "error": "Barcode not found."}

    def send_purchase_order(self, supplier, company, items, transaction_date=None, schedule_date=None, submit=True):
        """Creates a Purchase Order and optionally submits it."""
        endpoint = f"{self.url}/api/resource/Purchase Order"
        today = datetime.date.today().strftime('%Y-%m-%d')
        
        formatted_items = []
        for i in items:
            formatted_items.append({
                "item_code": i['item_code'],
                "qty": i['qty'],
                "rate": i['rate'],
                "warehouse": i['warehouse'],
                "schedule_date": schedule_date or today
            })

        payload = {
            "supplier": supplier,
            "company": company,
            "transaction_date": transaction_date or today,
            "schedule_date": schedule_date or today,
            "items": formatted_items
        }

        try:
            create_res = requests.post(endpoint, headers=self.headers, data=json.dumps(payload)).json()
            created_doc = create_res.get("data")
            if not submit or not isinstance(created_doc, dict):
                return create_res

            submit_endpoint = f"{self.url}/api/method/frappe.client.submit"
            submit_res = requests.post(
                submit_endpoint,
                headers=self.headers,
                json={"doc": created_doc}
            ).json()
            return submit_res if "message" in submit_res else create_res
        except Exception as e:
            return {"error": str(e)}

    def create_supplier(self, supplier_name):
        """Creates a Supplier with default group."""
        if not supplier_name or not supplier_name.strip():
            return {"error": "Supplier name is required."}

        groups = self.get_resource_list("Supplier Group")
        supplier_group = groups[0]["name"] if groups else "All Supplier Groups"

        payload = {
            "supplier_name": supplier_name.strip(),
            "supplier_group": supplier_group,
            "supplier_type": "Company"
        }
        endpoint = f"{self.url}/api/resource/Supplier"
        try:
            response = requests.post(endpoint, headers=self.headers, json=payload)
            return response.json()
        except Exception as e:
            return {"error": str(e)}

    def create_item(self, item_code, item_name, stock_uom, sales_price=0, purchase_price=0):
        """Creates an Item and optional Standard Selling/Buying prices."""
        if not item_code or not stock_uom:
            return {"error": "Item code and UOM are required."}

        item_groups = self.get_resource_list("Item Group")
        item_group = item_groups[0]["name"] if item_groups else "All Item Groups"

        item_payload = {
            "item_code": item_code.strip(),
            "item_name": (item_name or item_code).strip(),
            "stock_uom": stock_uom.strip(),
            "item_group": item_group,
            "is_stock_item": 1
        }
        item_endpoint = f"{self.url}/api/resource/Item"

        try:
            item_res = requests.post(item_endpoint, headers=self.headers, json=item_payload).json()
            if "data" not in item_res:
                return item_res

            item_code_created = item_res["data"]["name"]

            if float(sales_price or 0) > 0:
                self._create_item_price(item_code_created, "Standard Selling", sales_price)
            if float(purchase_price or 0) > 0:
                self._create_item_price(item_code_created, "Standard Buying", purchase_price)

            return item_res
        except Exception as e:
            return {"error": str(e)}

    def _create_item_price(self, item_code, price_list, rate):
        endpoint = f"{self.url}/api/resource/Item Price"
        payload = {
            "item_code": item_code,
            "price_list": price_list,
            "price_list_rate": float(rate)
        }
        try:
            requests.post(endpoint, headers=self.headers, json=payload)
        except Exception:
            pass

    def get_purchase_document_report(
        self,
        report_type="purchase_order",
        from_date=None,
        to_date=None,
        company=None,
        supplier=None,
        status="submitted",
        start=0,
        page_length=300
    ):
        """
        Fetch Purchase Order / Purchase Invoice / Purchase Receipt report rows.
        """
        report_map = {
            "purchase_order": {
                "doctype": "Purchase Order",
                "date_field": "transaction_date",
                "fields": [
                    "name", "transaction_date", "supplier", "company",
                    "grand_total", "rounded_total", "per_received",
                    "per_billed", "docstatus", "status"
                ]
            },
            "purchase_invoice": {
                "doctype": "Purchase Invoice",
                "date_field": "posting_date",
                "fields": [
                    "name", "posting_date", "supplier", "company",
                    "grand_total", "rounded_total", "outstanding_amount",
                    "paid_amount", "docstatus", "status"
                ]
            },
            "purchase_receipt": {
                "doctype": "Purchase Receipt",
                "date_field": "posting_date",
                "fields": [
                    "name", "posting_date", "supplier", "company",
                    "grand_total", "rounded_total", "per_billed",
                    "docstatus", "status"
                ]
            }
        }
        cfg = report_map.get(report_type, report_map["purchase_order"])
        endpoint = f"{self.url}/api/resource/{cfg['doctype']}"
        filters = []

        if from_date:
            filters.append([cfg["date_field"], ">=", from_date])
        if to_date:
            filters.append([cfg["date_field"], "<=", to_date])
        if company:
            filters.append(["company", "=", company])
        if supplier:
            filters.append(["supplier", "=", supplier])

        status_text = (status or "all").strip()
        status_key = status_text.lower()
        status_map = {"draft": 0, "submitted": 1, "cancelled": 2}
        if status_key in status_map:
            filters.append(["docstatus", "=", status_map[status_key]])
        elif status_key not in ("", "all"):
            filters.append(["status", "=", status_text])

        params = {
            "fields": json.dumps(cfg["fields"]),
            "filters": json.dumps(filters),
            "order_by": f"{cfg['date_field']} desc, creation desc",
            "limit_start": int(start or 0),
            "limit_page_length": int(page_length or 300)
        }

        try:
            response = requests.get(endpoint, headers=self.headers, params=params)
            response.raise_for_status()
            rows = response.json().get("data", [])

            # Normalize date key for consistent frontend rendering.
            for row in rows:
                row["document_date"] = row.get(cfg["date_field"])
            return rows
        except Exception as e:
            print(f"❌ Purchase Report Fetch Error [{cfg['doctype']}]: {str(e)}")
            return []

    @staticmethod
    def _extract_erp_error(payload):
        if not isinstance(payload, dict):
            return str(payload)
        if payload.get("exc"):
            return str(payload.get("exc"))
        if payload.get("message") and not isinstance(payload.get("message"), dict):
            return str(payload.get("message"))
        if payload.get("error"):
            return str(payload.get("error"))
        server_msgs = payload.get("_server_messages")
        if server_msgs:
            try:
                decoded = json.loads(server_msgs)
                if isinstance(decoded, list) and decoded:
                    msg_obj = json.loads(decoded[0]) if isinstance(decoded[0], str) else decoded[0]
                    if isinstance(msg_obj, dict):
                        return msg_obj.get("message") or str(msg_obj)
                    return str(msg_obj)
            except Exception:
                return str(server_msgs)
        return None

    def convert_purchase_document(self, source_doctype, source_name, target_doctype, submit=True):
        """
        Create a mapped target document from a source purchase document.
        Supported:
        - Purchase Order -> Purchase Invoice / Purchase Receipt
        - Purchase Invoice -> Purchase Receipt
        - Purchase Receipt -> Purchase Invoice
        """
        method_map = {
            ("Purchase Order", "Purchase Invoice"): [
                "erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_invoice"
            ],
            ("Purchase Order", "Purchase Receipt"): [
                "erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_receipt"
            ],
            ("Purchase Invoice", "Purchase Receipt"): [
                "erpnext.accounts.doctype.purchase_invoice.purchase_invoice.make_purchase_receipt"
            ],
            ("Purchase Receipt", "Purchase Invoice"): [
                "erpnext.stock.doctype.purchase_receipt.purchase_receipt.make_purchase_invoice"
            ]
        }

        methods = method_map.get((source_doctype, target_doctype), [])
        if not methods:
            return {"error": f"Unsupported conversion: {source_doctype} -> {target_doctype}"}

        last_error = None
        for method in methods:
            try:
                map_endpoint = f"{self.url}/api/method/{method}"
                mapped_res = requests.post(
                    map_endpoint,
                    headers=self.headers,
                    json={"source_name": source_name}
                ).json()
                mapped_doc = mapped_res.get("message")
                if not isinstance(mapped_doc, dict):
                    last_error = self._extract_erp_error(mapped_res) or mapped_res
                    continue

                # Insert mapped draft document via frappe.client.insert.
                insert_endpoint = f"{self.url}/api/method/frappe.client.insert"
                insert_res = requests.post(
                    insert_endpoint,
                    headers=self.headers,
                    json={"doc": mapped_doc}
                ).json()
                inserted_doc = insert_res.get("message")
                if isinstance(inserted_doc, dict) and inserted_doc.get("name"):
                    if not submit:
                        return insert_res

                    # Auto-submit converted doc so it does not remain Draft.
                    submit_endpoint = f"{self.url}/api/method/frappe.client.submit"
                    submit_res = requests.post(
                        submit_endpoint,
                        headers=self.headers,
                        json={"doc": inserted_doc}
                    ).json()
                    submitted_doc = submit_res.get("message")
                    if isinstance(submitted_doc, dict) and submitted_doc.get("name"):
                        return submit_res

                    last_error = self._extract_erp_error(submit_res) or submit_res
                    continue

                last_error = self._extract_erp_error(insert_res) or insert_res
            except Exception as e:
                last_error = {"error": str(e)}

        # Fallback: some ERPNext setups don't expose PI -> PR mapper API.
        if source_doctype == "Purchase Invoice" and target_doctype == "Purchase Receipt":
            custom = self._create_purchase_receipt_from_invoice(source_name, submit=submit)
            if custom and custom.get("ok"):
                return {"message": custom.get("doc")}
            if custom and custom.get("error"):
                last_error = custom.get("error")

        return last_error or {"error": "Conversion failed."}

    def _create_purchase_receipt_from_invoice(self, purchase_invoice_name, submit=True):
        """Create PR from PI by copying invoice item rows when mapper is unavailable."""
        try:
            get_endpoint = f"{self.url}/api/method/frappe.client.get"
            pi_res = requests.get(
                get_endpoint,
                headers=self.headers,
                params={"doctype": "Purchase Invoice", "name": purchase_invoice_name}
            ).json()
            pi_doc = pi_res.get("message")
            if not isinstance(pi_doc, dict):
                return {"ok": False, "error": self._extract_erp_error(pi_res) or "Unable to load Purchase Invoice."}

            pr_items = []
            for item in pi_doc.get("items", []):
                qty = float(item.get("qty") or 0)
                if qty <= 0:
                    continue
                pr_items.append({
                    "item_code": item.get("item_code"),
                    "qty": qty,
                    "rate": float(item.get("rate") or 0),
                    "warehouse": item.get("warehouse"),
                    "uom": item.get("uom"),
                    "stock_uom": item.get("stock_uom"),
                    "conversion_factor": float(item.get("conversion_factor") or 1)
                })

            if not pr_items:
                return {"ok": False, "error": "No eligible items found in Purchase Invoice."}

            pr_payload = {
                "supplier": pi_doc.get("supplier"),
                "company": pi_doc.get("company"),
                "posting_date": datetime.date.today().strftime('%Y-%m-%d'),
                "set_warehouse": pi_doc.get("set_warehouse"),
                "items": pr_items
            }
            create_endpoint = f"{self.url}/api/resource/Purchase Receipt"
            create_res = requests.post(create_endpoint, headers=self.headers, json=pr_payload).json()
            created = create_res.get("data")
            if not isinstance(created, dict):
                return {"ok": False, "error": self._extract_erp_error(create_res) or "Failed to create Purchase Receipt."}

            if submit:
                submit_endpoint = f"{self.url}/api/method/frappe.client.submit"
                submit_res = requests.post(
                    submit_endpoint,
                    headers=self.headers,
                    json={"doc": created}
                ).json()
                submitted = submit_res.get("message")
                if isinstance(submitted, dict) and submitted.get("name"):
                    return {"ok": True, "doc": submitted}
                return {"ok": False, "error": self._extract_erp_error(submit_res) or "Created PR but failed to submit."}

            return {"ok": True, "doc": created}
        except Exception as e:
            return {"ok": False, "error": str(e)}

# --- 2. DEFINE ROUTES ---
def get_purchase_engine():
    return SMBITSPurchaseBridge(
        url=session.get("erp_url"),
        sid=session.get("erp_sid"),
        csrf_token=session.get("erp_csrf_token")
    )

@purchase_bp.route('/')
def purchase_home():
    """Serves the main Purchase UI. Use 'purchase_index.html' in the templates folder."""
    return render_template('purchase_index.html')

@purchase_bp.route('/api/metadata', methods=['GET'])
def get_metadata():
    """Endpoint for the UI to populate dropdowns."""
    purchase_engine = get_purchase_engine()
    snapshot_key = f"purchase:metadata:{session.get('erp_url') or ''}"

    if not purchase_engine.is_erp_reachable(timeout=2):
        snap = load_snapshot(snapshot_key)
        if snap:
            snap["offline"] = True
            snap["from_temp_db"] = True
            return jsonify(snap)
        return jsonify({
            "suppliers": [],
            "items": [],
            "warehouses": [],
            "companies": [],
            "uoms": [],
            "offline": True,
            "from_temp_db": False
        }), 503

    payload = {
        "suppliers": purchase_engine.get_resource_list("Supplier"),
        "items": purchase_engine.get_resource_list("Item"),
        "warehouses": purchase_engine.get_resource_list("Warehouse"),
        "companies": purchase_engine.get_resource_list("Company"),
        "uoms": purchase_engine.get_resource_list("UOM"),
        "offline": False,
        "from_temp_db": False
    }
    save_snapshot(snapshot_key, {k: payload[k] for k in ("suppliers", "items", "warehouses", "companies", "uoms")})
    return jsonify(payload)

@purchase_bp.route('/api/price/<item_code>')
def get_price(item_code):
    """Fetches item buying price on-the-fly."""
    purchase_engine = get_purchase_engine()
    price = purchase_engine.get_purchase_price(item_code)
    return jsonify({"price": price})

@purchase_bp.route('/api/item_by_barcode')
def get_item_by_barcode():
    """Resolve barcode to item code for purchasing."""
    purchase_engine = get_purchase_engine()
    barcode = (request.args.get('barcode') or '').strip()
    if not barcode:
        return jsonify({"status": "error", "message": "Barcode is required."}), 400

    result = purchase_engine.find_item_by_barcode(barcode)
    if isinstance(result, dict) and result.get("ok") and result.get("item_code"):
        return jsonify({
            "status": "success",
            "item_code": result.get("item_code"),
            "item_name": result.get("item_name")
        })
    return jsonify({"status": "error", "message": result.get("error") or "Barcode not found."}), 404


@purchase_bp.route('/api/suppliers', methods=['POST'])
def create_supplier():
    """Create supplier from purchases UI."""
    purchase_engine = get_purchase_engine()
    data = request.json or {}
    supplier_name = (data.get('supplier_name') or '').strip()
    result = purchase_engine.create_supplier(supplier_name)

    if isinstance(result, dict) and "data" in result:
        return jsonify({"status": "success", "supplier": result["data"]})

    error_msg = result.get("message") if isinstance(result, dict) else str(result)
    if isinstance(error_msg, dict):
        error_msg = error_msg.get("message") or str(error_msg)
    if not error_msg and isinstance(result, dict):
        error_msg = result.get("error")
    return jsonify({"status": "error", "message": error_msg or "Failed to create supplier."}), 400


@purchase_bp.route('/api/items', methods=['POST'])
def create_item():
    """Create item from purchases UI."""
    purchase_engine = get_purchase_engine()
    data = request.json or {}
    payload = {
        "item_code": (data.get('item_code') or '').strip(),
        "item_name": (data.get('item_name') or '').strip(),
        "stock_uom": (data.get('stock_uom') or '').strip(),
        "sales_price": float(data.get('sales_price') or 0),
        "purchase_price": float(data.get('purchase_price') or 0)
    }
    result = purchase_engine.create_item(**payload)

    if isinstance(result, dict) and "data" in result:
        return jsonify({"status": "success", "item": result["data"]})

    error_msg = result.get("message") if isinstance(result, dict) else str(result)
    if isinstance(error_msg, dict):
        error_msg = error_msg.get("message") or str(error_msg)
    if not error_msg and isinstance(result, dict):
        error_msg = result.get("error")
    if is_transient_error(error_msg):
        queue_id = enqueue_job(
            "purchase_create_item",
            payload=payload,
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
    return jsonify({"status": "error", "message": error_msg or "Failed to create item."}), 400

@purchase_bp.route('/api/submit', methods=['POST'])
def submit_purchase():
    """Processes the PO submission from the frontend."""
    purchase_engine = get_purchase_engine()
    data = request.json
    result = purchase_engine.send_purchase_order(
        supplier=data.get('supplier'),
        company=data.get('company'),
        items=data.get('items'),
        transaction_date=data.get('transaction_date'),
        schedule_date=data.get('schedule_date')
    )
    
    created = result.get("message") if isinstance(result, dict) else None
    if not isinstance(created, dict):
        created = result.get("data") if isinstance(result, dict) else None
    if isinstance(created, dict) and created.get("name"):
        state = "Submitted" if int(created.get("docstatus") or 0) == 1 else "Created"
        return jsonify({"status": "success", "message": f"PO {created['name']} {state}!"})

    error_msg = None
    if isinstance(result, dict):
        error_msg = result.get("error") or result.get("message")
        if isinstance(error_msg, dict):
            error_msg = error_msg.get("message") or str(error_msg)
    if not error_msg:
        error_msg = "Failed to create Purchase Order"

    if is_transient_error(error_msg):
        queue_id = enqueue_job(
            "purchase_submit_order",
            payload={
                "supplier": data.get('supplier'),
                "company": data.get('company'),
                "items": data.get('items') or [],
                "transaction_date": data.get('transaction_date'),
                "schedule_date": data.get('schedule_date')
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
            "message": f"ERPNext unreachable. Saved offline as queue #{queue_id}. It will auto-sync when connection returns."
        })

    return jsonify({"status": "error", "message": error_msg}), 400


@purchase_bp.route('/report')
def purchase_reports():
    """Serves Purchase reports UI."""
    return render_template('purchase_reports.html')


@purchase_bp.route('/api/report_metadata', methods=['GET'])
def report_metadata():
    """Metadata for Purchase reports filters."""
    purchase_engine = get_purchase_engine()
    snapshot_key = f"purchase:report_metadata:{session.get('erp_url') or ''}"

    if not purchase_engine.is_erp_reachable(timeout=2):
        snap = load_snapshot(snapshot_key)
        if snap:
            snap["offline"] = True
            snap["from_temp_db"] = True
            return jsonify(snap)
        return jsonify({
            "suppliers": [],
            "companies": [],
            "offline": True,
            "from_temp_db": False
        }), 503

    payload = {
        "suppliers": purchase_engine.get_resource_list("Supplier"),
        "companies": purchase_engine.get_resource_list("Company"),
        "offline": False,
        "from_temp_db": False
    }
    save_snapshot(snapshot_key, {"suppliers": payload["suppliers"], "companies": payload["companies"]})
    return jsonify(payload)


@purchase_bp.route('/api/report', methods=['GET'])
def purchase_report():
    """Fetch report rows for PO/PI/PR with totals."""
    purchase_engine = get_purchase_engine()
    report_type = (request.args.get('report_type') or 'purchase_order').lower()
    from_date = request.args.get('from_date')
    to_date = request.args.get('to_date')
    company = request.args.get('company')
    supplier = request.args.get('supplier')
    status = request.args.get('status') or 'all'
    start = request.args.get('start', 0, type=int)
    page_length = request.args.get('page_length', 300, type=int)

    snapshot_key = (
        f"purchase:report:{session.get('erp_url') or ''}:"
        f"{report_type}:{from_date or ''}:{to_date or ''}:{company or ''}:{supplier or ''}:{status or ''}:{start}:{page_length}"
    )

    if not purchase_engine.is_erp_reachable(timeout=2):
        snap = load_snapshot(snapshot_key)
        if snap:
            snap["offline"] = True
            snap["from_temp_db"] = True
            return jsonify(snap)
        return jsonify({
            "rows": [],
            "summary": {"count": 0, "total_amount": 0, "total_outstanding": 0, "total_paid": 0},
            "offline": True,
            "from_temp_db": False
        }), 503

    rows = purchase_engine.get_purchase_document_report(
        report_type=report_type,
        from_date=from_date,
        to_date=to_date,
        company=company,
        supplier=supplier,
        status=status,
        start=start,
        page_length=page_length
    )

    total_amount = 0.0
    total_outstanding = 0.0
    total_paid = 0.0
    for row in rows:
        total_amount += float(row.get("rounded_total") or row.get("grand_total") or 0)
        total_outstanding += float(row.get("outstanding_amount") or 0)
        total_paid += float(row.get("paid_amount") or 0)

    payload = {
        "rows": rows,
        "summary": {
            "count": len(rows),
            "total_amount": total_amount,
            "total_outstanding": total_outstanding,
            "total_paid": total_paid
        },
        "offline": False,
        "from_temp_db": False
    }
    save_snapshot(snapshot_key, payload)
    return jsonify(payload)


@purchase_bp.route('/api/convert', methods=['POST'])
def convert_purchase_document():
    """Convert one purchase document to another linked purchase document."""
    purchase_engine = get_purchase_engine()
    data = request.json or {}
    source_type = (data.get("source_type") or "").lower()
    target_type = (data.get("target_type") or "").lower()
    source_name = (data.get("source_name") or "").strip()

    type_map = {
        "purchase_order": "Purchase Order",
        "purchase_invoice": "Purchase Invoice",
        "purchase_receipt": "Purchase Receipt"
    }
    source_doctype = type_map.get(source_type)
    target_doctype = type_map.get(target_type)
    if not source_doctype or not target_doctype or not source_name:
        return jsonify({"status": "error", "message": "Invalid conversion request."}), 400

    result = purchase_engine.convert_purchase_document(
        source_doctype=source_doctype,
        source_name=source_name,
        target_doctype=target_doctype,
        submit=True
    )

    created = result.get("message") if isinstance(result, dict) else None
    if isinstance(created, dict) and created.get("name"):
        return jsonify({
            "status": "success",
            "message": f"{target_doctype} {created['name']} created from {source_name}.",
            "doc": created
        })

    error_msg = purchase_engine._extract_erp_error(result)
    if not error_msg and isinstance(result, dict):
        error_msg = result.get("error") or result.get("message")
    return jsonify({"status": "error", "message": error_msg or "Conversion failed."}), 400
