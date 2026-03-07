import os
import socket
import time
import requests
from flask import Flask, render_template, Response, request, redirect, url_for, session, flash
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Import Blueprints
from my_inventory_app.bridge import inventory_bp
from my_sales_app.main import sales_bp
from my_purchases_app.bridge import purchase_bp

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or os.urandom(24)

# Register application modules
app.register_blueprint(inventory_bp, url_prefix='/inventory')
app.register_blueprint(sales_bp, url_prefix='/sales')
app.register_blueprint(purchase_bp, url_prefix='/purchases')

ERPNEXT_URL = (os.getenv("ERPNEXT_URL") or "").rstrip("/")
IDLE_TIMEOUT_SECONDS = int(os.getenv("IDLE_TIMEOUT_SECONDS", "60"))


def _is_public_path(path):
    public_prefixes = (
        "/login",
        "/health",
        "/favicon.ico",
        "/static/"
    )
    if any(path.startswith(prefix) for prefix in public_prefixes):
        return True
    if path.startswith("/sales/static/") or path.startswith("/inventory/static/") or path.startswith("/purchases/static/"):
        return True
    return False


def _authenticate_erpnext_user(username, password):
    if not ERPNEXT_URL:
        return {"ok": False, "message": "ERPNEXT_URL is not configured on server."}
    try:
        auth_session = requests.Session()
        login_res = auth_session.post(
            f"{ERPNEXT_URL}/api/method/login",
            data={"usr": username, "pwd": password},
            timeout=15
        )
        payload = login_res.json() if login_res.text else {}
        if login_res.status_code != 200:
            return {"ok": False, "message": payload.get("message") or "Invalid ERPNext credentials."}

        user_res = auth_session.get(
            f"{ERPNEXT_URL}/api/method/frappe.auth.get_logged_user",
            timeout=15
        )
        user_payload = user_res.json() if user_res.text else {}
        user_id = user_payload.get("message") if user_res.status_code == 200 else username
        return {"ok": True, "user": user_id or username}
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
        return render_template("login.html", next_url=next_url)

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    next_url = request.args.get("next") or request.form.get("next") or url_for("dashboard")
    if not str(next_url).startswith("/"):
        next_url = url_for("dashboard")

    if not username or not password:
        flash("ERPNext username and password are required.", "error")
        return render_template("login.html", next_url=next_url), 400

    auth = _authenticate_erpnext_user(username, password)
    if not auth.get("ok"):
        flash(auth.get("message") or "Login failed.", "error")
        return render_template("login.html", next_url=next_url), 401

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


@app.route("/favicon.ico")
def favicon():
    """Avoid repeated 404 noise when no favicon asset is configured."""
    return Response(status=204)


if __name__ == "__main__":

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
