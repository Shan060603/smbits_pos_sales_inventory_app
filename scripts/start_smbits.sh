#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${HOME}/erpnext-custom-web-app"
cd "${APP_DIR}"

PORT="${PORT:-5000}"

exec "${APP_DIR}/venv/bin/python" -c "from app import app, start_outbox_worker; start_outbox_worker(interval_seconds=20); app.run(host='0.0.0.0', port=int('${PORT}'), debug=False, threaded=True, use_reloader=False)"
