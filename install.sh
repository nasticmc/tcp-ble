#!/usr/bin/env bash
# Install the MeshCore BLE-peripheral-to-TCP proxy on Raspberry Pi Zero 2W (Bookworm).
set -euo pipefail

INSTALL_DIR="/opt/meshcore-ble-proxy"
SERVICE_NAME="meshcore-ble-proxy"
VENV="$INSTALL_DIR/venv"

# ── 1. system dependencies ──────────────────────────────────────────────────
echo "==> Installing system packages…"
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip \
    bluez libglib2.0-dev libdbus-1-dev \
    python3-dbus python3-gi gir1.2-glib-2.0

# bless uses BlueZ's experimental D-Bus GATT server API; enable it.
BTOPTS_FILE="/etc/bluetooth/main.conf"
if ! grep -q "^ExperimentalFeatures" "$BTOPTS_FILE" 2>/dev/null; then
    echo "==> Enabling BlueZ experimental features…"
    sudo sed -i 's/^#ExperimentalFeatures/ExperimentalFeatures/' "$BTOPTS_FILE" || \
        echo -e "\n[Policy]\nExperimentalFeatures=true" | sudo tee -a "$BTOPTS_FILE" > /dev/null
fi

sudo systemctl enable bluetooth
sudo systemctl restart bluetooth

# ── 2. install proxy ────────────────────────────────────────────────────────
echo "==> Installing proxy to $INSTALL_DIR…"
sudo mkdir -p "$INSTALL_DIR"
sudo cp proxy.py "$INSTALL_DIR/"
sudo cp requirements.txt "$INSTALL_DIR/"

echo "==> Creating Python virtual environment…"
# Recreate venv if it exists without system-site-packages (needed for python3-gi).
if [ -d "$VENV" ] && ! grep -q "include-system-site-packages = true" "$VENV/pyvenv.cfg" 2>/dev/null; then
    echo "    Recreating venv with --system-site-packages…"
    sudo rm -rf "$VENV"
fi
sudo python3 -m venv --system-site-packages "$VENV"
sudo "$VENV/bin/pip" install --quiet --upgrade pip
sudo "$VENV/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

# ── 3. systemd service ──────────────────────────────────────────────────────
echo "==> Installing systemd service…"
sudo cp "$SERVICE_NAME.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

echo ""
echo "Done.  The proxy connects to localhost:5000 by default."
echo "Start the service with:"
echo ""
echo "  sudo systemctl start $SERVICE_NAME"
echo "  sudo journalctl -u $SERVICE_NAME -f"
echo ""
echo "To change the TCP port or BLE name, edit:"
echo "  /etc/systemd/system/$SERVICE_NAME.service"
echo "then: sudo systemctl daemon-reload && sudo systemctl restart $SERVICE_NAME"
