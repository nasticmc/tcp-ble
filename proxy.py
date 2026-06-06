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
import signal
import struct
import threading
import time
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

# MeshCore wire format.
#
# On BLE the payload is unframed: each ATT write/notify carries exactly one
# logical frame (firmware: src/helpers/{esp32,nrf52}/SerialBLEInterface.cpp).
# On the companion's TCP/serial side the same payload is wrapped in a 3-byte
# header (firmware: src/helpers/ArduinoSerialInterface.cpp; client:
# meshcore.js src/connection/tcp_connection.js):
#
#     [type:1B] [length:LE16] [payload...]
#         0x3C '<'  app  -> radio   (phone → companion direction)
#         0x3E '>'  radio -> app    (companion → phone direction)
#
# The proxy must translate between the two — a naïve byte copy desyncs the
# companion's frame parser as soon as a payload byte happens to look like '<'.
MC_MAX_FRAME_SIZE      = 172   # firmware BaseSerialInterface.h MAX_FRAME_SIZE
MC_FRAME_TYPE_TO_RADIO   = 0x3C  # phone -> companion
MC_FRAME_TYPE_FROM_RADIO = 0x3E  # companion -> phone

TCP_RECONNECT_DELAY = 5   # seconds between TCP reconnect attempts
TCP_READ_SIZE       = 512 # bytes per read from companion

BLE_NOTIFY_QUEUE_MAXSIZE = 256   # backpressure cap; drops oldest on overflow
BLE_INTER_NOTIFY_DELAY   = 0.020 # 20 ms — one BLE connection interval

log = logging.getLogger("meshcore-proxy")


# ---------------------------------------------------------------------------
# BlueZ DisplayOnly pairing agent (matches MeshCore firmware IO-cap)
# ---------------------------------------------------------------------------

_AGENT_PATH  = "/com/meshcore/pairingagent"
_AGENT_IFACE = "org.bluez.Agent1"

# Real MeshCore firmware advertises DisplayOnly + MITM + bonded with a static
# passkey (ESP32: ESP_LE_AUTH_REQ_SC_MITM_BOND + setStaticPIN; nRF52:
# setMITM(true) + setIOCaps(true,false,false)). Mimicking that capability is
# what lets phones that previously bonded to a real node bond with the proxy
# without the SMP-MITM mismatch that silently breaks Just-Works.
_CAPABILITY  = "DisplayOnly"

# Set by _run_pairing_agent once the agent is registered (or has failed).
# main() waits on this before starting BLE so the agent is always in place
# before the adapter can receive a bond request.
_agent_ready = threading.Event()


if _DBUS_AVAILABLE:
    class _PairingAgent(dbus.service.Object):
        """DisplayOnly pairing agent with a fixed passkey."""

        def __init__(self, bus, path, pin: str):
            super().__init__(bus, path)
            self._pin     = pin
            self._pin_int = int(pin)

        @dbus.service.method(_AGENT_IFACE)
        def Release(self): pass

        @dbus.service.method(_AGENT_IFACE, in_signature="os")
        def AuthorizeService(self, device, uuid): pass

        @dbus.service.method(_AGENT_IFACE, in_signature="o", out_signature="s")
        def RequestPinCode(self, device):
            log.info("BlueZ requested PIN for %s — supplying %s", device, self._pin)
            return self._pin

        @dbus.service.method(_AGENT_IFACE, in_signature="o", out_signature="u")
        def RequestPasskey(self, device):
            log.info("BlueZ requested passkey for %s — supplying %06d",
                     device, self._pin_int)
            return dbus.UInt32(self._pin_int)

        @dbus.service.method(_AGENT_IFACE, in_signature="ouq")
        def DisplayPasskey(self, device, passkey, entered):
            # LESC: BlueZ generates the passkey itself. If it doesn't match
            # the static PIN, log it loudly so the user can enter the right
            # number on the phone.
            shown = int(passkey)
            if shown != self._pin_int:
                log.warning("Pairing %s: phone must enter PASSKEY %06d "
                            "(BlueZ generated, not the configured static PIN)",
                            device, shown)
            else:
                log.info("Pairing %s: phone must enter PASSKEY %06d",
                         device, shown)

        @dbus.service.method(_AGENT_IFACE, in_signature="os")
        def DisplayPinCode(self, device, pincode):
            log.info("Pairing %s: phone must enter PIN %s", device, pincode)

        @dbus.service.method(_AGENT_IFACE, in_signature="ou")
        def RequestConfirmation(self, device, passkey): pass

        @dbus.service.method(_AGENT_IFACE, in_signature="o")
        def RequestAuthorization(self, device): pass

        @dbus.service.method(_AGENT_IFACE)
        def Cancel(self): pass


