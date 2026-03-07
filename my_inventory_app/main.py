import socket
from flask import Flask, render_template, request, jsonify
from bridge import inventory_bp, inventory_engine

app = Flask(__name__)

# Register the blueprint to maintain the /inventory/ prefix if needed
# Or you can keep the routes direct as you had them.
# Given your HTML calls /inventory/api/..., we'll use the blueprint:
app.register_blueprint(inventory_bp, url_prefix='/inventory')

@app.route('/')
def index():
    """Redirects to the Inventory Dashboard or serves a landing page."""
    return render_template('inventory_index.html')

# Note: The routes below are now handled inside the inventory_bp in bridge.py
# If you prefer to keep them here in main, use this updated logic:

@app.route('/inventory/api/metadata', methods=['GET'])
def get_metadata():
    """Fetches ERPNext metadata for report filters."""
    try:
        warehouses = inventory_engine.get_resource_list("Warehouse")
        companies = inventory_engine.get_resource_list("Company")

        return jsonify({
            "warehouses": warehouses,
            "companies": [c['name'] for c in companies]
        })
    except Exception as e:
        print(f"❌ SMBITS Metadata Error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/inventory/api/stock_entry', methods=['POST'])
def create_stock_entry():
    """Submits Material adjustments directly to the ERPNext ledger."""
    data = request.json or {}
    result = inventory_engine.create_stock_entry(
        item_code=data.get('item_code'),
        warehouse=data.get('warehouse'),
        qty=data.get('qty'),
        purpose=data.get('purpose', 'Material Receipt')
    )
    
    if result and "name" in str(result): # ERPNext returns the document name on success
        return jsonify({"status": "success", "message": "Ledger Updated", "data": result})
    
    return jsonify({"status": "error", "message": result.get('error', 'Sync failed')}), 400

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip

if __name__ == '__main__':
    current_ip = get_local_ip()
    hostname = socket.gethostname()
    # Changed to 5000 to match your previous logs, or keep 5002 if preferred
    port = 5000 

    print("\n" + "═"*60)
    print(" 📦  SMBITS MASTER STOCK - COMMAND CENTER")
    print("═"*60)
    print(f" 🌐 NETWORK ACCESS:      http://{current_ip}:{port}")
    print(f" 📛 LOCAL HOSTNAME:     http://{hostname}.local:{port}")
    print("─"*60)
    print(" 📢 Status: Connected to ERPNext V15")
    print(" 📢 Mode:   Paginated Bulk Inventory Reporting (20/pg)")
    print("═"*60 + "\n")

    app.run(host='0.0.0.0', port=port, debug=True)