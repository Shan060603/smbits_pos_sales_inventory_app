import socket
from flask import Flask, render_template, request, jsonify
from bridge import SMBITSPurchaseBridge # Updated import

app = Flask(__name__)
bridge = SMBITSPurchaseBridge()

@app.route('/')
def index():
    """Serves the Purchase Order interface."""
    return render_template('index.html')

@app.route('/api/metadata', methods=['GET'])
def get_metadata():
    """Fetches Suppliers, Items, Warehouses, and Companies."""
    try:
        raw_warehouses = bridge.get_resource_list("Warehouse")
        raw_suppliers = bridge.get_resource_list("Supplier") # Switched to Supplier
        raw_items = bridge.get_resource_list("Item") 
        raw_companies = bridge.get_resource_list("Company")

        data = {
            "suppliers": [s['name'] for s in raw_suppliers], # Renamed key
            "items": raw_items, 
            "companies": [co['name'] for co in raw_companies],
            "warehouses": raw_warehouses  
        }
        return jsonify(data)
    except Exception as e:
        print(f"Metadata Error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/get_purchase_price/<item_code>')
def get_purchase_price(item_code):
    """Fetches the buying price for a specific item."""
    # This uses the Standard Buying price list as defined in your bridge
    price = bridge.get_purchase_price(item_code)
    return jsonify({"price": price})

@app.route('/api/get_stock')
def get_stock():
    """Fetches stock level (so the user knows if they NEED to buy more)."""
    item_code = request.args.get('item_code')
    warehouse = request.args.get('warehouse')
    if not item_code or not warehouse:
        return jsonify({"error": "Missing parameters"}), 400
    qty = bridge.get_stock_level(item_code, warehouse)
    return jsonify({"qty": qty})

@app.route('/api/submit_purchase_order', methods=['POST'])
def submit_purchase_order():
    """Pushes the PO to ERPNext."""
    data = request.json
    supplier = data.get('supplier')
    company = data.get('company')
    items = data.get('items')
    tax_perc = data.get('taxes_and_charges_added', 0)
    
    # Dates from the frontend
    transaction_date = data.get('transaction_date') 
    schedule_date = data.get('schedule_date')
    
    # Send to the PO-specific bridge method
    result = bridge.send_purchase_order(
        supplier=supplier, 
        company=company, 
        items=items, 
        tax_perc=tax_perc,
        transaction_date=transaction_date,
        schedule_date=schedule_date
    )
    
    if result and "data" in result:
        return jsonify({
            "status": "success", 
            "message": f"PO {result['data']['name']} created for {supplier}!"
        })
    else:
        # Extract the specific ERPNext error if possible
        error_msg = result.get("error") if isinstance(result, dict) else str(result)
        return jsonify({"status": "error", "message": error_msg}), 400

def get_local_ip():
    """Detects the current IP address for network sharing."""
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
    port = 5001 # Changed port to 5001 so you can run it alongside Sales if needed

    print("\n" + "═"*60)
    print(" 📦  SMBITS PURCHASING - EXTERNAL APP")
    print("═"*60)
    print(f" 🌐 ACCESS VIA IP:       http://{current_ip}:{port}")
    print(f" 📛 ACCESS VIA HOSTNAME: http://{hostname}.local:{port}")
    print("─"*60)
    print(" 📢 This app is strictly for Procurement / Suppliers.")
    print("═"*60 + "\n")

    app.run(host='0.0.0.0', port=port, debug=True)