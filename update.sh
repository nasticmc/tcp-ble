#!/usr/bin/env bash
# Pull the latest proxy code and restart the service.
set -euo pipefail

INSTALL_DIR="/opt/meshcore-ble-proxy"
SERVICE_NAME="meshcore-ble-proxy"
VENV="$INSTALL_DIR/venv"

# ── 1. pull latest code ─────────────────────────────────────────────────────
echo "==> Pulling latest code…"
git pull --ff-only

# ── 2. update proxy files ───────────────────────────────────────────────────
echo "==> Copying proxy.py to $INSTALL_DIR…"
sudo cp proxy.py "$INSTALL_DIR/"

# Only reinstall packages when requirements.txt actually changed.
if ! diff -q requirements.txt "$INSTALL_DIR/requirements.txt" > /dev/null 2>&1; then
    echo "==> requirements.txt changed — updating packages…"
    sudo cp requirements.txt "$INSTALL_DIR/"
    sudo "$VENV/bin/pip" install --quiet --upgrade pip
    sudo "$VENV/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
else
    echo "==> requirements.txt unchanged — skipping pip install"
fi

# ── 3. restart service ──────────────────────────────────────────────────────
echo "==> Restarting $SERVICE_NAME…"
sudo systemctl restart "$SERVICE_NAME"
sudo systemctl status "$SERVICE_NAME" --no-pager -l

echo ""
echo "Done.  Follow logs with:"
echo "  sudo journalctl -u $SERVICE_NAME -f"
