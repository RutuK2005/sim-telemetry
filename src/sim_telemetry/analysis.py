"""Offline analysis of a recorded session with Pandas."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .protocol import SEQ_MODULO, TIMESTAMP_MODULO


@dataclass(frozen=True)
class SessionSummary:
    samples: int
    duration_s: float
    max_speed_kmh: float
    avg_speed_kmh: float
    avg_throttle_pct: float
    full_throttle_share: float  # fraction of samples at >= 95 % throttle
    braking_events: int
    max_brake_pct: float
    max_rpm: int
    gear_usage: dict[int, float]  # gear -> fraction of samples
    dropped_packets: int


def load_session(path: str | Path) -> pd.DataFrame:
    """Read a session written by :class:`~sim_telemetry.logger.SessionLogger`."""
    path = Path(path)
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path)


def session_time_s(df: pd.DataFrame) -> pd.Series:
    """Seconds since the first sample, from the device clock (handles u32 wrap-around)."""
    step_ms = df["device_ms"].diff().fillna(0).mod(TIMESTAMP_MODULO)
    return step_ms.cumsum() / 1000.0


def count_braking_events(
    df: pd.DataFrame, threshold_pct: float = 10.0, min_duration_s: float = 0.2
) -> int:
    """Count separate stretches where the brake is above ``threshold_pct``.

    Consecutive braking samples form one event; very short blips (sensor noise
    or a tap of the pedal) shorter than ``min_duration_s`` are ignored.
    """
    braking = df["brake_pct"] >= threshold_pct
    # A new run starts every time `braking` changes value; cumsum gives each run an id.
    run_id = (braking != braking.shift()).cumsum()
    t = session_time_s(df)
    runs = t[braking].groupby(run_id[braking]).agg(["min", "max"])
    return int(((runs["max"] - runs["min"]) >= min_duration_s).sum())


def count_dropped_packets(seq: pd.Series) -> int:
    """Infer lost packets from gaps in the sequence numbers."""
    gaps = (seq.diff().dropna() - 1).mod(SEQ_MODULO)
    return int(gaps[gaps < SEQ_MODULO // 2].sum())


def summarize(df: pd.DataFrame) -> SessionSummary:
    """Compute headline numbers for a session DataFrame."""
    if df.empty:
        raise ValueError("session contains no samples")
    gear_share = df["gear"].value_counts(normalize=True).sort_index()
    return SessionSummary(
        samples=len(df),
        duration_s=round(float(session_time_s(df).iloc[-1]), 2),
        max_speed_kmh=round(float(df["speed_kmh"].max()), 1),
        avg_speed_kmh=round(float(df["speed_kmh"].mean()), 1),
        avg_throttle_pct=round(float(df["throttle_pct"].mean()), 1),
        full_throttle_share=round(float((df["throttle_pct"] >= 95).mean()), 3),
        braking_events=count_braking_events(df),
        max_brake_pct=float(df["brake_pct"].max()),
        max_rpm=int(df["rpm"].max()),
        gear_usage={int(g): round(float(s), 3) for g, s in gear_share.items()},
        dropped_packets=count_dropped_packets(df["seq"]),
    )


def format_summary(summary: SessionSummary) -> str:
    """Render a summary as a plain-text report."""
    lines = [
        f"Samples            {summary.samples}",
        f"Duration           {summary.duration_s:.1f} s",
        f"Max speed          {summary.max_speed_kmh:.1f} km/h",
        f"Average speed      {summary.avg_speed_kmh:.1f} km/h",
        f"Average throttle   {summary.avg_throttle_pct:.1f} %",
        f"Full throttle      {summary.full_throttle_share:.1%} of samples",
        f"Braking events     {summary.braking_events}",
        f"Max brake          {summary.max_brake_pct:.0f} %",
        f"Max rpm            {summary.max_rpm}",
        f"Dropped packets    {summary.dropped_packets}",
        "Gear usage",
    ]
    for gear, share in summary.gear_usage.items():
        label = {-1: "R", 0: "N"}.get(gear, str(gear))
        lines.append(f"  {label:>2}  {'#' * round(share * 40):<40} {share:6.1%}")
    return "\n".join(lines)
