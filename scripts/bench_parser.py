"""Measure how fast the pure-Python parser decodes frames on this machine.

Usage:  python scripts/bench_parser.py [--frames 100000] [--noise 0.0]
"""

from __future__ import annotations

import argparse
import platform
import time

from sim_telemetry.parser import FrameParser
from sim_telemetry.protocol import FRAME_SIZE
from sim_telemetry.simulator import SimulatedDevice


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=100_000)
    ap.add_argument("--noise", type=float, default=0.0)
    ap.add_argument("--chunk", type=int, default=4096, help="bytes per feed() call")
    args = ap.parse_args()

    data = b"".join(SimulatedDevice(seed=0, noise=args.noise).frames(args.frames))
    parser = FrameParser()
    start = time.perf_counter()
    decoded = 0
    for pos in range(0, len(data), args.chunk):
        decoded += len(parser.feed(data[pos : pos + args.chunk]))
    elapsed = time.perf_counter() - start

    # 115200 baud with 8N1 framing = 10 bits per byte on the wire.
    link_fps = 115200 / 10 / FRAME_SIZE
    print(f"python {platform.python_version()} on {platform.system()} {platform.machine()}")
    print(f"decoded {decoded} frames ({len(data) / 1e6:.1f} MB) in {elapsed:.2f} s")
    print(f"throughput: {decoded / elapsed:,.0f} frames/s, {len(data) / elapsed / 1e6:.2f} MB/s")
    print(f"a 115200-baud link carries at most {link_fps:.0f} frames/s of {FRAME_SIZE} bytes")
    print(f"parser stats: {parser.stats.as_dict()}")


if __name__ == "__main__":
    main()
