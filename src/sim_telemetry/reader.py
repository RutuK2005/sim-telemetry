"""Background reader thread: transport -> parser -> bounded queue.

Serial ports must be drained continuously or the OS buffer overflows, so
reading lives in its own thread. Consumers (logger, stats, dashboard) take
frames from the queue at their own pace.
"""

from __future__ import annotations

import contextlib
import queue
import threading
import time
from collections.abc import Iterator

from .parser import FrameParser
from .protocol import Frame
from .transport import Transport


class TelemetryReader(threading.Thread):
    """Reads bytes from a transport, decodes frames and puts them on ``self.queue``.

    When the queue is full the *oldest* frame is discarded: for live telemetry
    the newest value is the most useful one, and the reader must never block.
    """

    def __init__(self, transport: Transport, maxsize: int = 1000, chunk_size: int = 256):
        super().__init__(name="telemetry-reader", daemon=True)
        self.transport = transport
        self.queue: queue.Queue[Frame] = queue.Queue(maxsize=maxsize)
        self.parser = FrameParser()
        self.chunk_size = chunk_size
        self.queue_overflows = 0
        self.error: Exception | None = None
        # Not called `_stop`: threading.Thread already uses that name internally.
        self._stop_event = threading.Event()

    def run(self) -> None:
        try:
            while not self._stop_event.is_set():
                data = self.transport.read(self.chunk_size)
                if not data:
                    if self.transport.at_eof:
                        break
                    continue
                received_at = time.time()
                for frame in self.parser.feed(data):
                    frame.received_at = received_at
                    self._put(frame)
        except Exception as exc:  # e.g. serial.SerialException when a USB cable is pulled
            self.error = exc

    def _put(self, frame: Frame) -> None:
        try:
            self.queue.put_nowait(frame)
        except queue.Full:
            # This thread is the only producer, so after removing one item there is room.
            with contextlib.suppress(queue.Empty):
                self.queue.get_nowait()
            self.queue_overflows += 1
            self.queue.put_nowait(frame)

    def stop(self, timeout: float = 2.0) -> None:
        """Ask the thread to finish and wait for it."""
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout)

    def frames(self, poll: float = 0.1) -> Iterator[Frame]:
        """Yield frames as they arrive until the reader has stopped and the queue is empty."""
        while True:
            try:
                yield self.queue.get(timeout=poll)
            except queue.Empty:
                if not self.is_alive():
                    return

    def drain(self) -> list[Frame]:
        """Return every frame currently queued without blocking."""
        frames = []
        while True:
            try:
                frames.append(self.queue.get_nowait())
            except queue.Empty:
                return frames

    def health(self) -> dict[str, int]:
        """Link and queue counters (safe to call from another thread; values are plain ints)."""
        return {
            **self.parser.stats.as_dict(),
            "queue_overflows": self.queue_overflows,
            "queue_size": self.queue.qsize(),
        }
