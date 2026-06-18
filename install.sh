#!/usr/bin/env bash
#
# install.sh — set up server_monitor on a Linux host (Proxmox / TrueNAS SCALE / Plex).
#
# Installs into /opt/server-monitor with a dedicated virtualenv, drops a config
# template at /etc/server-monitor/config.yaml, and enables the systemd service.
#
# Usage:  sudo ./install.sh
#
set -euo pipefail

APP_DIR=/opt/server-monitor
CONF_DIR=/etc/server-monitor
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root (sudo ./install.sh)" >&2
  exit 1
fi

echo "==> Checking for sensor tools"
command -v sensors    >/dev/null 2>&1 || echo "   ! 'sensors' not found  (apt install lm-sensors) — CPU temps will use /sys fallback"
command -v smartctl   >/dev/null 2>&1 || echo "   ! 'smartctl' not found (apt install smartmontools) — disk temps disabled"
command -v nvidia-smi >/dev/null 2>&1 || echo "   ! 'nvidia-smi' not found — GPU temps disabled (fine if no NVIDIA GPU)"

echo "==> Installing application to ${APP_DIR}"
mkdir -p "${APP_DIR}"
cp "${SRC_DIR}/server_monitor.py" "${APP_DIR}/"

echo "==> Creating virtualenv"
if [[ ! -d "${APP_DIR}/venv" ]]; then
  python3 -m venv "${APP_DIR}/venv"
fi
"${APP_DIR}/venv/bin/pip" install --quiet --upgrade pip
"${APP_DIR}/venv/bin/pip" install --quiet -r "${SRC_DIR}/requirements.txt"

echo "==> Installing config to ${CONF_DIR}"
mkdir -p "${CONF_DIR}"
if [[ ! -f "${CONF_DIR}/config.yaml" ]]; then
  cp "${SRC_DIR}/config.example.yaml" "${CONF_DIR}/config.yaml"
  chmod 600 "${CONF_DIR}/config.yaml"
  echo "   -> Edit ${CONF_DIR}/config.yaml (set MQTT broker + node_name) before starting."
else
  echo "   -> ${CONF_DIR}/config.yaml already exists, leaving it untouched."
fi

echo "==> Installing systemd service"
cp "${SRC_DIR}/systemd/server-monitor.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable server-monitor.service

echo
echo "Done. Next steps:"
echo "  1. Edit ${CONF_DIR}/config.yaml"
echo "  2. systemctl start server-monitor"
echo "  3. journalctl -u server-monitor -f"
