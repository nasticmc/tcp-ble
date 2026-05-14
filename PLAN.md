# Plan: BLE Send Queue with Inter-Notify Pacing

## Problem

When a phone connects and triggers a bulk response (e.g. `CMD_GET_CONTACTS`),
pymc_repeater dumps all frames over TCP as fast as it can.  Our `_tcp_to_ble`
currently fires a BLE notify for every parsed frame immediately via
`server.update_value()`.

BLE has a connection interval (typically 15–50 ms).  The phone's GATT receive
buffer can hold a limited number of pending notifies; if we hammer it faster
than the phone drains it, frames are **silently dropped**.  The result is an
incomplete contact list or missed messages on the phone side.

This is a speed mismatch: TCP delivers data in microseconds; BLE can only
deliver one notify per connection interval.

---

## Solution

Mirror the pattern used by pymc_core's `CompanionFrameServer._writer_loop`:
move all outbound BLE sends through a dedicated `asyncio.Queue`, drained by a
single writer task that inserts a small inter-frame delay whenever the queue
still has frames waiting.

Key behaviours:

| Condition | Behaviour |
|---|---|
| Queue has more frames pending | Send frame, then `await asyncio.sleep(INTER_NOTIFY_DELAY)` |
| Queue just became empty | Send frame, **no delay** (keeps single-frame exchanges snappy) |
| No BLE client connected | Frames are dropped from the queue |

---

## Constants to add

```python
BLE_NOTIFY_QUEUE_MAXSIZE = 256   # backpressure cap; drops oldest on overflow
BLE_INTER_NOTIFY_DELAY   = 0.020 # 20 ms — one BLE connection interval
```

`BLE_NOTIFY_QUEUE_MAXSIZE = 256` gives headroom for a full contact dump
(pymc_core default cap is 1 000 contacts, but dumps are paged in practice).
Frames beyond 256 are dropped with a warning — same strategy pymc_core uses
for its TCP write queue.

`BLE_INTER_NOTIFY_DELAY = 0.020` matches a typical 20 ms BLE connection
interval.  This is the conservative starting value; it can be tuned down if
testing shows the phone handles faster delivery reliably.

---

## Changes to `proxy.py`

### 1. Add the queue and writer task to `Proxy.__init__`

```python
self._to_ble: asyncio.Queue[bytes] = asyncio.Queue(maxsize=BLE_NOTIFY_QUEUE_MAXSIZE)
self._ble_writer_task: asyncio.Task | None = None
```

### 2. New `_ble_writer_loop` method

Replaces direct calls to `server.update_value()` with a single drainer task.

```python
async def _ble_writer_loop(self) -> None:
    while True:
        data = await self._to_ble.get()
        await self._ble_notify(data)
        if not self._to_ble.empty():
            await asyncio.sleep(BLE_INTER_NOTIFY_DELAY)
```

### 3. Update `_ble_notify` to enqueue instead of notify directly

Current `_ble_notify` performs the notify directly.  It becomes the actual
low-level sender (called only by `_ble_writer_loop`).  Add a new
`_enqueue_ble_notify` that the rest of the code calls:

```python
def _enqueue_ble_notify(self, data: bytes) -> None:
    try:
        self._to_ble.put_nowait(data)
    except asyncio.QueueFull:
        log.warning("BLE notify queue full; dropping %d-byte frame", len(data))
```

### 4. Update `_tcp_to_ble` to call `_enqueue_ble_notify`

Replace:
```python
await self._ble_notify(payload)
```
With:
```python
self._enqueue_ble_notify(payload)
```

### 5. Start/stop the writer task in `_tcp_loop`

Start the writer task once when TCP connects and cancel it on disconnect, so it
is only running while a session is active.  This avoids the writer task
accumulating stale frames between sessions (the queue is already drained
between sessions).

```python
# on connect:
self._ble_writer_task = asyncio.create_task(self._ble_writer_loop())

# in finally block on disconnect:
if self._ble_writer_task:
    self._ble_writer_task.cancel()
    await asyncio.gather(self._ble_writer_task, return_exceptions=True)
    self._ble_writer_task = None
```

---

## What does NOT change

- Frame framing/parsing logic (`_ble_to_tcp`, `_tcp_to_ble`) — untouched.
- The BLE write path (phone → proxy → TCP) — untouched.
- Reconnect logic, queue drain on session end — untouched.
- The pairing agent — untouched.

---

## Testing checklist

- [ ] Single command/response (e.g. `CMD_SEND_SELF_ADVERT`) feels instantaneous
      — no artificial delay when queue empties after one frame.
- [ ] Contact sync: full contact list arrives on the phone without gaps after
      reconnect with a populated repeater.
- [ ] Heartbeat frames (`RESP_CODE_CURR_TIME`, sent every 15 s by pymc_repeater)
      are forwarded to the phone without interrupting normal traffic.
- [ ] Overflow path: simulate a burst > 256 frames and confirm the warning is
      logged without crashing.
- [ ] Session teardown: disconnect BLE mid-sync; confirm stale frames are
      drained and the next session starts clean.
