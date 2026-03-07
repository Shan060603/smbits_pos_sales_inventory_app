import requests
import os
from dotenv import load_dotenv

load_dotenv()

def test_erpnext_sync():
    url = os.getenv("ERPNEXT_URL").rstrip('/')
    api_key = os.getenv("API_KEY")
    api_secret = os.getenv("API_SECRET")
    
    headers = {
        "Authorization": f"token {api_key}:{api_secret}",
        "Content-Type": "application/json"
    }

    print(f"--- Testing Connection to {url} ---")
    
    try:
        # 1. Test basic connectivity
        response = requests.get(f"{url}/api/method/frappe.auth.get_logged_user", headers=headers)
        
        if response.status_code == 200:
            user = response.json().get("message")
            print(f"✅ SUCCESS: Connected as user: {user}")
            
            # 2. Test Permission to Read Items (Needed for Metadata)
            item_check = requests.get(f"{url}/api/resource/Item?limit_page_length=1", headers=headers)
            if item_check.status_code == 200:
                print("✅ PERMISSION: Can read 'Item' doctype.")
            else:
                print(f"❌ PERMISSION ERROR: Cannot read Items. Status: {item_check.status_code}")

            # 3. Test Permission to Create Sales Order (Draft)
            # We don't actually create one, just check the metadata
            so_check = requests.options(f"{url}/api/resource/Sales Order", headers=headers)
            if so_check.status_code == 200:
                print("✅ PERMISSION: Authorized to access 'Sales Order' endpoint.")
            
        else:
            print(f"❌ AUTH FAILED: Check your API Key/Secret. Status: {response.status_code}")
            print(f"Response: {response.text}")

    except Exception as e:
        print(f"❌ CONNECTION ERROR: Could not reach server. {str(e)}")

if __name__ == "__main__":
    test_erpnext_sync()