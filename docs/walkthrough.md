# sim-telemetry walkthrough

A study guide for this codebase: what each module does, the ideas behind it, and questions an
interviewer is likely to ask. Read it with the code open.

## 1. The big picture

```
device --bytes--> Transport --bytes--> FrameParser --Frame--> queue --> consumers
                                  (inside the reader thread)          logger / stats / web
```

A byte leaves the device, arrives through a `Transport`, is reassembled into a `Frame` by the
`FrameParser` inside the `TelemetryReader` thread, waits in a bounded `queue.Queue`, and is then
consumed by the logger, the rolling stats, or the dashboard's `LiveFeed`. Each step only knows
about the one before it, so you can swap the real serial port for the simulator, or CSV for
Parquet, without touching the rest.

A good exercise: run `sim-telemetry simulate --count 3` and decode the first hex line by hand
using the table in the README.

## 2. Module by module

### `protocol.py` - the contract between device and host
- Defines the frame with three `struct.Struct` objects (header, payload, CRC). `"<"` means
  little-endian; `h/H` = signed/unsigned 16-bit, `B/b` = unsigned/signed 8-bit, `I` = u32.
- `encode_payload` converts engineering units to fixed-point integers (steering x10, speed
  x100) and clamps them, so a weird value can never raise `struct.error`.
- `crc16_ccitt` is CRC-16/CCITT-FALSE with a 256-entry lookup table. The test checks the
  standard check value `0x29B1` for `b"123456789"`, which is how you know the implementation
  matches every other CRC-16/CCITT-FALSE in the world (including the Arduino sketch).
- `TelemetrySample` (frozen dataclass) is the decoded data; `Frame` adds `seq`, `timestamp_ms`
  and `received_at`.

### `parser.py` - turning a byte stream into frames
`FrameParser.feed(chunk)` appends to a `bytearray` buffer and loops:
1. Find `0xAA 0x55`. Bytes before it are garbage -> `bytes_skipped`. If there is no sync at all,
   keep only the last byte (it might be `0xAA`, the first half of the next sync).
2. Need 9 bytes for the header. If `length > 64` it cannot be real -> skip one byte, retry.
3. Need the whole frame. Compute the CRC over length..payload. Mismatch -> `crc_errors`, skip
   **one** byte, retry (a real frame may start inside what we just rejected).
4. Valid: remove the frame from the buffer, check the sequence number, decode, return.

Sequence tracking: `gap = (seq - last - 1) % 65536`. Modular arithmetic makes wrap-around
(65535 -> 0) look like a normal step. A gap larger than half the range means "went backwards",
which we count as a device reset instead of 60,000 lost packets.

Known trade-off: a false header with a large length makes the parser wait for up to 75 bytes
before it can reject it. On a live 50 Hz link that is a delay of a few frames, never a loss.

### `simulator.py` - believable data without hardware
- `Track` turns a list of `(length, radius)` segments into a curvature array (1/radius) sampled
  every metre, smoothed with a moving average so steering ramps in and out.
- `speed_profile` is a classic lap-time-simulation approach: grip limits corner speed
  (`v = sqrt(a_lat * R)`), a forward pass limits acceleration, a backward pass makes the car brake
  early enough for the next corner.
- `LapSimulator.step(dt)` moves the car along the track and derives throttle/brake from the
  required acceleration, steering from curvature (bicycle model: `atan(wheelbase / R)` times the
  steering ratio), gear from rpm with hysteresis, and rpm from speed and gear ratios.
- `SimulatedDevice` numbers and timestamps frames like firmware would; `apply_link_noise`
  randomly drops, bit-flips or pads frames so you can watch the parser recover.

### `transport.py` - where bytes come from
One abstract method, `read(size) -> bytes`, returning `b""` on timeout.
- `SerialTransport`: `serial.serial_for_url`, so it accepts `COM3`, `/dev/ttyUSB0` and URLs
  such as `loop://`. `read` blocks for at least one byte, then takes whatever else is waiting.
- `LoopbackTransport`: a `loop://` port plus a background thread (`pump_frames`) writing
  simulated frames into it. The bytes really pass through pyserial.
- `SimTransport`: in-process simulator that releases frames according to the wall clock, like
  a UART would. `realtime=False` returns frames immediately for tests.
- `FileTransport`: replays a `.bin` capture; sets `at_eof` and `live = False`.
- `pump_frames` schedules each frame at `start + n * period` instead of sleeping a fixed amount
  after each one, so timing errors do not accumulate (no drift).

### `reader.py` - the background thread
`TelemetryReader` is a `threading.Thread` that loops `read -> parse -> put`. Details worth
knowing:
- It is a daemon thread and has a `stop()` method based on a `threading.Event`.
- Queue full + live source: drop the oldest frame (`queue_overflows += 1`). Queue full + file:
  block until there is space (backpressure).
- Exceptions (e.g. USB cable pulled -> `SerialException`) are stored in `reader.error` instead
  of silently killing the thread.
- `frames(until=...)` is a generator that consumers loop over.

### `stats.py` - rolling statistics
A `deque` of `(received_at, seq_gap, clock_offset)` for the last 5 seconds. Old entries are
popped from the left, so every operation is cheap. `snapshot()` computes packet rate, drop rate
(`dropped / (received + dropped)`), and latency relative to the best frame seen.

### `logger.py` - session files
CSV via `csv.DictWriter`, flushed every 50 rows. Parquet via Pandas/pyarrow, written on close.
The file extension picks the format.

