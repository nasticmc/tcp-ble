#!/usr/bin/env python3
"""
MeshCore BLE-peripheral-to-TCP proxy.

The Pi advertises a Nordic UART Service (NUS) over BLE.  Your phone's
MeshCore app connects to the Pi as if it were a MeshCore node.  The Pi
forwards that traffic to a locally-running MeshCore companion over TCP.

Data flow
---------
Phone (MeshCore app)
  └─ BLE write  ──► Pi ──► TCP write ──► MeshCore companion (localhost:5000)
  └─ BLE notify ◄── Pi ◄── TCP read  ◄── MeshCore companion (localhost:5000)

NUS UUIDs
---------
Service : 6E400001-B5A3-F393-E0A9-E50E24DCCA9E
RX char : 6E400002-B5A3-F393-E0A9-E50E24DCCA9E  (phone writes → companion)
TX char : 6E400003-B5A3-F393-E0A9-E50E24DCCA9E  (companion → phone notify)
"""

import argparse
import asyncio
import logging
import threading
from typing import Any

try:
    import dbus
    import dbus.service
    import dbus.mainloop.glib
    from gi.repository import GLib as _GLib
    _DBUS_AVAILABLE = True
except ImportError:
    _DBUS_AVAILABLE = False

from bless import (
    BlessGATTCharacteristic,
    BlessServer,
    GATTAttributePermissions,
    GATTCharacteristicProperties,
)

NUS_SERVICE_UUID  = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # phone → companion
NUS_TX_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # companion → phone

TCP_RECONNECT_DELAY = 5   # seconds between TCP reconnect attempts
TCP_READ_SIZE       = 512 # bytes per read from companion

log = logging.getLogger("meshcore-proxy")


# ---------------------------------------------------------------------------
# BlueZ Just-Works pairing agent
# ---------------------------------------------------------------------------

_AGENT_PATH  = "/com/meshcore/pairingagent"
_AGENT_IFACE = "org.bluez.Agent1"
_CAPABILITY  = "NoInputNoOutput"

# Set by _run_pairing_agent once the agent is registered (or has failed).
# main() waits on this before starting BLE so the agent is always in place
# before the adapter can receive a bond request.
_agent_ready = threading.Event()


if _DBUS_AVAILABLE:
    class _PairingAgent(dbus.service.Object):
        """Auto-accepts all BLE bond requests (Just-Works pairing, no PIN)."""

        @dbus.service.method(_AGENT_IFACE)
        def Release(self): pass

        @dbus.service.method(_AGENT_IFACE, in_signature="os")
        def AuthorizeService(self, device, uuid): pass

        @dbus.service.method(_AGENT_IFACE, in_signature="o", out_signature="s")
        def RequestPinCode(self, device): return "0000"

        @dbus.service.method(_AGENT_IFACE, in_signature="o", out_signature="u")
        def RequestPasskey(self, device): return dbus.UInt32(0)

        @dbus.service.method(_AGENT_IFACE, in_signature="ouq")
        def DisplayPasskey(self, device, passkey, entered): pass

        @dbus.service.method(_AGENT_IFACE, in_signature="os")
        def DisplayPinCode(self, device, pincode): pass

        @dbus.service.method(_AGENT_IFACE, in_signature="ou")
        def RequestConfirmation(self, device, passkey): pass

        @dbus.service.method(_AGENT_IFACE, in_signature="o")
        def RequestAuthorization(self, device): pass

        @dbus.service.method(_AGENT_IFACE)
        def Cancel(self): pass


