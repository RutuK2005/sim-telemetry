"""Byte sources the reader can pull from: a serial port, a loopback port, a file or the simulator.

All transports share one tiny interface -- ``read(size) -> bytes`` -- so the
reader and parser never need to know where the bytes come from.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path

import serial

from .simulator import SimulatedDevice


class Transport(ABC):
    """A source of raw bytes. ``read`` returns ``b""`` on timeout."""

    #: Set once the source can never produce more data (e.g. end of a file).
    at_eof: bool = False
    #: False for replayed data, which can wait for a slow consumer without losing anything.
    live: bool = True

    @abstractmethod
    def read(self, size: int) -> bytes:
        """Return up to ``size`` bytes, blocking for at most a short timeout."""

    def close(self) -> None:  # noqa: B027 - optional hook, not every transport needs it
        """Release the underlying resource."""

    def __enter__(self) -> Transport:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class SerialTransport(Transport):
    """A real serial port (``COM3``, ``/dev/ttyUSB0``) or any pyserial URL (``loop://``)."""

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 0.1):
        # serial_for_url accepts plain port names as well as URLs.
        self.serial = serial.serial_for_url(port, baudrate=baudrate, timeout=timeout)

    def read(self, size: int) -> bytes:
        # Block for at least one byte (or the timeout), then grab whatever else is waiting.
        waiting = self.serial.in_waiting
        return self.serial.read(min(size, waiting) if waiting else 1)

    def write(self, data: bytes) -> None:
        self.serial.write(data)

    def close(self) -> None:
        self.serial.close()


def pump_frames(
    device: SimulatedDevice,
    write: Callable[[bytes], object],
    stop: threading.Event,
    seconds: float | None = None,
) -> int:
    """Write frames from ``device`` in real time until ``stop`` is set or ``seconds`` pass.

    Returns the number of frames generated.
    """
    period = 1.0 / device.rate_hz
    start = time.monotonic()
    count = 0
    while not stop.is_set() and (seconds is None or count * period < seconds):
        write(device.next_frame(int(count * period * 1000)))
        count += 1
        # Sleep until the next frame is due; scheduling from `start` avoids drift.
        delay = start + count * period - time.monotonic()
        if delay > 0:
            stop.wait(delay)
    return count


class LoopbackTransport(SerialTransport):
    """pyserial's ``loop://`` port with a background thread playing the device.

    Bytes travel through a genuine pyserial ``Serial`` object, so this exercises
    the same code path as real hardware without needing any.
    """

    def __init__(self, rate_hz: float = 50.0, noise: float = 0.0, seed: int | None = None):
        super().__init__("loop://")
        self._stop = threading.Event()
        device = SimulatedDevice(rate_hz=rate_hz, noise=noise, seed=seed)
        self._feeder = threading.Thread(
            target=pump_frames, args=(device, self.write, self._stop), daemon=True
        )
        self._feeder.start()

    def close(self) -> None:
        self._stop.set()
        self._feeder.join(timeout=1)
        super().close()


class SimTransport(Transport):
    """In-process simulated device: frames appear at ``rate_hz`` as if from a serial line.

    With ``realtime=False`` every ``read`` returns the next frame immediately,
    which is handy for tests and for generating files quickly.
    """

    def __init__(
        self,
        rate_hz: float = 50.0,
        noise: float = 0.0,
        seed: int | None = None,
        realtime: bool = True,
        timeout: float = 0.1,
    ):
        self.device = SimulatedDevice(rate_hz=rate_hz, noise=noise, seed=seed)
        self.realtime = realtime
        self.timeout = timeout
        self._buffer = bytearray()
        self._produced = 0
        self._start = time.monotonic()

    def _produce(self, count: int) -> None:
        period_ms = 1000.0 / self.device.rate_hz
        for _ in range(count):
            self._buffer += self.device.next_frame(int(self._produced * period_ms))
            self._produced += 1

    def read(self, size: int) -> bytes:
        if not self.realtime:
            self._produce(1)
        else:
            elapsed = time.monotonic() - self._start
            due = int(elapsed * self.device.rate_hz) + 1  # frames the device has sent by now
            self._produce(due - self._produced)
            if not self._buffer:
                # Nothing yet: wait for the next frame, like a serial read with a timeout.
                next_due = self._produced / self.device.rate_hz - elapsed
                time.sleep(max(0.0, min(self.timeout, next_due)))
                return b""
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data


class FileTransport(Transport):
    """Replays a raw capture (e.g. written by ``sim-telemetry simulate --out x.bin``)."""

    live = False

    def __init__(self, path: str | Path):
        self._file = open(path, "rb")  # noqa: SIM115 - closed in close()

    def read(self, size: int) -> bytes:
        data = self._file.read(size)
        if not data:
            self.at_eof = True
        return data

    def close(self) -> None:
        self._file.close()


def open_transport(
    source: str,
    rate_hz: float = 50.0,
    noise: float = 0.0,
    baudrate: int = 115200,
    seed: int | None = None,
) -> Transport:
    """Create a transport from a CLI-style source string.

    ``sim`` -> in-process simulator, ``loop://`` -> pyserial loopback fed by the
    simulator, ``*.bin`` -> raw capture file, anything else -> serial port / pyserial URL.
    """
    if source == "sim":
        return SimTransport(rate_hz=rate_hz, noise=noise, seed=seed)
    if source == "loop://":
        return LoopbackTransport(rate_hz=rate_hz, noise=noise, seed=seed)
    if source.endswith(".bin"):
        return FileTransport(source)
    return SerialTransport(source, baudrate=baudrate)
