#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${HOME}/erpnext-custom-web-app"
cd "${APP_DIR}"

exec "${APP_DIR}/venv/bin/python" app.py
