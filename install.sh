#!/usr/bin/env bash
# Install the MeshCore TCP-to-BLE proxy on a Raspberry Pi Zero 2W (Bookworm).
set -euo pipefail

INSTALL_DIR="/opt/meshcore-ble-proxy"
SERVICE_NAME="meshcore-ble-proxy"
VENV="$INSTALL_DIR/venv"

# ── 1. system dependencies ──────────────────────────────────────────────────
echo "==> Installing system packages…"
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip \
    bluez libglib2.0-dev

# Enable and start bluetoothd
sudo systemctl enable bluetooth
sudo systemctl start bluetooth

# ── 2. install proxy ────────────────────────────────────────────────────────
echo "==> Installing proxy to $INSTALL_DIR…"
sudo mkdir -p "$INSTALL_DIR"
sudo cp proxy.py "$INSTALL_DIR/"
sudo cp requirements.txt "$INSTALL_DIR/"

echo "==> Creating Python virtual environment…"
sudo python3 -m venv "$VENV"
sudo "$VENV/bin/pip" install --quiet --upgrade pip
sudo "$VENV/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

# ── 3. systemd service ──────────────────────────────────────────────────────
echo "==> Installing systemd service…"
sudo cp "$SERVICE_NAME.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

echo ""
echo "Done.  Edit /etc/systemd/system/$SERVICE_NAME.service to set your"
echo "BLE target (MAC address or device name), then start the service:"
echo ""
echo "  sudo systemctl start $SERVICE_NAME"
echo "  sudo journalctl -u $SERVICE_NAME -f"
