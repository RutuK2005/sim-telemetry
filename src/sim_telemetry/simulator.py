"""A simulated rig: a simple car lapping a simple track, emitting real frames.

The driving model is the classic "quasi-steady-state lap simulation":

1. Sample the track every metre and store its curvature (1 / radius).
2. In each corner the speed is limited by grip: ``v = sqrt(a_lat * radius)``.
3. A forward pass limits how fast the car can accelerate out of corners and a
   backward pass makes it brake early enough for the next one.

Throttle, brake, steering, gear and rpm are then derived from that speed
profile, so the numbers are physically consistent with each other.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterator

import numpy as np

from .protocol import TelemetrySample, encode_frame

# (length in metres, signed radius in metres; 0 = straight, negative = left turn)
DEFAULT_TRACK: list[tuple[float, float]] = [
    (650, 0),
    (70, 45),  # tight right after the main straight
    (240, 0),
    (130, -120),  # fast left
    (180, 0),
    (50, 18),  # hairpin
    (420, 0),
    (90, -60),  # chicane, part 1
    (90, 60),  # chicane, part 2
    (300, 0),
    (160, 90),  # long final corner
]

GEAR_RATIOS = (3.20, 2.10, 1.60, 1.25, 1.00, 0.82)
FINAL_DRIVE = 3.9
WHEEL_RADIUS_M = 0.32
WHEELBASE_M = 2.6
STEERING_RATIO = 14.0
IDLE_RPM, UPSHIFT_RPM, DOWNSHIFT_RPM = 900, 7000, 3200
MAX_SPEED_MS = 72.0  # ~260 km/h
LATERAL_ACCEL = 11.0  # m/s^2 of cornering grip
BRAKE_DECEL = 10.0  # m/s^2


def drive_accel(speed_ms: float) -> float:
    """Maximum forward acceleration (m/s^2); falls off with speed because of drag."""
    return 6.0 * (1 - speed_ms / MAX_SPEED_MS) + 0.5


class Track:
    """A closed track sampled every ``ds`` metres as an array of curvatures."""

    def __init__(self, segments: list[tuple[float, float]] = DEFAULT_TRACK, ds: float = 1.0):
        self.ds = ds
        pieces = [
            np.full(int(length / ds), 0.0 if radius == 0 else 1.0 / radius)
            for length, radius in segments
        ]
        raw = np.concatenate(pieces)
        # Drivers turn the wheel gradually, so smooth curvature over ~25 m (wrapping around).
        window = max(1, int(25 / ds))
        padded = np.concatenate([raw[-window:], raw, raw[:window]])
        smooth = np.convolve(padded, np.ones(window) / window, mode="same")
        self.curvature = smooth[window:-window]

    @property
    def length_m(self) -> float:
        return len(self.curvature) * self.ds


def speed_profile(track: Track) -> np.ndarray:
    """Return the fastest achievable speed (m/s) at every point of ``track``."""
    with np.errstate(divide="ignore"):
        corner_limit = np.sqrt(LATERAL_ACCEL / np.abs(track.curvature))
    v = np.minimum(corner_limit, MAX_SPEED_MS)
    n, ds = len(v), track.ds

    # Start both passes at the slowest point so the lap "wraps" correctly.
    start = int(np.argmin(v))
    for k in range(1, n):  # forward pass: limited by engine power
        i, prev = (start + k) % n, (start + k - 1) % n
        v[i] = min(v[i], math.sqrt(v[prev] ** 2 + 2 * drive_accel(v[prev]) * ds))
    for k in range(1, n):  # backward pass: brake in time for what comes next
        i, nxt = (start - k) % n, (start - k + 1) % n
        v[i] = min(v[i], math.sqrt(v[nxt] ** 2 + 2 * BRAKE_DECEL * ds))
    return v


def engine_rpm(speed_ms: float, gear: int) -> int:
    """Engine speed for a given road speed and gear (no clutch slip)."""
    if gear < 1:
        return IDLE_RPM
    wheel_rad_s = speed_ms / WHEEL_RADIUS_M
    rpm = wheel_rad_s * GEAR_RATIOS[gear - 1] * FINAL_DRIVE * 60 / (2 * math.pi)
    return max(IDLE_RPM, int(rpm))


class LapSimulator:
    """Steps a car around the track and produces :class:`TelemetrySample` values."""

    def __init__(self, track: Track | None = None, seed: int | None = None):
        self.track = track or Track()
        self.profile = speed_profile(self.track)
        self.rng = random.Random(seed)
        self.distance_m = float(np.argmin(self.profile))  # start at the slowest corner
        self.gear = 1
        self.laps = 0

    def step(self, dt: float) -> TelemetrySample:
        """Advance the car by ``dt`` seconds and return the new state."""
        n = len(self.profile)
        i = int(self.distance_m / self.track.ds) % n
        v, v_next = float(self.profile[i]), float(self.profile[(i + 1) % n])
        accel = (v_next**2 - v**2) / (2 * self.track.ds)

        if accel < -0.3:
            throttle, brake = 0.0, min(100.0, 100 * -accel / BRAKE_DECEL)
        else:
            throttle, brake = min(100.0, 25 + 75 * accel / drive_accel(v)), 0.0
            if 5 < throttle < 95:
                throttle += self.rng.gauss(0, 1.5)  # a human foot is never perfectly still

        steer_rad = math.atan(WHEELBASE_M * float(self.track.curvature[i]))
        steering = math.degrees(steer_rad) * STEERING_RATIO + self.rng.gauss(0, 0.4)

        self._shift(v)
        new_distance = self.distance_m + v * dt
        if new_distance >= self.track.length_m:
            self.laps += 1
        self.distance_m = new_distance % self.track.length_m

        return TelemetrySample(
            steering_deg=round(steering, 1),
            throttle_pct=round(max(0.0, min(100.0, throttle))),
            brake_pct=round(brake),
            gear=self.gear,
            speed_kmh=round(v * 3.6, 2),
            rpm=engine_rpm(v, self.gear),
        )

    def _shift(self, speed_ms: float) -> None:
        # Hysteresis between up- and downshift points avoids hunting between gears.
        rpm = engine_rpm(speed_ms, self.gear)
        if rpm > UPSHIFT_RPM and self.gear < len(GEAR_RATIOS):
            self.gear += 1
        elif rpm < DOWNSHIFT_RPM and self.gear > 1:
            self.gear -= 1


class SimulatedDevice:
    """Turns simulator samples into numbered, timestamped frames, like the firmware does."""

    def __init__(self, rate_hz: float = 50.0, seed: int | None = None, noise: float = 0.0):
        self.rate_hz = rate_hz
        self.noise = noise
        self.sim = LapSimulator(seed=seed)
        self.rng = random.Random(seed)
        self.seq = 0

    def next_frame(self, timestamp_ms: int) -> bytes:
        """Generate the next frame; with ``noise`` > 0 the bytes may be damaged."""
        sample = self.sim.step(1.0 / self.rate_hz)
        frame = encode_frame(self.seq, timestamp_ms, sample)
        self.seq += 1
        return apply_link_noise(frame, self.rng, self.noise)

    def frames(self, count: int) -> Iterator[bytes]:
        """Yield ``count`` frames with timestamps spaced exactly one period apart."""
        period_ms = 1000.0 / self.rate_hz
        for k in range(count):
            yield self.next_frame(int(k * period_ms))


def apply_link_noise(frame: bytes, rng: random.Random, probability: float) -> bytes:
    """Randomly drop, corrupt or pad a frame to imitate a noisy serial link."""
    if probability <= 0 or rng.random() >= probability:
        return frame
    fault = rng.choice(("drop", "flip", "garbage"))
    if fault == "drop":
        return b""
    if fault == "flip":
        damaged = bytearray(frame)
        damaged[rng.randrange(len(damaged))] ^= 1 << rng.randrange(8)
        return bytes(damaged)
    return bytes(rng.getrandbits(8) for _ in range(rng.randint(1, 16))) + frame
