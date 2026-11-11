"""Command-line interface: ``sim-telemetry simulate | record | serve | analyze``."""

from __future__ import annotations

import argparse
import sys
import threading
import time
from collections.abc import Sequence

from .analysis import format_summary, load_session, summarize
from .logger import SessionLogger
from .parser import FrameParser
from .reader import TelemetryReader
from .simulator import SimulatedDevice
from .stats import RollingStats
from .transport import SerialTransport, open_transport, pump_frames

SOURCE_HELP = "COM3 / /dev/ttyUSB0 (serial), loop:// (pyserial loopback), sim, or capture.bin"


def cmd_simulate(args: argparse.Namespace) -> int:
    """Run the simulated device: preview frames, write a capture file, or drive a serial port."""
    device = SimulatedDevice(rate_hz=args.rate, noise=args.noise, seed=args.seed)
    count = int(args.seconds * args.rate)

    if args.out:
        with open(args.out, "wb") as f:
            for frame in device.frames(count):
                f.write(frame)
        print(f"wrote {count} frames ({args.seconds:g} s at {args.rate:g} Hz) to {args.out}")
        return 0

    if args.port:
        # Acts as the "device" end, e.g. one side of a virtual COM-port pair.
        transport = SerialTransport(args.port, baudrate=args.baud)
        print(f"streaming to {args.port} at {args.rate:g} Hz, Ctrl+C to stop")
        stop = threading.Event()
        try:
            sent = pump_frames(device, transport.write, stop, seconds=args.seconds or None)
        except KeyboardInterrupt:
            sent = device.seq
        finally:
            transport.close()
        print(f"sent {sent} frames")
        return 0

    # No destination: show a few frames as hex and decoded values.
    parser = FrameParser()
    for raw in device.frames(args.count):
        for frame in parser.feed(raw):
            s = frame.sample
            print(
                f"{raw.hex(' ')}  seq={frame.seq:<4} t={frame.timestamp_ms:>5}ms "
                f"speed={s.speed_kmh:6.1f} gear={s.gear} rpm={s.rpm:<5} "
                f"thr={s.throttle_pct:3.0f} brk={s.brake_pct:3.0f} steer={s.steering_deg:6.1f}"
            )
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    """Read from a source in a background thread and log every frame to CSV/Parquet."""
    transport = open_transport(args.source, rate_hz=args.rate, noise=args.noise, baudrate=args.baud)
    reader = TelemetryReader(transport)
    stats = RollingStats()
    deadline = time.monotonic() + args.seconds if args.seconds else None
    last_report = time.monotonic()

    hint = "" if deadline or not transport.live else " (Ctrl+C to stop)"
    print(f"recording {args.source} -> {args.out}{hint}")
    reader.start()
    try:
        with SessionLogger(args.out) as logger:
            for frame in reader.frames(until=deadline):
                logger.write(frame)
                stats.update(frame)
                if time.monotonic() - last_report >= 1.0:
                    last_report = time.monotonic()
                    snap = stats.snapshot()
                    print(
                        f"  frames={snap.total_frames:<7} rate={snap.packet_rate_hz:5.1f} Hz "
                        f"drop={snap.drop_rate:6.2%} crc_errors={reader.health()['crc_errors']}"
                    )
    except KeyboardInterrupt:
        print("stopped")
    finally:
        reader.stop()
        transport.close()

    health = reader.health()
    print(
        f"saved {stats.total_frames} frames to {args.out} | dropped={health['dropped_packets']} "
        f"crc_errors={health['crc_errors']} bytes_skipped={health['bytes_skipped']} "
        f"queue_overflows={health['queue_overflows']}"
    )
    if reader.error is not None:
        print(f"reader stopped with an error: {reader.error}", file=sys.stderr)
        return 1
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Start the live dashboard web server."""
    import uvicorn  # imported lazily: only this command needs it

    from .dashboard import create_app

    app = create_app(args.source, rate_hz=args.rate, noise=args.noise)
    print(f"dashboard on http://{args.host}:{args.http_port}  (source: {args.source})")
    uvicorn.run(app, host=args.host, port=args.http_port, log_level="warning")
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    """Print a Pandas summary of a recorded session."""
    df = load_session(args.session)
    print(f"Session: {args.session}")
    print(format_summary(summarize(df)))
    return 0


def _add_device_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--rate", type=float, default=50.0, help="frames per second (default 50)")
    p.add_argument(
        "--noise", type=float, default=0.0, help="probability of damaging each simulated frame"
    )
    p.add_argument("--baud", type=int, default=115200, help="serial baud rate (default 115200)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sim-telemetry", description="Telemetry pipeline for a driving-simulator rig."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("simulate", help="run the simulated device")
    p.add_argument("--out", help="write a raw capture file (e.g. capture.bin) as fast as possible")
    p.add_argument("--port", help="stream in real time to this serial port / pyserial URL")
    p.add_argument("--seconds", type=float, default=10.0, help="how much data to generate")
    p.add_argument("--count", type=int, default=10, help="frames to print in preview mode")
    p.add_argument("--seed", type=int, default=None, help="random seed for repeatable output")
    _add_device_options(p)
    p.set_defaults(func=cmd_simulate)

    p = sub.add_parser("record", help="record a session to CSV or Parquet")
    p.add_argument("--port", "--source", dest="source", required=True, help=SOURCE_HELP)
    p.add_argument("--out", required=True, help="output file, .csv or .parquet")
    p.add_argument("--seconds", type=float, default=None, help="stop after this many seconds")
    _add_device_options(p)
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("serve", help="serve the live web dashboard")
    p.add_argument("--source", default="sim", help=SOURCE_HELP)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--http-port", type=int, default=8000)
    _add_device_options(p)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("analyze", help="summarise a recorded session")
    p.add_argument("session", help="session .csv or .parquet file")
    p.set_defaults(func=cmd_analyze)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``sim-telemetry`` console script."""
    args = build_parser().parse_args(argv)
    return args.func(args)
