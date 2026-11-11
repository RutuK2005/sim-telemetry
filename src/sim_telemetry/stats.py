"""Rolling link statistics: packet rate, drop rate and latency over a sliding time window."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from .protocol import SEQ_MODULO, Frame


@dataclass(frozen=True, slots=True)
class StatsSnapshot:
    total_frames: int
    packet_rate_hz: float
    dropped_in_window: int
    drop_rate: float  # 0..1, dropped / expected within the window
    latency_ms_mean: float
    latency_ms_p95: float


class RollingStats:
    """Keeps the last ``window_s`` seconds of frames and summarises them.

    Latency caveat: the device clock (ms since boot) and the host clock are not
    synchronised, so absolute one-way latency is unknowable. Instead we track
    ``host_time - device_time`` and report it relative to the smallest value
    seen, i.e. "how much later than the best-case frame did this one arrive".
    That captures buffering and jitter, which is what matters for a live view.
    """

    def __init__(self, window_s: float = 5.0):
        self.window_s = window_s
        self.total_frames = 0
        self._window: deque[tuple[float, int, float]] = deque()  # (received_at, gap, offset)
        self._last_seq: int | None = None
        self._min_offset_ms: float | None = None
        self._first_seen: float | None = None

    def update(self, frame: Frame) -> None:
        """Record one received frame."""
        received_at = frame.received_at if frame.received_at is not None else time.time()
        gap = 0
        if self._last_seq is not None:
            gap = (frame.seq - self._last_seq - 1) % SEQ_MODULO
            if gap >= SEQ_MODULO // 2:  # sequence went backwards (device reset)
                gap = 0
        self._last_seq = frame.seq

        offset_ms = received_at * 1000 - frame.timestamp_ms
        if self._min_offset_ms is None or offset_ms < self._min_offset_ms:
            self._min_offset_ms = offset_ms
        if self._first_seen is None:
            self._first_seen = received_at

        self._window.append((received_at, gap, offset_ms))
        self.total_frames += 1
        self._evict(received_at)

    def _evict(self, now: float) -> None:
        while self._window and self._window[0][0] < now - self.window_s:
            self._window.popleft()

    def snapshot(self, now: float | None = None) -> StatsSnapshot:
        """Summarise the current window."""
        now = time.time() if now is None else now
        self._evict(now)
        count = len(self._window)
        if count == 0 or self._first_seen is None or self._min_offset_ms is None:
            return StatsSnapshot(self.total_frames, 0.0, 0, 0.0, 0.0, 0.0)

        # Until a full window has passed, divide by the time we have actually been running.
        span = min(self.window_s, max(now - self._first_seen, 1e-3))
        dropped = sum(gap for _, gap, _ in self._window)
        latencies = sorted(offset - self._min_offset_ms for _, _, offset in self._window)
        p95 = latencies[min(count - 1, int(0.95 * count))]
        return StatsSnapshot(
            total_frames=self.total_frames,
            packet_rate_hz=round(count / span, 1),
            dropped_in_window=dropped,
            drop_rate=round(dropped / (count + dropped), 4),
            latency_ms_mean=round(sum(latencies) / count, 2),
            latency_ms_p95=round(p95, 2),
        )
