import os
import requests

ERPNEXT_URL = os.getenv("ERPNEXT_URL")
API_KEY = os.getenv("ERP_API_KEY")
API_SECRET = os.getenv("ERP_API_SECRET")

headers = {
    "Authorization": f"token {API_KEY}:{API_SECRET}",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Expect": ""   # prevents 417 EXPECTATION FAILED
}


def erp_get(resource, fields=None, filters=None, limit=200, start=0):
    """
    Safe ERPNext GET wrapper
    """

    url = f"{ERPNEXT_URL}/api/resource/{resource}"

    params = {
        "limit_page_length": limit,
        "limit_start": start
    }

    if fields:
        params["fields"] = str(fields)

    if filters:
        params["filters"] = str(filters)

    try:
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        return r.json().get("data", [])

    except requests.exceptions.RequestException as e:
        print(f"❌ ERP API Error [{resource}]: {e}")
        return []