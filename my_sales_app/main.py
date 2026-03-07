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


@sales_bp.route('/report')
def sales_invoice_report():
    """Serves the sales invoice report UI."""
    return render_template('sales_report.html')

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


@sales_bp.route('/api/report_metadata', methods=['GET'])
def get_report_metadata():
    """Fetch lightweight metadata for sales reporting filters."""
    try:
        customers = bridge.get_resource_list("Customer")
        companies = bridge.get_resource_list("Company")
        return jsonify({
            "customers": customers,
            "companies": companies
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@sales_bp.route('/api/invoice_report', methods=['GET'])
def invoice_report():
    """Returns filtered Sales Invoice rows plus summary totals."""
    from_date = request.args.get('from_date')
    to_date = request.args.get('to_date')
    company = request.args.get('company')
    customer = request.args.get('customer')
    status = (request.args.get('status') or 'submitted').lower()
    start = request.args.get('start', 0, type=int)
    page_length = request.args.get('page_length', 200, type=int)

    rows = bridge.get_sales_invoice_report(
        from_date=from_date,
        to_date=to_date,
        company=company,
        customer=customer,
        status=status,
        start=start,
        page_length=page_length
    )

    total_amount = 0.0
    total_paid = 0.0
    total_outstanding = 0.0
    for row in rows:
        total_amount += float(row.get('rounded_total') or row.get('grand_total') or 0)
        total_paid += float(row.get('paid_amount') or 0)
        total_outstanding += float(row.get('outstanding_amount') or 0)

    return jsonify({
        "rows": rows,
        "summary": {
            "count": len(rows),
            "total_amount": total_amount,
            "total_paid": total_paid,
            "total_outstanding": total_outstanding
        }
    })

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


@sales_bp.route('/api/customers', methods=['POST'])
def create_customer():
    """Creates a new customer from POS."""
    data = request.json or {}
    customer_name = (data.get('customer_name') or '').strip()
    if not customer_name:
        return jsonify({"status": "error", "message": "Customer name is required."}), 400

    result = bridge.create_customer(customer_name)
    if result and isinstance(result, dict) and 'data' in result:
        return jsonify({
            "status": "success",
            "customer": result['data']
        })

    error_msg = result.get('message') if isinstance(result, dict) else str(result)
    if isinstance(error_msg, dict):
        error_msg = error_msg.get('message') or str(error_msg)
    if not error_msg and isinstance(result, dict):
        error_msg = result.get('error')
    return jsonify({"status": "error", "message": error_msg or "Failed to create customer."}), 400

@sales_bp.route('/api/submit', methods=['POST'])
def submit_order():
    """Transmits the POS cart to ERPNext as a Sales Invoice."""
    data = request.json or {}
    items = data.get('items') or []
    customer = data.get('customer')
    company = data.get('company')
    mode_of_payment = data.get('mode_of_payment')
    paid_amount = float(data.get('paid_amount') or 0)
    discount = float(data.get('additional_discount_percentage') or 0)

    if not customer:
        return jsonify({"status": "error", "message": "Customer is required."}), 400
    if not company:
        return jsonify({"status": "error", "message": "Company is required."}), 400
    if not items:
        return jsonify({"status": "error", "message": "Cart is empty."}), 400
    if not mode_of_payment:
        return jsonify({"status": "error", "message": "Mode of payment is required."}), 400

    subtotal = 0.0
    for row in items:
        qty = float(row.get('qty') or 0)
        rate = float(row.get('rate') or 0)
        line_discount = float(row.get('discount_percentage') or 0)
        line_total = qty * rate * (1 - (line_discount / 100.0))
        subtotal += line_total

    grand_total = subtotal * (1 - (discount / 100.0))
    if paid_amount < grand_total:
        return jsonify({
            "status": "error",
            "message": f"Paid amount is less than total. Need at least {grand_total:.2f}."
        }), 400

    result = bridge.send_sales_invoice(
        customer=customer,
        company=company,
        items=items,
        discount=data.get('additional_discount_percentage', 0),
        posting_date=data.get('posting_date'),
        due_date=data.get('delivery_date'),
        mode_of_payment=mode_of_payment,
        paid_amount=paid_amount,
        project=data.get('project'),
        cost_center=data.get('cost_center')
    )
    
    invoice_doc = None
    if result and isinstance(result, dict):
        invoice_doc = result.get("message") or result.get("data")

    # Check if ERPNext returned a successful document
    if isinstance(invoice_doc, dict) and invoice_doc.get("name"):
        change = paid_amount - grand_total
        status_label = "Submitted" if int(invoice_doc.get("docstatus") or 0) == 1 else "Created"
        return jsonify({
            "status": "success", 
            "message": f"Invoice {invoice_doc['name']} {status_label}. Change: {change:.2f}"
        })
    
    # Handle API errors from ERPNext side
    error_msg = result.get('message') if isinstance(result, dict) else str(result)
    if isinstance(error_msg, dict):
        error_msg = error_msg.get('message') or str(error_msg)
    if not error_msg and isinstance(result, dict):
        error_msg = result.get('error')
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