### `analysis.py` - Pandas summary
- Time axis from the device clock: `diff().mod(2**32).cumsum()` handles the u32 wrap.
- Braking events: `braking = brake >= 10`; `(braking != braking.shift()).cumsum()` gives every
  consecutive run its own id; group by id, keep braking runs lasting at least 0.2 s.
- Gear usage: `value_counts(normalize=True)`.

### `dashboard.py` + `static/index.html` - live view
- The FastAPI `lifespan` opens the transport, starts the reader thread and an asyncio task
  (`LiveFeed.run`), and cleans all of them up on shutdown.
- `LiveFeed` bridges thread and event loop: every 100 ms it drains the thread-safe queue without
  blocking (`get_nowait`), updates stats and sends one JSON message to each WebSocket client.
- The page keeps the last 500 points per chart and redraws with `chart.update("none")`
  (no animation). It reconnects automatically if the socket closes.

### `cli.py`
`argparse` sub-commands, each a small `cmd_*` function. `main(argv)` takes an argument list so
tests can call it directly.

## 3. Key concepts

**Framing.** A serial port delivers a stream of bytes with no message boundaries. Framing adds
structure: a recognisable start marker (sync word), a length so you know where the frame ends,
and an integrity check. Alternatives: delimiter-based framing with byte stuffing (SLIP, COBS),
or text lines (`"123,45,...\n"`), which are easy to debug but bigger and slower to parse.

**Checksums vs CRCs.** A sum of bytes misses swapped bytes and many multi-bit errors. A CRC
treats the data as a polynomial and divides by a generator polynomial; the remainder is the
CRC. CRC-16 detects every single-bit error and every burst error up to 16 bits long, and random
corruption slips through with probability about 1 / 65536.

**Threads and queues.** I/O like `serial.read()` releases the GIL while waiting, so a
reader thread runs in parallel with the main thread. `queue.Queue` is thread-safe (it has its own
lock), so no extra locking is needed. A *bounded* queue prevents memory from growing forever if
a consumer is slower than the producer; you then choose a policy: block (backpressure) or drop.

**WebSockets.** HTTP is request/response; a WebSocket starts as HTTP, then "upgrades" into a
long-lived two-way connection, so the server can push data whenever it wants. Here the server
pushes JSON 10 times a second. Alternatives: polling (wasteful) or Server-Sent Events
(one-way, would also work).

**Async + threads together.** FastAPI runs on an asyncio event loop; blocking calls inside it
would freeze every client. The blocking serial read therefore lives in a normal thread, and the
async side only uses non-blocking `get_nowait()` plus `await asyncio.sleep()`.

## 4. Likely interview questions

1. **Why binary instead of sending CSV text over serial?**
   Smaller (20 bytes vs roughly 40 characters for the same values as a CSV line), constant size, no string parsing on the host
   or formatting on the microcontroller. Text is easier to debug; the `simulate --count` preview
   and hex dump make up for that.

2. **How does the parser recover from corruption?**
   It searches for the sync word, validates the length, then the CRC. On any failure it discards
   a single byte and searches again, so a real frame hidden behind a false sync is still found.
   The fuzz tests inject random garbage, fake sync words and bit flips in random chunk sizes and
   check every real frame is recovered.

3. **How do you detect dropped packets, and what about wrap-around?**
   Every frame carries a 16-bit sequence number. `(seq - last - 1) % 65536` is the number of
   frames missing; modular arithmetic makes 65535 -> 0 a gap of zero. A huge "gap" means the
   counter went backwards, which is treated as a device reset.

4. **What happens if the consumer is slower than the device?**
   The bounded queue fills up. For live data the reader drops the oldest frame and counts it in
   `queue_overflows`; it never blocks, because blocking would let the OS serial buffer overflow
   and lose data uncontrollably. For file replay it blocks instead (backpressure).

5. **Why a separate thread for reading? Doesn't the GIL prevent parallelism?**
   The GIL is released during blocking I/O, so the reader can wait on the port while the main
   thread writes files or serves web requests. The CPU work (parsing) is small: the included
   benchmark decodes far more frames per second than a 115200-baud link can deliver.

6. **How do you measure latency when the device and PC clocks are not synchronised?**
   I can't measure true one-way latency. I track `host_time - device_time` per frame; its
   minimum is the best case, and every other frame's excess over that minimum is delay added by
   buffering or scheduling. That is the useful number for a live display. Clock drift would
   slowly bias it; a fix would be to fit a line to the offsets over time.

7. **Why CRC-16 and not a simple checksum or CRC-32?**
   CRC-16 catches all single-bit errors and bursts up to 16 bits, which covers typical UART
   noise; a sum would miss reordered bytes. CRC-32 is stronger but adds 2 bytes to a 20-byte frame
   (10 % overhead) for a benefit that is small at this frame size.

8. **How is the code tested without hardware?**
   The simulator generates frames; `loop://` sends them through a real pyserial port; a
   file transport replays captures; the parser is fuzzed; the reader is tested for overflow and
   error handling; the CLI is called through `main([...])`; the WebSocket endpoint is exercised
   with FastAPI's `TestClient`, all in-process with no network.

9. **How does the dashboard get data from a thread into async code safely?**
   The thread only touches the `queue.Queue`. An asyncio task drains it with `get_nowait()`
   every 100 ms, which never blocks the event loop, then sends a single batched message to each
   client. Batching keeps the browser at ~10 redraws per second regardless of the frame rate.

10. **What would you change to handle 1000 Hz or many sensors?**
    Raise the baud rate (or use USB CDC / CAN), consider a faster CRC (C extension), batch writes
    to disk, send decimated data to the browser, and add a protocol version byte so payloads can
    evolve. The length field already lets the host skip payloads it does not understand.
