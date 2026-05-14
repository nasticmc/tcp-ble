# MeshCore BLE Peripheral-to-TCP Proxy

Runs on a **Raspberry Pi Zero 2W**.  Advertises a Nordic UART Service (NUS)
over Bluetooth so your **phone's MeshCore app** can connect to the Pi as
if it were a MeshCore node, while the Pi forwards all traffic to a
**locally-running MeshCore companion** over TCP.

```
Phone (MeshCore app)
  │  BLE — phone connects to Pi
  ▼
┌──────────────────┐
│  RPi Zero 2W     │  proxy.py
│  (BLE peripheral)│──── TCP 127.0.0.1:5000 ────► MeshCore companion
└──────────────────┘
```

## Requirements

- Raspberry Pi Zero 2W running Raspberry Pi OS Bookworm
- Python 3.11+
- MeshCore companion already running and listening on TCP (default: `localhost:5000`)

## Quick start

```bash
# install Python dependency
pip install -r requirements.txt

# run the proxy (connects to localhost:5000, advertises as "MeshCore" over BLE)
python3 proxy.py

# custom companion address / BLE name
python3 proxy.py --tcp-port 5000 --ble-name "MyNode"
```

On your phone, open the MeshCore app, scan for BLE devices, and connect to **MeshCore** (or whatever `--ble-name` you chose).

## Install as a system service (auto-start on boot)

```bash
chmod +x install.sh
./install.sh

sudo systemctl start meshcore-ble-proxy
sudo journalctl -u meshcore-ble-proxy -f
```

To change the TCP port or BLE advertised name, edit the service file:

```bash
sudo nano /etc/systemd/system/meshcore-ble-proxy.service
# Edit the ExecStart line, then:
sudo systemctl daemon-reload
sudo systemctl restart meshcore-ble-proxy
```

## CLI reference

```
proxy.py [options]

  --tcp-host HOST    Companion TCP host  (default: 127.0.0.1)
  --tcp-port PORT    Companion TCP port  (default: 5000)
  --ble-name NAME    BLE advertised name (default: MeshCore)
  --ble-pin  PIN     6-digit passkey for MITM pairing (default: 123456)
  --log-level LEVEL  DEBUG / INFO / WARNING / ERROR (default: INFO)
```

## How it works

MeshCore uses **two different wire formats** for the same payload:

| Transport | Framing |
|-----------|---------|
| BLE NUS   | One ATT write/notify = one whole frame, no header. Max 172 bytes. |
| TCP / serial companion | `[type:1B][length:LE16][payload...]` — type `0x3C` for phone→radio, `0x3E` for radio→phone. |

The proxy translates between the two:

| Direction | Path | Translation |
|-----------|------|------------|
| Phone → Companion | BLE write → TCP write | Wrap each BLE payload as `0x3C, len_lo, len_hi, payload` and send to TCP. |
| Companion → Phone | TCP read → BLE notify | Stateful parser: find `0x3E`, read 16-bit LE length, emit each payload as one notify. |

Without this translation the companion's `<`/`>` frame parser desyncs as soon
as a payload byte happens to look like a frame marker, which silently corrupts
the session — usually right at the first `DeviceQuery` exchange.

The proxy reconnects to the companion TCP socket automatically on failure.
BLE writes that arrive while TCP is reconnecting are queued and flushed once
the connection is restored.

## Pairing

The official MeshCore firmware advertises **DisplayOnly + MITM + bonded** with
a static passkey (`ESP_LE_AUTH_REQ_SC_MITM_BOND` on ESP32, `setMITM(true) +
DisplayOnly` on nRF52). The proxy registers a matching BlueZ agent so the
phone sees the same IO-capability profile. On first connect the phone is
prompted to enter the 6-digit PIN from `--ble-pin`.

If BlueZ generates its own LESC passkey instead of using the static one, the
proxy logs the actual number at INFO level — watch `journalctl -u
meshcore-ble-proxy -f` and enter what's printed there.

Stale bond records are wiped at startup so a Pi reboot (which loses its LTK)
doesn't cause the phone's old bond to be rejected silently by SMP.

## BlueZ note

`bless` uses BlueZ's experimental GATT server D-Bus API.  `install.sh`
enables `ExperimentalFeatures` in `/etc/bluetooth/main.conf` automatically.
If you set up manually, make sure that line is uncommented.

## NUS UUIDs

| Role | UUID |
|------|------|
| Service | `6E400001-B5A3-F393-E0A9-E50E24DCCA9E` |
| RX (phone writes) | `6E400002-B5A3-F393-E0A9-E50E24DCCA9E` |
| TX (phone subscribes) | `6E400003-B5A3-F393-E0A9-E50E24DCCA9E` |
