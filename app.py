import os
import socket
import time
import requests
from urllib.parse import urlparse
from flask import Flask, render_template, Response, request, redirect, url_for, session, flash, send_from_directory
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Import Blueprints
from my_inventory_app.bridge import inventory_bp
from my_sales_app.main import sales_bp
from my_purchases_app.bridge import purchase_bp
from offline_outbox import init_outbox, start_outbox_worker, outbox_status, process_pending_jobs, save_snapshot

app = Flask(__name__)
# Use a stable key across debug reload processes so first-login session is not lost.
app.secret_key = os.getenv("FLASK_SECRET_KEY", "smbits-local-dev-secret")
init_outbox()

# Register application modules
app.register_blueprint(inventory_bp, url_prefix='/inventory')
app.register_blueprint(sales_bp, url_prefix='/sales')
app.register_blueprint(purchase_bp, url_prefix='/purchases')

IDLE_TIMEOUT_SECONDS = int(os.getenv("IDLE_TIMEOUT_SECONDS", "300"))


def _normalize_erp_url(raw_url):
    raw = (raw_url or "").strip()
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = f"http://{raw}"
    return raw.rstrip("/")


def _is_public_path(path):
    public_prefixes = (
        "/login",
        "/health",
        "/favicon.ico",
        "/manifest.webmanifest",
        "/sw.js",
        "/static/"
    )
    if any(path.startswith(prefix) for prefix in public_prefixes):
        return True
    if path.startswith("/sales/static/") or path.startswith("/inventory/static/") or path.startswith("/purchases/static/"):
        return True
    return False


def _authenticate_erpnext_user(username, password, base_url=None):
    base_url = (base_url or "").rstrip("/")
    if not base_url:
        return {"ok": False, "message": "ERP URL is required."}
    try:
        auth_session = requests.Session()
        login_res = auth_session.post(
            f"{base_url}/api/method/login",
            data={"usr": username, "pwd": password},
            timeout=15
        )
        payload = login_res.json() if login_res.text else {}
        if login_res.status_code != 200:
            return {"ok": False, "message": payload.get("message") or "Invalid ERPNext credentials."}

        user_res = auth_session.get(
            f"{base_url}/api/method/frappe.auth.get_logged_user",
            timeout=15
        )
        user_payload = user_res.json() if user_res.text else {}
        user_id = user_payload.get("message") if user_res.status_code == 200 else username
        # Robust session extraction: read from session jar and direct response cookies.
        cookies = auth_session.cookies.get_dict()
        sid = cookies.get("sid") or login_res.cookies.get("sid")
        csrf_token = cookies.get("csrf_token") or login_res.cookies.get("csrf_token") or ""

        # Last-resort parse from Set-Cookie header.
        if not sid:
            set_cookie = login_res.headers.get("Set-Cookie") or ""
            for part in set_cookie.split(";"):
                part = part.strip()
                if part.startswith("sid="):
                    sid = part.split("=", 1)[1].strip()
                    break

        if not sid:
            return {"ok": False, "message": "ERPNext login succeeded but no session ID was returned."}
        return {"ok": True, "user": user_id or username, "sid": sid, "csrf_token": csrf_token}
    except Exception as e:
        return {"ok": False, "message": f"ERPNext login failed: {str(e)}"}


@app.before_request
def require_login():
    if _is_public_path(request.path):
        return None

    if not session.get("erp_user"):
        if "/api/" in request.path:
            return Response(status=401)
        return redirect(url_for("login", next=request.path))
    if not session.get("erp_url") or not session.get("erp_sid"):
        session.clear()
        if "/api/" in request.path:
            return Response(status=401)
        return redirect(url_for("login", next=request.path))

    now = int(time.time())
    last_activity = int(session.get("last_activity_ts") or 0)
    if last_activity and (now - last_activity) > IDLE_TIMEOUT_SECONDS:
        session.clear()
        if "/api/" in request.path:
            return Response(status=401)
        return redirect(url_for("login", next=request.path))

    session["last_activity_ts"] = now
    return None


