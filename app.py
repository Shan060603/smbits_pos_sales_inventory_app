import os
import socket
from flask import Flask, render_template, Response
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Import Blueprints
from my_inventory_app.bridge import inventory_bp
from my_sales_app.main import sales_bp
from my_purchases_app.bridge import purchase_bp

app = Flask(__name__)

# Register application modules
app.register_blueprint(inventory_bp, url_prefix='/inventory')
app.register_blueprint(sales_bp, url_prefix='/sales')
app.register_blueprint(purchase_bp, url_prefix='/purchases')


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
