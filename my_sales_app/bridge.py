import os
import requests
import json
import datetime
from dotenv import load_dotenv

# Load secrets from .env
load_dotenv()

class SMBITSBridge:
    def __init__(self):
        # rstrip handles cases where the URL might have a trailing slash in .env
        self.url = os.getenv("ERPNEXT_URL", "").rstrip('/')
        self.api_key = os.getenv("API_KEY")
        self.api_secret = os.getenv("API_SECRET")
        self.headers = {
            "Authorization": f"token {self.api_key}:{self.api_secret}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    def get_resource_list(self, doctype):
        """Fetches resources with specific fields based on doctype for dropdowns."""
        endpoint = f"{self.url}/api/resource/{doctype}"
        
        # Mapping doctypes to the specific fields needed for filtering and display
        field_map = {
            "Item": ["name", "item_name"],
            "Warehouse": ["name", "company"],
            "Project": ["name", "project_name", "company"],
            "Cost Center": ["name", "company"],
            "Customer": ["name", "customer_name", "company"],
            "Company": ["name"]
        }
        
        fields = field_map.get(doctype, ["name"])
        
        params = {
            "fields": json.dumps(fields),
            "limit_page_length": 2000 # Higher limit for large item lists
        }
        
        try:
            response = requests.get(endpoint, headers=self.headers, params=params)
            response.raise_for_status() # Raise error for 401/404/500
            return response.json().get("data", [])
        except Exception as e:
            print(f"❌ Fetch Error [{doctype}]: {str(e)}")
            return []

    def get_item_price(self, item_code):
        """Fetches the 'Standard Selling' price. Added check for 'Selling' flag."""
        endpoint = f"{self.url}/api/resource/Item Price"
        params = {
            "fields": json.dumps(["price_list_rate"]),
            "filters": json.dumps([
                ["item_code", "=", item_code],
                ["price_list", "=", "Standard Selling"]
            ]),
            "limit_page_length": 1
        }
        try:
            response = requests.get(endpoint, headers=self.headers, params=params)
            if response.status_code == 200:
                data = response.json().get("data", [])
                # Ensure we return a float even if data is missing
                return float(data[0]['price_list_rate']) if data else 0.0
            return 0.0
        except Exception as e:
            print(f"❌ Price Fetch Error: {str(e)}")
            return 0.0

    def get_stock_level(self, item_code, warehouse):
        """Fetches actual stock from the Bin doctype."""
        endpoint = f"{self.url}/api/resource/Bin"
        params = {
            "fields": json.dumps(["actual_qty"]),
            "filters": json.dumps([
                ["item_code", "=", item_code],
                ["warehouse", "=", warehouse]
            ])
        }
        try:
            response = requests.get(endpoint, headers=self.headers, params=params)
            if response.status_code == 200:
                data = response.json().get("data", [])
                return float(data[0]['actual_qty']) if data else 0.0
            return 0.0
        except Exception as e:
            print(f"❌ Stock Fetch Error: {str(e)}")
            return 0.0

    def send_sales_order(self, customer, company, items, discount=0, posting_date=None, delivery_date=None, project=None, cost_center=None):
        """Sends the Sales Order. ERPNext expects 'transaction_date' for POs/SOs."""
        endpoint = f"{self.url}/api/resource/Sales Order"
        
        today = datetime.date.today().strftime('%Y-%m-%d')
        
        # Clean up the items list to ensure ERPNext accepts the structure
        # ERPNext usually requires 'qty' and 'rate' inside each item object
        formatted_items = []
        for item in items:
            formatted_items.append({
                "item_code": item['item_code'],
                "qty": float(item['qty']),
                "rate": float(item['rate']),
                "warehouse": item.get('warehouse')
            })

        payload = {
            "customer": customer,
            "company": company, 
            "transaction_date": posting_date or today,
            "delivery_date": delivery_date or today,
            "project": project if project else None,
            "cost_center": cost_center if cost_center else None,
            "docstatus": 0, # Draft status
            "items": formatted_items,
            "additional_discount_percentage": float(discount),
            "apply_discount_on": "Grand Total"
        }

        try:
            # Note: We use json=payload which automatically handles Content-Type headers
            response = requests.post(endpoint, headers=self.headers, json=payload)
            return response.json()
        except Exception as e:
            return {"error": str(e)}