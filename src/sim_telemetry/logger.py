"""Session logger: writes every decoded frame to CSV (streaming) or Parquet (on close)."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import pandas as pd

from .protocol import Frame

COLUMNS = [
    "received_at",
    "seq",
    "device_ms",
    "steering_deg",
    "throttle_pct",
    "brake_pct",
    "gear",
    "speed_kmh",
    "rpm",
]


def frame_to_row(frame: Frame) -> dict[str, Any]:
    """Flatten a frame into a dict keyed by :data:`COLUMNS`."""
    s = frame.sample
    return {
        "received_at": round(frame.received_at or 0.0, 6),
        "seq": frame.seq,
        "device_ms": frame.timestamp_ms,
        "steering_deg": s.steering_deg,
        "throttle_pct": s.throttle_pct,
        "brake_pct": s.brake_pct,
        "gear": s.gear,
        "speed_kmh": s.speed_kmh,
        "rpm": s.rpm,
    }


class SessionLogger:
    """Log frames to ``path``; the file extension (.csv or .parquet) selects the format.

    CSV rows are written as they arrive (flushed every ``flush_every`` rows), so
    a crash loses at most a few rows. Parquet is a columnar format that cannot
    be appended row by row, so rows are buffered in memory and written on close.
    """

    def __init__(self, path: str | Path, flush_every: int = 50):
        self.path = Path(path)
        self.format = self.path.suffix.lower().lstrip(".")
        if self.format not in ("csv", "parquet"):
            raise ValueError(f"unsupported log format {self.path.suffix!r}; use .csv or .parquet")
        self.flush_every = flush_every
        self.rows_written = 0
        self._rows: list[dict[str, Any]] = []
        self._closed = False
        self._file = None
        if self.format == "csv":
            self._file = self.path.open("w", newline="", encoding="utf-8")
            self._writer = csv.DictWriter(self._file, fieldnames=COLUMNS)
            self._writer.writeheader()

    def write(self, frame: Frame) -> None:
        """Append one frame to the session."""
        row = frame_to_row(frame)
        if self._file is not None:
            self._writer.writerow(row)
            if (self.rows_written + 1) % self.flush_every == 0:
                self._file.flush()
        else:
            self._rows.append(row)
        self.rows_written += 1

    def close(self) -> None:
        """Flush and close the file (writes the Parquet file if that format was chosen)."""
        if self._closed:
            return
        self._closed = True
        if self._file is not None:
            self._file.close()
        else:
            pd.DataFrame(self._rows, columns=COLUMNS).to_parquet(self.path, index=False)
            self._rows.clear()

    def __enter__(self) -> SessionLogger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
