import pandas as pd
import pytest

from sim_telemetry.analysis import (
    count_braking_events,
    count_dropped_packets,
    format_summary,
    summarize,
)


def make_session(brake: list[float], seq: list[int] | None = None) -> pd.DataFrame:
    n = len(brake)
    return pd.DataFrame(
        {
            "seq": seq if seq is not None else list(range(n)),
            "device_ms": [i * 20 for i in range(n)],  # 50 Hz
            "steering_deg": [0.0] * n,
            "throttle_pct": [0.0 if b else 100.0 for b in brake],
            "brake_pct": brake,
            "gear": [3] * (n // 2) + [4] * (n - n // 2),
            "speed_kmh": [100.0 + i for i in range(n)],
            "rpm": [5000] * n,
        }
    )


def test_braking_events_ignore_short_blips():
    brake = [0] * 10 + [80] * 25 + [0] * 10 + [50] * 3 + [0] * 10 + [100] * 15 + [0] * 5
    # 25 samples = 0.48 s (event), 3 samples = 0.04 s (blip), 15 samples = 0.28 s (event)
    assert count_braking_events(make_session(brake)) == 2


def test_dropped_packets_handles_wraparound():
    seq = pd.Series([65533, 65534, 0, 1, 5, 6])  # 65535 lost, then 2..4 lost
    assert count_dropped_packets(seq) == 4


def test_summary_values():
    df = make_session([0] * 40 + [90] * 20 + [0] * 40)
    summary = summarize(df)
    assert summary.samples == 100
    assert summary.duration_s == pytest.approx(1.98)
    assert summary.max_speed_kmh == 199.0
    assert summary.avg_throttle_pct == 80.0
    assert summary.full_throttle_share == 0.8
    assert summary.braking_events == 1
    assert summary.gear_usage == {3: 0.5, 4: 0.5}
    assert summary.dropped_packets == 0
    text = format_summary(summary)
    assert "Max speed          199.0 km/h" in text
    assert "Braking events     1" in text


def test_empty_session_is_an_error():
    with pytest.raises(ValueError):
        summarize(make_session([]))