def _run_pairing_agent() -> None:
    """Register a Just-Works BlueZ agent and run its GLib event loop.

    Called in a daemon thread so it doesn't block the asyncio loop.
    """
    if not _DBUS_AVAILABLE:
        log.warning("python3-dbus/python3-gi not available — BLE bond requests will fail")
        _agent_ready.set()
        return
    try:
        bus = dbus.SystemBus()

        # Keep the adapter pairable indefinitely.  BlueZ's default PairableTimeout
        # (180 s) causes the adapter to stop accepting bond requests silently, which
        # makes Android's pairing dialog flash and disappear with BmBondStateEnum.none.
        try:
            adapter_props = dbus.Interface(
                bus.get_object("org.bluez", "/org/bluez/hci0"),
                "org.freedesktop.DBus.Properties",
            )
            adapter_props.Set("org.bluez.Adapter1", "Pairable", dbus.Boolean(True))
            adapter_props.Set("org.bluez.Adapter1", "PairableTimeout", dbus.UInt32(0))
            log.info("Adapter set to always-pairable")
        except Exception as exc:
            log.warning("Could not set adapter pairable: %s", exc)

        agent = _PairingAgent(bus, _AGENT_PATH)
        mgr = dbus.Interface(
            bus.get_object("org.bluez", "/org/bluez"),
            "org.bluez.AgentManager1",
        )
        mgr.RegisterAgent(_AGENT_PATH, _CAPABILITY)
        mgr.RequestDefaultAgent(_AGENT_PATH)
        log.info("BlueZ pairing agent registered (Just-Works, no PIN required)")
        _agent_ready.set()
        _GLib.MainLoop().run()
    except Exception as exc:
        log.warning("BlueZ pairing agent failed: %s", exc)
        _agent_ready.set()


# ---------------------------------------------------------------------------
# Proxy
# ---------------------------------------------------------------------------

