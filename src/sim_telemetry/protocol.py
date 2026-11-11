"""Binary frame format shared by the device (real or simulated) and the host.

Every frame is little-endian and looks like this::

    | sync (2) | length (1) | seq (2) | timestamp_ms (4) | payload (length) | crc16 (2) |

The CRC covers everything between the sync bytes and the CRC itself
(length, seq, timestamp and payload). See README.md for the full table.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

SYNC = b"\xaa\x55"

# length (u8), seq (u16), timestamp_ms (u32) -- follows the two sync bytes
HEADER_STRUCT = struct.Struct("<BHI")
# steering (i16, 0.1 deg), throttle (u8, %), brake (u8, %), gear (i8),
# speed (u16, 0.01 km/h), rpm (u16)
PAYLOAD_STRUCT = struct.Struct("<hBBbHH")
CRC_STRUCT = struct.Struct("<H")

HEADER_SIZE = len(SYNC) + HEADER_STRUCT.size  # 9 bytes
PAYLOAD_SIZE = PAYLOAD_STRUCT.size  # 9 bytes
CRC_SIZE = CRC_STRUCT.size  # 2 bytes
FRAME_SIZE = HEADER_SIZE + PAYLOAD_SIZE + CRC_SIZE  # 20 bytes

# Anything longer than this is treated as a corrupted length byte.
MAX_PAYLOAD_SIZE = 64

SEQ_MODULO = 1 << 16
TIMESTAMP_MODULO = 1 << 32


def _make_crc_table() -> list[int]:
    table = []
    for byte in range(256):
        crc = byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
        table.append(crc & 0xFFFF)
    return table


_CRC_TABLE = _make_crc_table()


def crc16_ccitt(data: bytes | bytearray | memoryview, crc: int = 0xFFFF) -> int:
    """Compute CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflection, no final XOR).

    A lookup table processes one byte per step instead of one bit, which is
    about 8x faster in pure Python. The check value for b"123456789" is 0x29B1.
    """
    for byte in data:
        crc = ((crc << 8) & 0xFFFF) ^ _CRC_TABLE[(crc >> 8) ^ byte]
    return crc


@dataclass(frozen=True, slots=True)
class TelemetrySample:
    """One snapshot of the rig's controls and vehicle state, in engineering units."""

    steering_deg: float  # steering-wheel angle, negative = left
    throttle_pct: float  # 0..100
    brake_pct: float  # 0..100
    gear: int  # -1 = reverse, 0 = neutral, 1..6
    speed_kmh: float
    rpm: int


@dataclass(slots=True)
class Frame:
    """A decoded frame plus the time the host received it (set by the reader)."""

    seq: int
    timestamp_ms: int
    sample: TelemetrySample
    received_at: float | None = None


def _clamp(value: float, low: int, high: int) -> int:
    return max(low, min(high, round(value)))


def encode_payload(sample: TelemetrySample) -> bytes:
    """Pack a sample into the fixed-point payload, clamping values to the field ranges."""
    return PAYLOAD_STRUCT.pack(
        _clamp(sample.steering_deg * 10, -32768, 32767),
        _clamp(sample.throttle_pct, 0, 100),
        _clamp(sample.brake_pct, 0, 100),
        _clamp(sample.gear, -1, 9),
        _clamp(sample.speed_kmh * 100, 0, 65535),
        _clamp(sample.rpm, 0, 65535),
    )


def decode_payload(payload: bytes | bytearray | memoryview) -> TelemetrySample:
    """Unpack a payload produced by :func:`encode_payload`."""
    steer, throttle, brake, gear, speed, rpm = PAYLOAD_STRUCT.unpack(payload)
    return TelemetrySample(
        steering_deg=steer / 10,
        throttle_pct=float(throttle),
        brake_pct=float(brake),
        gear=gear,
        speed_kmh=speed / 100,
        rpm=rpm,
    )


def encode_frame(seq: int, timestamp_ms: int, sample: TelemetrySample) -> bytes:
    """Build a complete frame (sync + header + payload + CRC) ready to send."""
    payload = encode_payload(sample)
    body = (
        HEADER_STRUCT.pack(len(payload), seq % SEQ_MODULO, timestamp_ms % TIMESTAMP_MODULO)
        + payload
    )
    return SYNC + body + CRC_STRUCT.pack(crc16_ccitt(body))
