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

echo "==> Ensuring Python venv support"
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found — install it first (e.g. apt install python3)" >&2
  exit 1
fi
# On Debian/Ubuntu the venv module ships in a separate package.
if ! python3 -m venv --help >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    echo "   installing python3-venv via apt"
    apt-get update -qq && apt-get install -y python3-venv
  else
    echo "python3 venv module unavailable; install python3-venv" >&2
    exit 1
  fi
fi

echo "==> Creating virtualenv"
# Discard a half-built venv left over from a previous failed run.
if [[ -d "${APP_DIR}/venv" && ! -x "${APP_DIR}/venv/bin/python" ]]; then
  rm -rf "${APP_DIR}/venv"
fi
if [[ ! -d "${APP_DIR}/venv" ]]; then
  python3 -m venv "${APP_DIR}/venv"
fi
VENV_PY="${APP_DIR}/venv/bin/python"
# Bootstrap pip if the venv came up without it (happens on some distros).
if ! "${VENV_PY}" -m pip --version >/dev/null 2>&1; then
  "${VENV_PY}" -m ensurepip --upgrade
fi
"${VENV_PY}" -m pip install --quiet --upgrade pip
"${VENV_PY}" -m pip install --quiet -r "${SRC_DIR}/requirements.txt"

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
