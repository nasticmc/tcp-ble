# MeshCore TCP-to-BLE Proxy

Bridges a **MeshCore companion app** (TCP) to a **MeshCore node** over
Bluetooth Low Energy using the Nordic UART Service (NUS).  
Designed to run on a **Raspberry Pi Zero 2W**.

```
Companion app
  (Android / PC)
        │  TCP :4403
        ▼
  ┌─────────────┐
  │  RPi Zero   │  proxy.py
  │  2W         │──────────── BLE (NUS) ──── MeshCore node
  └─────────────┘
```

## Requirements

- Raspberry Pi Zero 2W running Raspberry Pi OS Bookworm (64-bit recommended)
- Python 3.11+
- `bluez` (installed by `install.sh`)

## Quick start

### 1. Find your MeshCore node's BLE address

```bash
python3 proxy.py scan
```

Look for a device named something like `MeshCore` or `MC-XXXX`.

### 2. Run the proxy

```bash
# by MAC address (most reliable)
python3 proxy.py run AA:BB:CC:DD:EE:FF

# or by name substring
python3 proxy.py run MeshCore

# custom TCP port
python3 proxy.py run AA:BB:CC:DD:EE:FF --tcp-port 5000
```

The proxy listens on **TCP port 4403** by default.  
Point your MeshCore companion app at the Pi's IP address, port 4403.

### 3. Install as a system service (auto-start on boot)

```bash
chmod +x install.sh
./install.sh

# edit the service to set your BLE target
sudo nano /etc/systemd/system/meshcore-ble-proxy.service
# change:  Environment=BLE_TARGET=MeshCore
# to:      Environment=BLE_TARGET=AA:BB:CC:DD:EE:FF

sudo systemctl start meshcore-ble-proxy
sudo journalctl -u meshcore-ble-proxy -f
```

## CLI reference

```
proxy.py run <target> [options]

  target              BLE MAC address or device name substring
  --tcp-host ADDR     TCP listen address  (default: 0.0.0.0)
  --tcp-port PORT     TCP listen port     (default: 4403)
  --scan-timeout N    BLE scan timeout in seconds (default: 15)
  --log-level LEVEL   DEBUG / INFO / WARNING / ERROR (default: INFO)

proxy.py scan [--timeout N]
  List nearby BLE devices and exit.
```

## How it works

| Direction | Transport | Detail |
|-----------|-----------|--------|
| App → Node | TCP read → BLE write | Written to the NUS **RX** characteristic (write-without-response), chunked to the negotiated ATT MTU |
| Node → App | BLE notify → TCP write | Notifications on the NUS **TX** characteristic are forwarded to all connected TCP clients |

The proxy reconnects to the BLE device automatically whenever the
connection drops (e.g. node reboot).  Multiple TCP clients can connect
simultaneously; all receive the same incoming BLE data.

## NUS UUIDs

| Role    | UUID |
|---------|------|
| Service | `6E400001-B5A3-F393-E0A9-E50E24DCCA9E` |
| RX (write → node) | `6E400002-B5A3-F393-E0A9-E50E24DCCA9E` |
| TX (notify ← node) | `6E400003-B5A3-F393-E0A9-E50E24DCCA9E` |
