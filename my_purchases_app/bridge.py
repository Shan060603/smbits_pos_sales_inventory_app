import os
import requests
import json
import datetime
from pathlib import Path
from flask import Blueprint, render_template, request, jsonify
from dotenv import load_dotenv

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
    def __init__(self):
        # rstrip ensures no double slashes in API calls
        self.url = (os.getenv("ERPNEXT_URL") or "").rstrip('/')
        self.api_key = os.getenv("API_KEY")
        self.api_secret = os.getenv("API_SECRET")
        self.headers = {
            "Authorization": f"token {self.api_key}:{self.api_secret}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

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

    def send_purchase_order(self, supplier, company, items, transaction_date=None, schedule_date=None):
        """Sends Purchase Order data to ERPNext as a Draft (docstatus: 0)."""
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
            "docstatus": 0, 
            "items": formatted_items
        }

        try:
            response = requests.post(endpoint, headers=self.headers, data=json.dumps(payload))
            return response.json()
        except Exception as e:
            return {"error": str(e)}

# --- 2. INITIALIZE ENGINE & DEFINE ROUTES ---
purchase_engine = SMBITSPurchaseBridge()

@purchase_bp.route('/')
def purchase_home():
    """Serves the main Purchase UI. Use 'purchase_index.html' in the templates folder."""
    return render_template('purchase_index.html')

@purchase_bp.route('/api/metadata', methods=['GET'])
def get_metadata():
    """Endpoint for the UI to populate dropdowns."""
    return jsonify({
        "suppliers": purchase_engine.get_resource_list("Supplier"),
        "items": purchase_engine.get_resource_list("Item"),
        "warehouses": purchase_engine.get_resource_list("Warehouse"),
        "companies": purchase_engine.get_resource_list("Company")
    })

@purchase_bp.route('/api/price/<item_code>')
def get_price(item_code):
    """Fetches item buying price on-the-fly."""
    price = purchase_engine.get_purchase_price(item_code)
    return jsonify({"price": price})

@purchase_bp.route('/api/submit', methods=['POST'])
def submit_purchase():
    """Processes the PO submission from the frontend."""
    data = request.json
    result = purchase_engine.send_purchase_order(
        supplier=data.get('supplier'),
        company=data.get('company'),
        items=data.get('items'),
        transaction_date=data.get('transaction_date'),
        schedule_date=data.get('schedule_date')
    )
    
    if "data" in result:
        return jsonify({"status": "success", "message": f"PO {result['data']['name']} created!"})
    return jsonify({"status": "error", "message": "Failed to create Purchase Order"}), 400