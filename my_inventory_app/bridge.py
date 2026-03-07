import os
import json
import requests
from flask import Blueprint, render_template, request, jsonify
from dotenv import load_dotenv

load_dotenv()

inventory_bp = Blueprint(
    "inventory_bp",
    __name__,
    template_folder="templates",
    static_folder="static"
)


# ----------------------------------------------------
# ERP INVENTORY BRIDGE
# ----------------------------------------------------

class SMBITSInventoryBridge:

    def __init__(self):

        self.url = (os.getenv("ERPNEXT_URL") or "").rstrip("/")
        self.api_key = os.getenv("API_KEY")
        self.api_secret = os.getenv("API_SECRET")

        # Persistent session (faster than raw requests)
        self.session = requests.Session()

        # IMPORTANT: update headers instead of replacing them
        self.session.headers.update({
            "Authorization": f"token {self.api_key}:{self.api_secret}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Expect": ""   # prevents ERPNext 417 error
        })

    # ----------------------------------------------------
    # GENERIC RESOURCE FETCH
    # ----------------------------------------------------

    def get_resource_list(self, doctype, filters=None, fields=None):

        endpoint = f"{self.url}/api/resource/{doctype}"

        params = {
            "limit_page_length": 200
        }

        if fields:
            params["fields"] = json.dumps(fields)

        if filters:
            params["filters"] = json.dumps(filters)

        try:

            response = self.session.get(
                endpoint,
                params=params,
                timeout=20
            )

            response.raise_for_status()

            data = response.json()

            return data.get("data", [])

        except Exception as e:

            print(f"❌ SMBITS Fetch Error [{doctype}]: {e}")

            return []

    # ----------------------------------------------------
    # INVENTORY REPORT
    # ----------------------------------------------------

    def get_full_stock_report(self, company=None, start=0, page_length=20):

        try:

            # 1️⃣ Fetch warehouses
            wh_filters = [["company", "=", company]] if company else []

            warehouses = self.get_resource_list(
                "Warehouse",
                filters=wh_filters,
                fields=["name"]
            )

            allowed_whs = {w["name"] for w in warehouses}

            if not allowed_whs:
                return []

            # 2️⃣ Fetch Bins
            bin_endpoint = f"{self.url}/api/resource/Bin"

            bin_params = {
                "fields": json.dumps([
                    "item_code",
                    "item_name",
                    "warehouse",
                    "actual_qty",
                    "valuation_rate"
                ]),
                "limit_page_length": 200,
                "limit_start": start
            }

            bin_res = self.session.get(
                bin_endpoint,
                params=bin_params,
                timeout=25
            )

            bin_res.raise_for_status()

            all_bins = bin_res.json().get("data", [])

            # 3️⃣ Filter warehouses locally
            filtered_bins = [
                b for b in all_bins
                if b.get("warehouse") in allowed_whs
            ]

            paged_bins = filtered_bins[:page_length]

            # 4️⃣ Fetch selling prices
            if paged_bins:

                item_codes = [
                    b["item_code"]
                    for b in paged_bins
                    if b.get("item_code")
                ]

                price_endpoint = f"{self.url}/api/resource/Item Price"

                price_params = {
                    "fields": json.dumps([
                        "item_code",
                        "price_list_rate"
                    ]),
                    "filters": json.dumps([
                        ["item_code", "in", item_codes],
                        ["price_list", "=", "Standard Selling"]
                    ])
                }

                price_res = self.session.get(
                    price_endpoint,
                    params=price_params,
                    timeout=20
                )

                prices = {
                    p["item_code"]: p["price_list_rate"]
                    for p in price_res.json().get("data", [])
                }

                for b in paged_bins:

                    b["selling_price"] = prices.get(
                        b.get("item_code"), 0
                    )

                    b["actual_qty"] = b.get("actual_qty", 0)

                    b["valuation_rate"] = b.get("valuation_rate", 0)

                    b["item_name"] = b.get(
                        "item_name",
                        b.get("item_code", "Unknown")
                    )

            return paged_bins

        except Exception as e:

            print(f"❌ SMBITS Master Report Error: {e}")

            return []

    # ----------------------------------------------------
    # STOCK ENTRY CREATION
    # ----------------------------------------------------

    def create_stock_entry(
        self,
        item_code,
        warehouse,
        qty,
        purpose="Material Receipt"
    ):

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

            response = self.session.post(
                endpoint,
                json=payload,
                timeout=25
            )

            if response.status_code in (200, 201):

                return response.json()

            return {"error": response.text}

        except Exception as e:

            return {"error": str(e)}


# ----------------------------------------------------
# INITIALIZE ENGINE
# ----------------------------------------------------

inventory_engine = SMBITSInventoryBridge()


# ----------------------------------------------------
# ROUTES
# ----------------------------------------------------

@inventory_bp.route("/")
def inventory_home():

    return render_template("inventory_index.html")


@inventory_bp.route("/api/metadata", methods=["GET"])
def get_metadata():

    company = request.args.get("company")

    wh_filters = [["company", "=", company]] if company else []

    warehouses = inventory_engine.get_resource_list(
        "Warehouse",
        filters=wh_filters,
        fields=["name"]
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

    company = request.args.get("company")

    start = request.args.get("start", 0, type=int)

    data = inventory_engine.get_full_stock_report(
        company,
        start=start,
        page_length=20
    )

    return jsonify(data)


@inventory_bp.route("/api/stock_entry", methods=["POST"])
def adjust_stock():

    data = request.json or {}

    result = inventory_engine.create_stock_entry(
        item_code=data.get("item_code"),
        warehouse=data.get("warehouse"),
        qty=data.get("qty"),
        purpose=data.get("purpose", "Material Receipt")
    )

    return jsonify(result)