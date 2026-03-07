import socket
from flask import Blueprint, render_template, request, jsonify
# Explicit relative import to find the bridge in the same folder
from .bridge import SMBITSBridge

# 1. Define the Blueprint with the template folder specified
sales_bp = Blueprint('sales_bp', __name__, template_folder='templates')

# 2. Initialize the bridge logic
bridge = SMBITSBridge()

@sales_bp.route('/')
def index():
    """Serves the POS interface (sales_index.html)."""
    return render_template('sales_index.html')

@sales_bp.route('/api/metadata', methods=['GET'])
def get_metadata():
    """Fetches all dropdown data for Customers, Projects, and Cost Centers."""
    try:
        # Fetching raw lists from ERPNext via the bridge
        raw_warehouses = bridge.get_resource_list("Warehouse")
        raw_customers = bridge.get_resource_list("Customer")
        raw_items = bridge.get_resource_list("Item") 
        raw_companies = bridge.get_resource_list("Company")
        raw_projects = bridge.get_resource_list("Project")
        raw_cost_centers = bridge.get_resource_list("Cost Center")

        # We keep the objects (dictionaries) so JS can access .name and .company properties
        data = {
            "customers": raw_customers, 
            "items": raw_items, 
            "companies": raw_companies, # Changed: sending full objects for .name access
            "warehouses": raw_warehouses,
            "projects": raw_projects,      
            "cost_centers": raw_cost_centers 
        }
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@sales_bp.route('/api/get_price/<path:item_code>')
def get_price(item_code):
    """Fetches the standard selling rate for an item."""
    price = bridge.get_item_price(item_code)
    return jsonify({"price": price})

@sales_bp.route('/api/get_stock')
def get_stock():
    """Fetches real-time stock levels for a specific item and warehouse."""
    item_code = request.args.get('item_code')
    warehouse = request.args.get('warehouse')
    if not item_code or not warehouse:
        return jsonify({"error": "Missing parameters"}), 400
    qty = bridge.get_stock_level(item_code, warehouse)
    return jsonify({"qty": qty})

@sales_bp.route('/api/submit', methods=['POST'])
def submit_order():
    """Transmits the POS cart to ERPNext as a Sales Order."""
    data = request.json
    result = bridge.send_sales_order(
        customer=data.get('customer'), 
        company=data.get('company'), 
        items=data.get('items'), 
        discount=data.get('additional_discount_percentage', 0),
        posting_date=data.get('posting_date'),
        delivery_date=data.get('delivery_date'),
        project=data.get('project'),
        cost_center=data.get('cost_center')
    )
    
    # Check if ERPNext returned a successful document name
    if result and isinstance(result, dict) and "data" in result:
        return jsonify({
            "status": "success", 
            "message": f"Order {result['data']['name']} created!"
        })
    
    # Handle API errors from ERPNext side
    error_msg = result.get('message') if isinstance(result, dict) else str(result)
    return jsonify({"status": "error", "message": error_msg}), 400

def get_local_ip():
    """Utility to find the local IP of your ASUS TUF for mobile access."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip