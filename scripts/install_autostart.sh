#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_SYSTEMD_DIR="${HOME}/.config/systemd/user"
SERVICE_NAME="smbits-autostart.service"

mkdir -p "${USER_SYSTEMD_DIR}"
cp "${REPO_DIR}/scripts/${SERVICE_NAME}" "${USER_SYSTEMD_DIR}/${SERVICE_NAME}"

systemctl --user daemon-reload
systemctl --user enable --now "${SERVICE_NAME}"

echo "Installed and started ${SERVICE_NAME}"
echo "Check status with: systemctl --user status ${SERVICE_NAME}"