def _remove_stale_bonds(bus: "dbus.SystemBus") -> None:
    """Remove all bonded/paired device records from BlueZ.

    When the Pi restarts it loses its LTK, but the phone still tries to
    connect with the old key.  SMP rejects it after ~0.5 s before the
    pairing agent is ever consulted.  Wiping the records forces a clean
    Just-Works pairing on the next connection attempt.
    """
    try:
        om = dbus.Interface(
            bus.get_object("org.bluez", "/"),
            "org.freedesktop.DBus.ObjectManager",
        )
        adapter = dbus.Interface(
            bus.get_object("org.bluez", "/org/bluez/hci0"),
            "org.bluez.Adapter1",
        )
        n = 0
        for path, ifaces in om.GetManagedObjects().items():
            if "org.bluez.Device1" not in ifaces:
                continue
            props = ifaces["org.bluez.Device1"]
            if props.get("Paired") or props.get("Bonded"):
                try:
                    adapter.RemoveDevice(path)
                    n += 1
                    log.debug("Removed bonded device %s (%s)",
                              props.get("Address", "?"), path)
                except Exception as e:
                    log.debug("RemoveDevice %s: %s", path, e)
        if n:
            log.info("Cleared %d stale bond(s) — phone will re-pair on next connect", n)
    except Exception as exc:
        log.warning("Bond cleanup failed: %s", exc)


