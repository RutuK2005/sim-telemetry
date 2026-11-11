import random
import time

import numpy as np

from sim_telemetry.parser import FrameParser
from sim_telemetry.simulator import (
    LapSimulator,
    SimulatedDevice,
    Track,
    apply_link_noise,
    speed_profile,
)
from sim_telemetry.transport import (
    FileTransport,
    LoopbackTransport,
    SimTransport,
    open_transport,
)


def read_frames(transport, wanted: int, max_seconds: float = 3.0) -> list:
    parser, frames = FrameParser(), []
    deadline = time.monotonic() + max_seconds
    while len(frames) < wanted and time.monotonic() < deadline:
        frames.extend(parser.feed(transport.read(256)))
    return frames


def test_speed_profile_is_slower_in_corners():
    track = Track()
    v = speed_profile(track)
    straight = np.abs(track.curvature) < 1e-6
    assert v.min() > 5
    assert v[straight].max() > v[~straight].min() * 2


def test_lap_simulator_values_are_plausible():
    sim = LapSimulator(seed=1)
    samples = [sim.step(0.02) for _ in range(50 * 90)]
    assert sim.laps >= 1
    for s in samples:
        assert 0 <= s.throttle_pct <= 100 and 0 <= s.brake_pct <= 100
        assert not (s.throttle_pct > 0 and s.brake_pct > 0)
        assert 1 <= s.gear <= 6
        assert 0 < s.speed_kmh < 270
        assert 900 <= s.rpm <= 7500
    assert {s.gear for s in samples} >= {1, 2, 3, 4, 5}
    assert any(s.brake_pct > 50 for s in samples)


def test_lap_simulator_is_deterministic_with_seed():
    a, b = LapSimulator(seed=3), LapSimulator(seed=3)
    assert [a.step(0.02) for _ in range(100)] == [b.step(0.02) for _ in range(100)]


def test_simulated_device_frames_parse_back():
    device = SimulatedDevice(rate_hz=50, seed=0)
    frames = FrameParser().feed(b"".join(device.frames(100)))
    assert [f.seq for f in frames] == list(range(100))
    assert [f.timestamp_ms for f in frames[:3]] == [0, 20, 40]


def test_link_noise_always_changes_frame_when_probability_is_one():
    frame = next(SimulatedDevice(seed=0).frames(1))
    rng = random.Random(0)
    assert all(apply_link_noise(frame, rng, 1.0) != frame for _ in range(50))


def test_noisy_device_still_parses_most_frames():
    device = SimulatedDevice(rate_hz=50, seed=4, noise=0.05)
    parser = FrameParser()
    frames = parser.feed(b"".join(device.frames(2000)) + bytes(80))
    assert len(frames) > 1800
    assert parser.stats.dropped_packets == 2000 - len(frames)


def test_sim_transport_fast_mode():
    frames = read_frames(SimTransport(realtime=False, seed=0), wanted=50)
    assert [f.seq for f in frames] == list(range(50))


def test_sim_transport_realtime_paces_frames():
    transport = SimTransport(rate_hz=100, seed=0)
    start = time.monotonic()
    frames = read_frames(transport, wanted=20)
    elapsed = time.monotonic() - start
    assert len(frames) == 20
    assert elapsed > 0.1  # 20 frames at 100 Hz cannot arrive instantly


def test_loopback_transport_uses_real_pyserial_port():
    with LoopbackTransport(rate_hz=100, seed=0) as transport:
        frames = read_frames(transport, wanted=10)
    assert [f.seq for f in frames] == list(range(10))


def test_file_transport_reaches_eof(tmp_path):
    path = tmp_path / "capture.bin"
    path.write_bytes(b"".join(SimulatedDevice(seed=0).frames(30)))
    with FileTransport(path) as transport:
        frames = read_frames(transport, wanted=30)
        assert transport.read(64) == b""
        assert transport.at_eof
    assert len(frames) == 30


def test_open_transport_dispatch(tmp_path):
    path = tmp_path / "x.bin"
    path.write_bytes(b"")
    assert isinstance(open_transport("sim"), SimTransport)
    with open_transport(str(path)) as t:
        assert isinstance(t, FileTransport)
    with open_transport("loop://") as t:
        assert isinstance(t, LoopbackTransport)
