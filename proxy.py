#!/usr/bin/env python3
"""
MeshCore TCP-to-BLE proxy.

Listens for a MeshCore companion app on a TCP port and forwards the traffic
to a MeshCore node over Bluetooth Low Energy using the Nordic UART Service
(NUS).  Designed for Raspberry Pi Zero 2W.

NUS UUIDs
---------
Service : 6E400001-B5A3-F393-E0A9-E50E24DCCA9E
RX char : 6E400002-B5A3-F393-E0A9-E50E24DCCA9E  (write → node)
TX char : 6E400003-B5A3-F393-E0A9-E50E24DCCA9E  (notify ← node)
"""

import argparse
import asyncio
import logging
import sys

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

BLE_RECONNECT_DELAY = 5   # seconds between reconnect attempts
TCP_READ_SIZE       = 512 # bytes per TCP read

log = logging.getLogger("meshcore-proxy")


# ---------------------------------------------------------------------------
# Proxy
# ---------------------------------------------------------------------------

class Proxy:
    def __init__(self, ble_target: str, tcp_host: str, tcp_port: int,
                 scan_timeout: float):
        self.ble_target   = ble_target
        self.tcp_host     = tcp_host
        self.tcp_port     = tcp_port
        self.scan_timeout = scan_timeout

        self._ble_client: BleakClient | None = None
        self._tcp_writers: set[asyncio.StreamWriter] = set()
        # Set when BLE is connected and notifications are running.
        self._ble_ready = asyncio.Event()

    # ------------------------------------------------------------------
    # BLE → TCP  (notification callback, called from bleak's thread)
    # ------------------------------------------------------------------

    def _on_ble_notify(self, _handle, data: bytearray) -> None:
        writers = list(self._tcp_writers)
        if not writers:
            return
        loop = asyncio.get_event_loop()
        loop.call_soon_threadsafe(self._forward_to_tcp, bytes(data), writers)

    def _forward_to_tcp(self, data: bytes,
                        writers: list[asyncio.StreamWriter]) -> None:
        dead: set[asyncio.StreamWriter] = set()
        for w in writers:
            try:
                w.write(data)
            except Exception:
                dead.add(w)
        self._tcp_writers -= dead

    # ------------------------------------------------------------------
    # BLE connection management
    # ------------------------------------------------------------------

    async def _find_address(self) -> str:
        """Return a MAC/address string for the target device."""
        t = self.ble_target
        # Looks like a MAC address already
        if len(t) == 17 and t.count(":") == 5:
            return t
        log.info("Scanning for BLE device matching %r (%.0f s)…",
                 t, self.scan_timeout)
        device = await BleakScanner.find_device_by_filter(
            lambda d, _adv: t.lower() in (d.name or "").lower(),
            timeout=self.scan_timeout,
        )
        if device is None:
            raise RuntimeError(
                f"No BLE device found matching {t!r} within "
                f"{self.scan_timeout} s"
            )
        log.info("Found: %s  [%s]", device.name, device.address)
        return device.address

    async def _ble_loop(self) -> None:
        """Maintain the BLE connection, reconnecting automatically."""
        while True:
            self._ble_ready.clear()
            disconnect_event = asyncio.Event()

            try:
                address = await self._find_address()

                def on_disconnect(_client: BleakClient) -> None:
                    log.warning("BLE disconnected")
                    disconnect_event.set()
                    self._ble_ready.clear()

                async with BleakClient(
                    address,
                    disconnected_callback=on_disconnect,
                ) as client:
                    await client.start_notify(NUS_TX_CHAR_UUID,
                                              self._on_ble_notify)
                    self._ble_client = client
                    mtu = getattr(client, "mtu_size", 23)
                    log.info(
                        "BLE connected  address=%s  MTU=%d  write_chunk=%d",
                        address, mtu, mtu - 3,
                    )
                    self._ble_ready.set()
                    await disconnect_event.wait()

            except (BleakError, RuntimeError, asyncio.TimeoutError,
                    OSError) as exc:
                log.warning("BLE error: %s — retry in %d s",
                            exc, BLE_RECONNECT_DELAY)
            except Exception as exc:
                log.exception("Unexpected BLE error: %s", exc)
            finally:
                self._ble_client = None
                self._ble_ready.clear()

            await asyncio.sleep(BLE_RECONNECT_DELAY)

    # ------------------------------------------------------------------
    # TCP → BLE
    # ------------------------------------------------------------------

    async def _handle_tcp_client(self, reader: asyncio.StreamReader,
                                  writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        log.info("TCP client connected: %s", peer)
        self._tcp_writers.add(writer)
        try:
            while True:
                data = await reader.read(TCP_READ_SIZE)
                if not data:
                    break
                # Wait (and buffer one chunk) if BLE is reconnecting.
                await self._ble_ready.wait()
                client = self._ble_client
                if client is None or not client.is_connected:
                    log.warning("BLE gone, dropping %d bytes", len(data))
                    continue
                mtu_payload = (getattr(client, "mtu_size", 23) - 3)
                try:
                    for offset in range(0, len(data), mtu_payload):
                        await client.write_gatt_char(
                            NUS_RX_CHAR_UUID,
                            data[offset : offset + mtu_payload],
                            response=False,
                        )
                except BleakError as exc:
                    log.warning("BLE write error: %s", exc)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.warning("TCP client %s error: %s", peer, exc)
        finally:
            self._tcp_writers.discard(writer)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            log.info("TCP client disconnected: %s", peer)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        server = await asyncio.start_server(
            self._handle_tcp_client, self.tcp_host, self.tcp_port,
        )
        addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
        log.info("TCP server listening on %s", addrs)
        async with server:
            await asyncio.gather(
                server.serve_forever(),
                self._ble_loop(),
            )


# ---------------------------------------------------------------------------
# Scan helper
# ---------------------------------------------------------------------------

async def scan(timeout: float) -> None:
    print(f"Scanning {timeout:.0f} s for BLE devices …\n")
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    if not devices:
        print("No devices found.")
        return
    print(f"{'ADDRESS':<20} {'RSSI':>5}  NAME")
    print("-" * 60)
    for addr, (device, adv) in sorted(
        devices.items(), key=lambda x: -(x[1][1].rssi or -999)
    ):
        print(f"{addr:<20} {adv.rssi or '?':>5}  {device.name or '(unknown)'}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="MeshCore TCP-to-BLE proxy (NUS bridge)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd")

    run_p = sub.add_parser("run", help="Start the proxy")
    run_p.add_argument(
        "target",
        help="BLE device MAC address (AA:BB:CC:DD:EE:FF) or name substring",
    )
    run_p.add_argument(
        "--tcp-host", default="0.0.0.0",
        help="TCP listen address",
    )
    run_p.add_argument(
        "--tcp-port", type=int, default=4403,
        help="TCP listen port",
    )
    run_p.add_argument(
        "--scan-timeout", type=float, default=15.0,
        help="BLE scan timeout in seconds (name-based discovery only)",
    )
    run_p.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    scan_p = sub.add_parser("scan", help="List nearby BLE devices and exit")
    scan_p.add_argument(
        "--timeout", type=float, default=10.0,
        help="Scan duration in seconds",
    )

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.cmd == "scan":
        asyncio.run(scan(args.timeout))
        return

    if args.cmd != "run":
        parser.print_help()
        sys.exit(1)

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    proxy = Proxy(
        ble_target=args.target,
        tcp_host=args.tcp_host,
        tcp_port=args.tcp_port,
        scan_timeout=args.scan_timeout,
    )

    try:
        asyncio.run(proxy.run())
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()