def _run_pairing_agent(pin: str) -> None:
    """Register a DisplayOnly BlueZ agent and run its GLib event loop.

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
            # Power-cycle the adapter to flush any stale GATT application or
            # advertisement registration left over from a previous crash.  BlueZ
            # normally removes these when the owning D-Bus connection closes, but
            # on some BlueZ versions the state persists and causes
            # RegisterAdvertisement to fail with "Failed to register advertisement"
            # on the next startup.
            try:
                adapter_props.Set("org.bluez.Adapter1", "Powered", dbus.Boolean(False))
                time.sleep(0.5)
                adapter_props.Set("org.bluez.Adapter1", "Powered", dbus.Boolean(True))
                time.sleep(1.5)
                log.info("Adapter power-cycled to clear stale BLE state")
            except Exception as exc:
                log.warning("Adapter power cycle failed: %s", exc)

            adapter_props.Set("org.bluez.Adapter1", "Pairable", dbus.Boolean(True))
            adapter_props.Set("org.bluez.Adapter1", "PairableTimeout", dbus.UInt32(0))
            adapter_props.Set("org.bluez.Adapter1", "Discoverable", dbus.Boolean(True))
            adapter_props.Set("org.bluez.Adapter1", "DiscoverableTimeout", dbus.UInt32(0))
            log.info("Adapter set to always-pairable and always-discoverable")
        except Exception as exc:
            log.warning("Could not configure adapter properties: %s", exc)

        # Wipe stale bond records so mismatched LTKs can't cause SMP failures.
        _remove_stale_bonds(bus)

        agent = _PairingAgent(bus, _AGENT_PATH, pin)
        mgr = dbus.Interface(
            bus.get_object("org.bluez", "/org/bluez"),
            "org.bluez.AgentManager1",
        )
        mgr.RegisterAgent(_AGENT_PATH, _CAPABILITY)
        mgr.RequestDefaultAgent(_AGENT_PATH)
        log.info("BlueZ pairing agent registered (DisplayOnly, static PIN %s)", pin)
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
        # Outbound BLE frames are drained by a single writer task to respect
        # the BLE connection interval and avoid silently dropped notifies.
        self._to_ble: asyncio.Queue[bytes] = asyncio.Queue(maxsize=BLE_NOTIFY_QUEUE_MAXSIZE)
        self._ble_writer_task: asyncio.Task | None = None

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
            # One BLE write == one logical MeshCore frame. Hand off to the
            # asyncio loop without blocking the BLE callback. Must use the
            # stored loop reference — get_event_loop() raises RuntimeError in
            # Python 3.10+ when called from a non-main thread.
            self._loop.call_soon_threadsafe(self._from_ble.put_nowait, bytes(value))

    # ------------------------------------------------------------------
    # BLE → TCP  (wrap each frame in the companion's 3-byte header)
    # ------------------------------------------------------------------

    async def _ble_to_tcp(self, writer: asyncio.StreamWriter) -> None:
        while True:
            payload = await self._from_ble.get()
            if writer.is_closing():
                log.warning("TCP closing, dropping %d-byte frame from phone",
                            len(payload))
                continue
            if len(payload) > MC_MAX_FRAME_SIZE:
                # Firmware silently drops oversize frames; surface it instead.
                log.warning("Dropping oversize BLE frame: %d bytes > %d",
                            len(payload), MC_MAX_FRAME_SIZE)
                continue
            header = struct.pack("<BH", MC_FRAME_TYPE_TO_RADIO, len(payload))
            try:
                writer.write(header + payload)
                await writer.drain()
            except OSError as exc:
                log.warning("TCP write error: %s", exc)
                raise

    # ------------------------------------------------------------------
    # TCP → BLE  (parse 0x3E framed stream, notify one frame per write)
    # ------------------------------------------------------------------

    async def _tcp_to_ble(self, reader: asyncio.StreamReader) -> None:
        buf = bytearray()
        while True:
            chunk = await reader.read(TCP_READ_SIZE)
            if not chunk:
                raise EOFError("Companion closed connection")
            buf.extend(chunk)

            while True:
                # Resync to a frame start byte. Anything else is stray data
                # that would desync the phone's BLE-side parser.
                while buf and buf[0] != MC_FRAME_TYPE_FROM_RADIO:
                    log.warning("TCP resync: dropping stray byte 0x%02x", buf[0])
                    del buf[0]
                if len(buf) < 3:
                    break  # need at least type + length

                length = buf[1] | (buf[2] << 8)
                if length > MC_MAX_FRAME_SIZE:
                    # Almost certainly a bogus length from a desync. Skip the
                    # type byte and rescan from the next position.
                    log.warning("TCP frame length %d > MAX_FRAME_SIZE (%d); "
                                "resyncing", length, MC_MAX_FRAME_SIZE)
                    del buf[0]
                    continue
                if len(buf) < 3 + length:
                    break  # incomplete frame, wait for more

                payload = bytes(buf[3:3 + length])
                del buf[:3 + length]
                self._enqueue_ble_notify(payload)

    def _enqueue_ble_notify(self, data: bytes) -> None:
        try:
            self._to_ble.put_nowait(data)
        except asyncio.QueueFull:
            log.warning("BLE notify queue full; dropping %d-byte frame", len(data))

    async def _ble_writer_loop(self) -> None:
        while True:
            data = await self._to_ble.get()
            await self._ble_notify(data)
            if not self._to_ble.empty():
                await asyncio.sleep(BLE_INTER_NOTIFY_DELAY)

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
            self._ble_writer_task = asyncio.create_task(self._ble_writer_loop())

            # Forward the frame that triggered the connection immediately,
            # wrapping it in the companion's 3-byte header like _ble_to_tcp.
            if len(first) > MC_MAX_FRAME_SIZE:
                log.warning("Dropping oversize first BLE frame: %d bytes > %d",
                            len(first), MC_MAX_FRAME_SIZE)
                first = b""
            try:
                if first:
                    writer.write(
                        struct.pack("<BH", MC_FRAME_TYPE_TO_RADIO, len(first))
                        + first
                    )
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
                if self._ble_writer_task:
                    self._ble_writer_task.cancel()
                # Await cancellation before closing the writer so neither task
                # is mid-drain when the writer is torn down.
                await asyncio.gather(t_read, t_write, return_exceptions=True)
                if self._ble_writer_task:
                    await asyncio.gather(self._ble_writer_task, return_exceptions=True)
                    self._ble_writer_task = None
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

            # Flush queued frames that accumulated while the session was ending
            # so they don't arrive out-of-context at the start of the next session.
            n = 0
            for q in (self._from_ble, self._to_ble):
                while not q.empty():
                    try:
                        q.get_nowait()
                        n += 1
                    except asyncio.QueueEmpty:
                        break
            if n:
                log.debug("Dropped %d stale frame(s) after session end", n)

            log.info("TCP session ended — will reconnect when BLE client sends data")

    # ------------------------------------------------------------------
    # BLE setup
    # ------------------------------------------------------------------

    async def _build_ble_server(self) -> BlessServer:
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
        return server

    async def _cleanup_failed_ble_start(self, server: BlessServer) -> None:
        """Undo partial Bless/BlueZ registration after a failed start attempt.

        Bless exports the root GATT application before registering the
        advertisement.  If advertisement registration fails, retrying
        ``server.start()`` on the same instance tries to export the root
        ``org.bluez`` interface again and dbus-next raises the duplicate-export
        error seen in systemd logs.  Clean up best-effort so the next retry can
        start from a fresh D-Bus connection.
        """
        try:
            await server.setup_task
        except Exception as exc:
            log.debug("BLE setup did not finish before cleanup: %s", exc)
            return

        app = getattr(server, "app", None)
        adapter = getattr(server, "adapter", None)
        bus = getattr(server, "bus", None)
        if app is None or adapter is None or bus is None:
            return

        for advertisement in list(getattr(app, "advertisements", [])):
            try:
                iface = adapter.get_interface("org.bluez.LEAdvertisingManager1")
                await iface.call_unregister_advertisement(advertisement.path)
            except Exception as exc:
                log.debug("BLE failed-start advertisement unregister skipped: %s", exc)
            try:
                bus.unexport(advertisement.path)
            except Exception as exc:
                log.debug("BLE failed-start advertisement unexport skipped: %s", exc)
            try:
                app.advertisements.remove(advertisement)
            except ValueError:
                pass

        try:
            await app.unregister(adapter)
        except Exception as exc:
            log.debug("BLE failed-start application unregister skipped: %s", exc)
        try:
            bus.unexport(app.path, app)
        except Exception as exc:
            log.debug("BLE failed-start application unexport skipped: %s", exc)
        try:
            bus.disconnect()
        except Exception as exc:
            log.debug("BLE failed-start D-Bus disconnect skipped: %s", exc)

    async def _setup_ble(self) -> BlessServer:
        for attempt in range(1, 4):
            server = await self._build_ble_server()
            try:
                await server.start()
                log.info("BLE advertising as %r", self.ble_name)
                return server
            except Exception as exc:
                await self._cleanup_failed_ble_start(server)
                if attempt >= 3:
                    raise
                log.warning("BLE start attempt %d/3 failed (%s) — retrying in 3 s",
                            attempt, exc)
                await asyncio.sleep(3)

        raise RuntimeError("unreachable BLE start retry state")

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def _on_asyncio_error(self, loop: asyncio.AbstractEventLoop,
                          context: dict) -> None:
        exc = context.get("exception")
        if exc is not None:
            log.error("Unhandled asyncio error [%s]: %s", type(exc).__name__, exc)
        else:
            log.error("Asyncio error: %s", context.get("message", "(no message)"))

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        # Prevent exceptions raised inside bless's internal asyncio callbacks
        # from reaching the default handler, which stops the loop.
        self._loop.set_exception_handler(self._on_asyncio_error)
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
        "--ble-pin", default="123456",
        help="Static 6-digit passkey for MITM-bonded pairing. The phone will "
             "be prompted to enter this number on first connect.",
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

    if not (args.ble_pin.isdigit() and len(args.ble_pin) == 6):
        raise SystemExit(
            f"--ble-pin must be exactly 6 digits, got {args.ble_pin!r}"
        )

    # Make SIGTERM (systemd stop/restart) behave like Ctrl-C so the finally
    # block in Proxy.run() always calls server.stop() and unregisters the BLE
    # advertisement.  Without this, a systemd-initiated stop bypasses cleanup
    # and leaves BlueZ with a stale advertisement that blocks the next startup.
    signal.signal(signal.SIGTERM, signal.default_int_handler)

    if _DBUS_AVAILABLE:
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    threading.Thread(
        target=_run_pairing_agent, args=(args.ble_pin,),
        daemon=True, name="bluez-agent",
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
