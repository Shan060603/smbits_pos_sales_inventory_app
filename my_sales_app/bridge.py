import os
import requests
import json
import datetime
from dotenv import load_dotenv

# Load secrets from .env
load_dotenv()

class SMBITSBridge:
    def __init__(self, url=None, sid=None, csrf_token=None):
        self.url = (url or os.getenv("ERPNEXT_URL", "")).rstrip('/')
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
        """Quick connectivity check to avoid multiple noisy failures when offline."""
        if not self.url:
            return False
        try:
            endpoint = f"{self.url}/api/method/frappe.auth.get_logged_user"
            res = requests.get(endpoint, headers=self.headers, timeout=timeout)
            return res.status_code in (200, 401, 403)
        except Exception:
            return False

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

    def send_sales_invoice(
        self,
        customer,
        company,
        items,
        discount=0,
        posting_date=None,
        due_date=None,
        mode_of_payment=None,
        paid_amount=0,
        project=None,
        cost_center=None
    ):
        """Creates a Sales Invoice directly from POS checkout data."""
        endpoint = f"{self.url}/api/resource/Sales Invoice"
        
        today = datetime.date.today().strftime('%Y-%m-%d')
        
        # Clean up the items list to ensure ERPNext accepts the structure
        # ERPNext usually requires 'qty' and 'rate' inside each item object
        formatted_items = []
        for item in items:
            base_rate = float(item['rate'])
            line = {
                "item_code": item['item_code'],
                "qty": float(item['qty']),
                "rate": base_rate,
                "warehouse": item.get('warehouse')
            }
            item_discount = float(item.get('discount_percentage') or 0)
            if item_discount > 0:
                line["discount_percentage"] = item_discount
                line["price_list_rate"] = base_rate
                line["rate"] = base_rate * (1 - (item_discount / 100.0))
            formatted_items.append(line)

        payload = {
            "customer": customer,
            "company": company,
            "posting_date": posting_date or today,
            "due_date": due_date or today,
            "project": project if project else None,
            "cost_center": cost_center if cost_center else None,
            "items": formatted_items,
            "additional_discount_percentage": float(discount),
            "apply_discount_on": "Grand Total",
            "is_pos": 1,
            "update_stock": 1
        }

        if mode_of_payment and float(paid_amount or 0) > 0:
            payload["payments"] = [{
                "mode_of_payment": mode_of_payment,
                "amount": float(paid_amount)
            }]
            payload["paid_amount"] = float(paid_amount)

        try:
            response = requests.post(endpoint, headers=self.headers, json=payload)
            created = response.json()
            if "data" not in created:
                return created

            # Convert invoice to Submitted (docstatus=1) immediately.
            submit_endpoint = f"{self.url}/api/method/frappe.client.submit"
            submit_payload = {"doc": created["data"]}
            submit_response = requests.post(submit_endpoint, headers=self.headers, json=submit_payload)
            submitted = submit_response.json()
            return submitted if "message" in submitted else created
        except Exception as e:
            return {"error": str(e)}

    def create_customer(self, customer_name):
        """Creates a Customer in ERPNext with default group/territory."""
        if not customer_name or not customer_name.strip():
            return {"error": "Customer name is required."}

        customer_groups = self.get_resource_list("Customer Group")
        territories = self.get_resource_list("Territory")
        customer_group = customer_groups[0]["name"] if customer_groups else "All Customer Groups"
        territory = territories[0]["name"] if territories else "All Territories"

        payload = {
            "customer_name": customer_name.strip(),
            "customer_type": "Company",
            "customer_group": customer_group,
            "territory": territory
        }

        endpoint = f"{self.url}/api/resource/Customer"
        try:
            response = requests.post(endpoint, headers=self.headers, json=payload)
            return response.json()
        except Exception as e:
            return {"error": str(e)}

    def create_item(self, item_code, item_name, stock_uom, sales_price=0, purchase_price=0):
        """Creates an Item and optional Standard Selling/Buying prices."""
        item_code = (item_code or "").strip()
        item_name = (item_name or item_code).strip()
        stock_uom = (stock_uom or "").strip()
        if not item_code or not stock_uom:
            return {"ok": False, "error": "Item code and UOM are required."}

        item_groups = self.get_resource_list("Item Group")
        item_group = item_groups[0]["name"] if item_groups else "All Item Groups"

        item_payload = {
            "item_code": item_code,
            "item_name": item_name,
            "stock_uom": stock_uom,
            "item_group": item_group,
            "is_stock_item": 1
        }

        try:
            endpoint = f"{self.url}/api/resource/Item"
            response = requests.post(endpoint, headers=self.headers, json=item_payload)
            body = response.json() if response.text else {}
            if response.status_code not in (200, 201) or "data" not in body:
                msg = body.get("message")
                if isinstance(msg, dict):
                    msg = msg.get("message")
                return {"ok": False, "error": msg or body.get("error") or f"Failed to create item ({response.status_code})"}

            created_code = body["data"]["name"]
            if float(sales_price or 0) > 0:
                self._create_item_price(created_code, "Standard Selling", sales_price)
            if float(purchase_price or 0) > 0:
                self._create_item_price(created_code, "Standard Buying", purchase_price)

            return {"ok": True, "data": body["data"]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

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

    def get_sales_invoice_report(
        self,
        from_date=None,
        to_date=None,
        company=None,
        customer=None,
        status="submitted",
        start=0,
        page_length=200
    ):
        """Fetch Sales Invoice rows with optional filters for reporting."""
        endpoint = f"{self.url}/api/resource/Sales Invoice"
        filters = []

        if from_date:
            filters.append(["posting_date", ">=", from_date])
        if to_date:
            filters.append(["posting_date", "<=", to_date])
        if company:
            filters.append(["company", "=", company])
        if customer:
            filters.append(["customer", "=", customer])

        status_map = {
            "submitted": 1,
            "draft": 0,
            "cancelled": 2
        }
        if status in status_map:
            filters.append(["docstatus", "=", status_map[status]])

        params = {
            "fields": json.dumps([
                "name",
                "posting_date",
                "customer",
                "company",
                "grand_total",
                "rounded_total",
                "outstanding_amount",
                "paid_amount",
                "docstatus"
            ]),
            "filters": json.dumps(filters),
            "order_by": "posting_date desc, creation desc",
            "limit_start": int(start or 0),
            "limit_page_length": int(page_length or 200)
        }

        try:
            response = requests.get(endpoint, headers=self.headers, params=params)
            response.raise_for_status()
            return response.json().get("data", [])
        except Exception as e:
            print(f"❌ Sales Invoice Report Fetch Error: {str(e)}")
            return []

    def get_sales_order_report(
        self,
        from_date=None,
        to_date=None,
        company=None,
        customer=None,
        status="submitted",
        start=0,
        page_length=200
    ):
        """Fetch Sales Order rows with optional filters for reporting."""
        endpoint = f"{self.url}/api/resource/Sales Order"
        filters = []

        if from_date:
            filters.append(["transaction_date", ">=", from_date])
        if to_date:
            filters.append(["transaction_date", "<=", to_date])
        if company:
            filters.append(["company", "=", company])
        if customer:
            filters.append(["customer", "=", customer])

        status_map = {
            "submitted": 1,
            "draft": 0,
            "cancelled": 2
        }
        if status in status_map:
            filters.append(["docstatus", "=", status_map[status]])

        params = {
            "fields": json.dumps([
                "name",
                "transaction_date",
                "customer",
                "company",
                "grand_total",
                "rounded_total",
                "per_billed",
                "per_delivered",
                "status",
                "docstatus"
            ]),
            "filters": json.dumps(filters),
            "order_by": "transaction_date desc, creation desc",
            "limit_start": int(start or 0),
            "limit_page_length": int(page_length or 200)
        }

        try:
            response = requests.get(endpoint, headers=self.headers, params=params)
            response.raise_for_status()
            return response.json().get("data", [])
        except Exception as e:
            print(f"❌ Sales Order Report Fetch Error: {str(e)}")
            return []