def get_local_ip():
    """Detect the local network IP address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


@app.context_processor
def inject_idle_timeout():
    return {"idle_timeout_seconds": IDLE_TIMEOUT_SECONDS}


@app.route("/")
def dashboard():
    """Main dashboard UI."""
    return render_template("dashboard.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if session.get("erp_user"):
            return redirect(url_for("dashboard"))
        next_url = request.args.get("next") or url_for("dashboard")
        if not str(next_url).startswith("/"):
            next_url = url_for("dashboard")
        return render_template(
            "login.html",
            next_url=next_url,
            erp_url=""
        )

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    erp_url = _normalize_erp_url(request.form.get("erp_url"))
    next_url = request.args.get("next") or request.form.get("next") or url_for("dashboard")
    if not str(next_url).startswith("/"):
        next_url = url_for("dashboard")

    if not erp_url:
        flash("ERP URL is required.", "error")
        return render_template("login.html", next_url=next_url, erp_url=erp_url), 400
    parsed = urlparse(erp_url)
    if not parsed.scheme or not parsed.netloc:
        flash("ERP URL is invalid. Example: http://192.168.1.20", "error")
        return render_template("login.html", next_url=next_url, erp_url=erp_url), 400
    if not username or not password:
        flash("ERPNext username and password are required.", "error")
        return render_template("login.html", next_url=next_url, erp_url=erp_url), 400

    auth = _authenticate_erpnext_user(username, password, base_url=erp_url)
    if not auth.get("ok"):
        flash(auth.get("message") or "Login failed.", "error")
        return render_template("login.html", next_url=next_url, erp_url=erp_url), 401

    session.clear()
    session["erp_url"] = erp_url
    session["erp_sid"] = auth.get("sid")
    session["erp_csrf_token"] = auth.get("csrf_token") or ""
    session["erp_user"] = auth.get("user")
    session["last_activity_ts"] = int(time.time())
    return redirect(next_url)


@app.route("/logout", methods=["POST", "GET"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/health")
def health():
    """Health check endpoint for monitoring."""
    return {"status": "ok", "service": "smbits-erp-portal"}


@app.route("/api/offline_queue/status")
def offline_queue_status():
    return outbox_status()


@app.route("/api/offline_queue/sync", methods=["POST"])
def offline_queue_sync():
    synced = process_pending_jobs(limit=100)
    status = outbox_status()
    status["synced_now"] = int(synced)
    return status


@app.route("/api/offline_queue/warmup", methods=["POST"])
def offline_queue_warmup():
    erp_url = session.get("erp_url")
    sid = session.get("erp_sid")
    csrf = session.get("erp_csrf_token")
    if not erp_url or not sid:
        return {"status": "error", "message": "Not logged in."}, 401

    from my_sales_app.bridge import SMBITSBridge
    from my_purchases_app.bridge import SMBITSPurchaseBridge
    from my_inventory_app.bridge import SMBITSInventoryBridge

    sales_bridge = SMBITSBridge(url=erp_url, sid=sid, csrf_token=csrf)
    purchase_bridge = SMBITSPurchaseBridge(url=erp_url, sid=sid, csrf_token=csrf)
    inventory_bridge = SMBITSInventoryBridge(url=erp_url, sid=sid, csrf_token=csrf)

    if not sales_bridge.is_erp_reachable(timeout=5):
        return {"status": "error", "message": "ERPNext is not reachable right now."}, 503

    stats = {
        "sales": {},
        "purchases": {},
        "inventory": {}
    }

    # Sales snapshots
    sales_meta = {
        "customers": sales_bridge.get_resource_list("Customer"),
        "items": sales_bridge.get_resource_list("Item"),
        "companies": sales_bridge.get_resource_list("Company"),
        "warehouses": sales_bridge.get_resource_list("Warehouse"),
        "projects": sales_bridge.get_resource_list("Project"),
        "cost_centers": sales_bridge.get_resource_list("Cost Center"),
    }
    save_snapshot(f"sales:metadata:{erp_url}", sales_meta)
    stats["sales"]["metadata"] = {
        "customers": len(sales_meta["customers"]),
        "items": len(sales_meta["items"]),
        "companies": len(sales_meta["companies"]),
        "warehouses": len(sales_meta["warehouses"])
    }

    sales_report_meta = {
        "customers": sales_meta["customers"],
        "companies": sales_meta["companies"]
    }
    save_snapshot(f"sales:report_metadata:{erp_url}", sales_report_meta)
    stats["sales"]["report_metadata"] = {
        "customers": len(sales_report_meta["customers"]),
        "companies": len(sales_report_meta["companies"])
    }

    for report_type in ("invoice", "order"):
        if report_type == "invoice":
            rows = sales_bridge.get_sales_invoice_report(status="submitted", start=0, page_length=200)
            total_amount = sum(float(r.get("rounded_total") or r.get("grand_total") or 0) for r in rows)
            total_paid = sum(float(r.get("paid_amount") or 0) for r in rows)
            total_outstanding = sum(float(r.get("outstanding_amount") or 0) for r in rows)
            payload = {
                "rows": rows,
                "summary": {
                    "count": len(rows),
                    "total_amount": total_amount,
                    "total_paid": total_paid,
                    "total_outstanding": total_outstanding
                },
                "offline": False,
                "from_temp_db": False
            }
            key = f"sales:invoice_report:{erp_url}:::::submitted:0:200"
        else:
            rows = sales_bridge.get_sales_order_report(status="submitted", start=0, page_length=200)
            total_amount = sum(float(r.get("rounded_total") or r.get("grand_total") or 0) for r in rows)
            total_to_bill = 0.0
            for r in rows:
                row_total = float(r.get("rounded_total") or r.get("grand_total") or 0)
                billed_pct = float(r.get("per_billed") or 0)
                total_to_bill += max(row_total * (1 - (billed_pct / 100.0)), 0.0)
            payload = {
                "rows": rows,
                "summary": {"count": len(rows), "total_amount": total_amount, "total_to_bill": total_to_bill},
                "offline": False,
                "from_temp_db": False
            }
            key = f"sales:order_report:{erp_url}:::::submitted:0:200"
        save_snapshot(key, payload)
        stats["sales"][f"{report_type}_report_rows"] = len(rows)

    # Purchase snapshots
    purchase_meta = {
        "suppliers": purchase_bridge.get_resource_list("Supplier"),
        "items": purchase_bridge.get_resource_list("Item"),
        "warehouses": purchase_bridge.get_resource_list("Warehouse"),
        "companies": purchase_bridge.get_resource_list("Company"),
        "uoms": purchase_bridge.get_resource_list("UOM")
    }
    save_snapshot(f"purchase:metadata:{erp_url}", purchase_meta)
    save_snapshot(
        f"purchase:report_metadata:{erp_url}",
        {"suppliers": purchase_meta["suppliers"], "companies": purchase_meta["companies"]}
    )
    stats["purchases"]["metadata"] = {
        "suppliers": len(purchase_meta["suppliers"]),
        "items": len(purchase_meta["items"]),
        "warehouses": len(purchase_meta["warehouses"]),
        "companies": len(purchase_meta["companies"])
    }

    for report_type in ("purchase_order", "purchase_invoice", "purchase_receipt"):
        rows = purchase_bridge.get_purchase_document_report(
            report_type=report_type, status="all", start=0, page_length=300
        )
        total_amount = sum(float(r.get("rounded_total") or r.get("grand_total") or 0) for r in rows)
        total_outstanding = sum(float(r.get("outstanding_amount") or 0) for r in rows)
        total_paid = sum(float(r.get("paid_amount") or 0) for r in rows)
        payload = {
            "rows": rows,
            "summary": {
                "count": len(rows),
                "total_amount": total_amount,
                "total_outstanding": total_outstanding,
                "total_paid": total_paid
            },
            "offline": False,
            "from_temp_db": False
        }
        key = f"purchase:report:{erp_url}:{report_type}:::::all:0:300"
        save_snapshot(key, payload)
        stats["purchases"][f"{report_type}_rows"] = len(rows)

    # Inventory snapshots
    inv_companies = inventory_bridge.get_resource_list("Company", fields=["name"])
    inv_warehouses = inventory_bridge.get_resource_list("Warehouse", fields=["name", "company"])
    inv_meta = {
        "warehouses": inv_warehouses,
        "companies": [c.get("name") for c in inv_companies if c.get("name")],
        "offline": False,
        "from_temp_db": False
    }
    save_snapshot(f"inventory:metadata:{erp_url}:", inv_meta)
    stats["inventory"]["metadata"] = {
        "companies": len(inv_meta["companies"]),
        "warehouses": len(inv_warehouses)
    }

    inv_rows = inventory_bridge.get_full_stock_report(
        company=None, warehouse=None, include_zero_stock=False, start=0, page_length=20
    )
    save_snapshot(f"inventory:stock_report:{erp_url}:::0:0:20", inv_rows)
    stats["inventory"]["stock_rows_page1"] = len(inv_rows)

    inv_item_list = inventory_bridge.get_item_list()
    save_snapshot(f"inventory:item_list:{erp_url}", inv_item_list)
    stats["inventory"]["item_list_rows"] = len(inv_item_list)

    inv_uoms = inventory_bridge.get_resource_list("UOM", fields=["name"], start=0, page_length=2000)
    inv_groups = inventory_bridge.get_resource_list("Item Group", fields=["name"], start=0, page_length=2000)
    inv_filters = {
        "uoms": [u.get("name") for u in inv_uoms if u.get("name")],
        "item_groups": [g.get("name") for g in inv_groups if g.get("name")],
        "offline": False,
        "from_temp_db": False
    }
    save_snapshot(f"inventory:item_list_filters:{erp_url}", inv_filters)
    stats["inventory"]["filter_uoms"] = len(inv_filters["uoms"])
    stats["inventory"]["filter_item_groups"] = len(inv_filters["item_groups"])

    return {"status": "success", "message": "Offline snapshots warmed.", "stats": stats}


@app.route("/favicon.ico")
def favicon():
    """Serve favicon from project root when available."""
    if os.path.exists("favicon.ico"):
        return send_from_directory(".", "favicon.ico")
    return Response(status=204)


@app.route("/manifest.webmanifest")
def webmanifest():
    """Serve PWA manifest."""
    return send_from_directory("static", "manifest.webmanifest")


@app.route("/sw.js")
def service_worker():
    """Serve service worker at root scope."""
    return send_from_directory("static", "sw.js")


if __name__ == "__main__":
    # Start auto-sync worker once in the reloader child process.
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not os.getenv("FLASK_DEBUG"):
        start_outbox_worker(interval_seconds=20)

    current_ip = get_local_ip()
    hostname = socket.gethostname()
    port = int(os.getenv("PORT", 5000))

    print("\n" + "═" * 60)
    print(" ☀️  SMBITS ERP PORTAL - NETWORK DASHBOARD")
    print("═" * 60)
    print(f" 🌐 ACCESS VIA IP:       http://{current_ip}:{port}")
    print(f" 📛 ACCESS VIA HOSTNAME: http://{hostname}.local:{port}")
    print("─" * 60)
    print(" 📢 Share the URL above with tablets/phones on this Wi-Fi.")
    print(" 📱 Interface auto-adapts to tablets, phones, and desktops.")
    print("═" * 60 + "\n")

    app.run(
        host="0.0.0.0",
        port=port,
        debug=True,
        threaded=True
    )
