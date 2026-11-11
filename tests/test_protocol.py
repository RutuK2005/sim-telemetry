import pytest

from sim_telemetry.protocol import (
    FRAME_SIZE,
    PAYLOAD_SIZE,
    SYNC,
    TelemetrySample,
    crc16_ccitt,
    decode_payload,
    encode_frame,
    encode_payload,
)

SAMPLE = TelemetrySample(
    steering_deg=-123.4, throttle_pct=87, brake_pct=0, gear=4, speed_kmh=142.37, rpm=6150
)


def test_crc_check_value():
    # Standard check value for CRC-16/CCITT-FALSE.
    assert crc16_ccitt(b"123456789") == 0x29B1


def test_crc_is_incremental():
    data = b"telemetry frame"
    assert crc16_ccitt(data[7:], crc16_ccitt(data[:7])) == crc16_ccitt(data)


def test_frame_sizes():
    assert PAYLOAD_SIZE == 9
    assert FRAME_SIZE == 20
    frame = encode_frame(1, 1000, SAMPLE)
    assert len(frame) == FRAME_SIZE
    assert frame.startswith(SYNC)


def test_payload_round_trip():
    assert decode_payload(encode_payload(SAMPLE)) == SAMPLE


def test_payload_clamps_out_of_range_values():
    wild = TelemetrySample(
        steering_deg=99999, throttle_pct=150, brake_pct=-5, gear=12, speed_kmh=900, rpm=-1
    )
    decoded = decode_payload(encode_payload(wild))
    assert decoded.steering_deg == pytest.approx(3276.7)
    assert decoded.throttle_pct == 100
    assert decoded.brake_pct == 0
    assert decoded.gear == 9
    assert decoded.speed_kmh == pytest.approx(655.35)
    assert decoded.rpm == 0


def test_seq_and_timestamp_wrap():
    frame = encode_frame(65536 + 5, 2**32 + 7, SAMPLE)
    assert int.from_bytes(frame[3:5], "little") == 5
    assert int.from_bytes(frame[5:9], "little") == 7
