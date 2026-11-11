import csv

import pandas as pd
import pytest

from sim_telemetry.logger import COLUMNS, SessionLogger
from sim_telemetry.protocol import Frame, TelemetrySample
from sim_telemetry.reader import TelemetryReader
from sim_telemetry.simulator import SimulatedDevice
from sim_telemetry.stats import RollingStats
from sim_telemetry.transport import FileTransport, SimTransport, Transport

SAMPLE = TelemetrySample(
    steering_deg=-5.5, throttle_pct=40, brake_pct=0, gear=2, speed_kmh=60.25, rpm=3900
)


def make_frame(seq: int, device_ms: int, received_at: float) -> Frame:
    return Frame(seq=seq, timestamp_ms=device_ms, sample=SAMPLE, received_at=received_at)


def write_capture(tmp_path, count: int):
    path = tmp_path / "capture.bin"
    path.write_bytes(b"".join(SimulatedDevice(seed=0).frames(count)))
    return path


# --- reader -------------------------------------------------------------------------------


def test_reader_delivers_all_frames_from_file(tmp_path):
    reader = TelemetryReader(FileTransport(write_capture(tmp_path, 500)))
    reader.start()
    frames = list(reader.frames())
    assert [f.seq for f in frames] == list(range(500))
    assert all(f.received_at is not None for f in frames)
    assert reader.health()["frames_ok"] == 500
    assert reader.error is None


def test_reader_drops_oldest_when_queue_is_full(tmp_path):
    reader = TelemetryReader(FileTransport(write_capture(tmp_path, 100)), maxsize=10)
    reader.start()
    reader.join(timeout=5)  # nobody consumes, so the queue overflows
    frames = reader.drain()
    assert [f.seq for f in frames] == list(range(90, 100))  # newest frames survive
    assert reader.queue_overflows == 90


def test_reader_stop_ends_live_source():
    reader = TelemetryReader(SimTransport(rate_hz=100, seed=0))
    reader.start()
    first = next(reader.frames())
    reader.stop()
    assert not reader.is_alive()
    assert first.seq == 0


def test_reader_records_transport_errors():
    class BrokenTransport(Transport):
        def read(self, size: int) -> bytes:
            raise OSError("device disconnected")

    reader = TelemetryReader(BrokenTransport())
    reader.start()
    reader.join(timeout=2)
    assert isinstance(reader.error, OSError)


# --- stats --------------------------------------------------------------------------------


def test_stats_packet_rate_and_drops():
    stats = RollingStats(window_s=5.0)
    t0 = 1000.0
    for i in range(100):
        if i % 10 == 9:
            continue  # lose every 10th packet
        stats.update(make_frame(i, i * 20, t0 + i * 0.02))
    snap = stats.snapshot(now=t0 + 2.0)
    assert snap.total_frames == 90
    assert snap.packet_rate_hz == pytest.approx(45, rel=0.05)
    assert snap.dropped_in_window == 9  # the 10th loss is the last frame, not yet visible
    assert snap.drop_rate == pytest.approx(9 / 99, abs=1e-3)


def test_stats_latency_is_relative_to_best_frame():
    stats = RollingStats()
    t0 = 500.0
    stats.update(make_frame(0, 0, t0))  # best case
    stats.update(make_frame(1, 20, t0 + 0.020))  # on time
    stats.update(make_frame(2, 40, t0 + 0.070))  # 30 ms late
    snap = stats.snapshot(now=t0 + 0.1)
    assert snap.latency_ms_mean == pytest.approx(10.0, abs=0.01)
    assert snap.latency_ms_p95 == pytest.approx(30.0, abs=0.01)


def test_stats_window_evicts_old_frames():
    stats = RollingStats(window_s=1.0)
    for i in range(50):
        stats.update(make_frame(i, i * 20, 100.0 + i * 0.02))
    assert stats.snapshot(now=110.0).packet_rate_hz == 0.0
    assert stats.snapshot(now=110.0).total_frames == 50


# --- logger -------------------------------------------------------------------------------


def test_csv_logger_writes_header_and_rows(tmp_path):
    path = tmp_path / "session.csv"
    with SessionLogger(path) as log:
        for i in range(120):
            log.write(make_frame(i, i * 20, 1.0 + i))
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert list(rows[0].keys()) == COLUMNS
    assert len(rows) == 120
    assert float(rows[5]["speed_kmh"]) == 60.25


def test_parquet_logger_round_trip(tmp_path):
    pytest.importorskip("pyarrow")
    path = tmp_path / "session.parquet"
    with SessionLogger(path) as log:
        for i in range(30):
            log.write(make_frame(i, i * 20, 1.0 + i))
    df = pd.read_parquet(path)
    assert list(df.columns) == COLUMNS
    assert len(df) == 30


def test_logger_rejects_unknown_extension(tmp_path):
    with pytest.raises(ValueError):
        SessionLogger(tmp_path / "session.txt")