class Proxy:
    def __init__(self, tcp_host: str, tcp_port: int, ble_name: str):
        self.tcp_host = tcp_host
        self.tcp_port = tcp_port
        self.ble_name = ble_name

        self._server: BlessServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # BLE writes arrive in a sync callback; queue them for the async loop.
        self._from_ble: asyncio.Queue[bytes] = asyncio.Queue()

    # ------------------------------------------------------------------
    # BLE callbacks (called synchronously by bless)
    # ------------------------------------------------------------------

    def _on_read(self, characteristic: BlessGATTCharacteristic,
                 **_kwargs) -> bytearray:
        return characteristic.value or bytearray()

    def _on_write(self, characteristic: BlessGATTCharacteristic,
                  value: Any, **_kwargs) -> None:
        characteristic.value = value
        if value and self._loop is not None:
            # Hand off to the asyncio loop without blocking the BLE callback.
            # Must use the stored loop reference — get_event_loop() raises
            # RuntimeError in Python 3.10+ when called from a non-main thread.
            self._loop.call_soon_threadsafe(self._from_ble.put_nowait, bytes(value))

    # ------------------------------------------------------------------
    # BLE → TCP  (drain the queue)
    # ------------------------------------------------------------------

    async def _ble_to_tcp(self, writer: asyncio.StreamWriter) -> None:
        while True:
            data = await self._from_ble.get()
            if writer.is_closing():
                log.warning("TCP closing, dropping %d bytes from phone", len(data))
                continue
            try:
                writer.write(data)
                await writer.drain()
            except OSError as exc:
                log.warning("TCP write error: %s", exc)
                raise

    # ------------------------------------------------------------------
    # TCP → BLE  (read companion, notify phone)
    # ------------------------------------------------------------------

    async def _tcp_to_ble(self, reader: asyncio.StreamReader) -> None:
        while True:
            data = await reader.read(TCP_READ_SIZE)
            if not data:
                raise EOFError("Companion closed connection")
            await self._ble_notify(data)

    async def _ble_notify(self, data: bytes) -> None:
        if self._server is None:
            return
        char = self._server.get_characteristic(NUS_TX_CHAR_UUID)
        if char is None:
            return
        char.value = bytearray(data)
        self._server.update_value(NUS_SERVICE_UUID, NUS_TX_CHAR_UUID)

    # ------------------------------------------------------------------
    # TCP connection loop
    # ------------------------------------------------------------------

    async def _tcp_loop(self) -> None:
        while True:
            # Wait for the first BLE packet before opening a TCP connection.
            # Connecting eagerly leaves the companion with an idle session it
            # will close on a short timeout (causing rapid reconnect loops).
            log.info("Waiting for BLE client before connecting to companion…")
            first = await self._from_ble.get()

            # Connect, retrying on transient failures.
            while True:
                try:
                    log.info("Connecting to companion %s:%d…",
                             self.tcp_host, self.tcp_port)
                    reader, writer = await asyncio.open_connection(
                        self.tcp_host, self.tcp_port
                    )
                    break
                except (ConnectionRefusedError, OSError) as exc:
                    log.warning("TCP connect failed: %s — retry in %d s",
                                exc, TCP_RECONNECT_DELAY)
                    await asyncio.sleep(TCP_RECONNECT_DELAY)

            log.info("TCP connected to companion %s:%d", self.tcp_host, self.tcp_port)

            # Forward the packet that triggered the connection immediately.
            try:
                writer.write(first)
                await writer.drain()
            except OSError as exc:
                log.warning("TCP write error on first packet: %s", exc)
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
                continue

            t_read  = asyncio.create_task(self._tcp_to_ble(reader))
            t_write = asyncio.create_task(self._ble_to_tcp(writer))
            try:
                _done, pending = await asyncio.wait(
                    [t_read, t_write],
                    return_when=asyncio.FIRST_EXCEPTION,
                )
                for t in _done:
                    if not t.cancelled() and t.exception():
                        log.warning("TCP session ended: %s", t.exception())
            finally:
                t_read.cancel()
                t_write.cancel()
                # Await cancellation before closing the writer so neither task
                # is mid-drain when the writer is torn down.
                await asyncio.gather(t_read, t_write, return_exceptions=True)
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

            # Flush BLE data that queued up while the session was ending so
            # it doesn't arrive out-of-context at the start of the next session.
            n = 0
            while not self._from_ble.empty():
                try:
                    self._from_ble.get_nowait()
                    n += 1
                except asyncio.QueueEmpty:
                    break
            if n:
                log.debug("Dropped %d stale BLE packet(s) after session end", n)

            log.info("TCP session ended — will reconnect when BLE client sends data")

    # ------------------------------------------------------------------
    # BLE setup
    # ------------------------------------------------------------------

    async def _setup_ble(self) -> BlessServer:
        server = BlessServer(name=self.ble_name)
        server.read_request_func  = self._on_read
        server.write_request_func = self._on_write

        await server.add_new_service(NUS_SERVICE_UUID)

        # RX — phone writes data to the companion
        await server.add_new_characteristic(
            NUS_SERVICE_UUID,
            NUS_RX_CHAR_UUID,
            (GATTCharacteristicProperties.write |
             GATTCharacteristicProperties.write_without_response),
            None,
            GATTAttributePermissions.writeable,
        )

        # TX — companion data is pushed to the phone via notify
        await server.add_new_characteristic(
            NUS_SERVICE_UUID,
            NUS_TX_CHAR_UUID,
            GATTCharacteristicProperties.notify,
            None,
            GATTAttributePermissions.readable,
        )

        await server.start()
        log.info("BLE advertising as %r", self.ble_name)
        return server

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._server = await self._setup_ble()
        try:
            await self._tcp_loop()
        finally:
            await self._server.stop()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="MeshCore BLE peripheral → TCP proxy",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--tcp-host", default="127.0.0.1",
        help="MeshCore companion TCP host",
    )
    p.add_argument(
        "--tcp-port", type=int, default=5000,
        help="MeshCore companion TCP port",
    )
    p.add_argument(
        "--ble-name", default="MeshCore",
        help="BLE advertised name (what the phone sees)",
    )
    p.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    if _DBUS_AVAILABLE:
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    threading.Thread(
        target=_run_pairing_agent, daemon=True, name="bluez-agent"
    ).start()
    _agent_ready.wait(timeout=5.0)

    proxy = Proxy(
        tcp_host=args.tcp_host,
        tcp_port=args.tcp_port,
        ble_name=args.ble_name,
    )
    try:
        asyncio.run(proxy.run())
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()
