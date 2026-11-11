import random

from sim_telemetry.parser import FrameParser
from sim_telemetry.protocol import (
    CRC_STRUCT,
    HEADER_STRUCT,
    SYNC,
    TelemetrySample,
    crc16_ccitt,
    encode_frame,
)

SAMPLE = TelemetrySample(
    steering_deg=12.5, throttle_pct=60, brake_pct=0, gear=3, speed_kmh=88.8, rpm=4200
)


def make_frames(count: int, start_seq: int = 0) -> list[bytes]:
    return [encode_frame(start_seq + i, 1000 + 20 * i, SAMPLE) for i in range(count)]


def feed_in_random_chunks(parser: FrameParser, data: bytes, rng: random.Random) -> list:
    frames, pos = [], 0
    while pos < len(data):
        size = rng.randint(1, 64)
        frames.extend(parser.feed(data[pos : pos + size]))
        pos += size
    return frames


def test_clean_stream_single_feed():
    parser = FrameParser()
    frames = parser.feed(b"".join(make_frames(10)))
    assert [f.seq for f in frames] == list(range(10))
    assert frames[0].sample == SAMPLE
    assert parser.stats.frames_ok == 10
    assert parser.stats.dropped_packets == 0
    assert parser.stats.bytes_skipped == 0


def test_byte_by_byte_feed_gives_same_result():
    parser = FrameParser()
    frames = []
    for byte in b"".join(make_frames(5)):
        frames.extend(parser.feed(bytes([byte])))
    assert [f.seq for f in frames] == [0, 1, 2, 3, 4]


def test_leading_garbage_is_skipped():
    parser = FrameParser()
    frames = parser.feed(b"\x00\x13\x37garbage" + make_frames(1)[0])
    assert len(frames) == 1
    assert parser.stats.bytes_skipped == 10


def test_corrupted_frame_counts_crc_error_and_drop():
    raw = make_frames(3)
    bad = bytearray(raw[1])
    bad[12] ^= 0xFF  # flip a payload byte
    parser = FrameParser()
    frames = parser.feed(raw[0] + bytes(bad) + raw[2])
    assert [f.seq for f in frames] == [0, 2]
    assert parser.stats.crc_errors == 1
    assert parser.stats.dropped_packets == 1


def test_sequence_gap_detected():
    raw = make_frames(10)
    parser = FrameParser()
    parser.feed(b"".join(raw[:3] + raw[7:]))  # frames 3..6 lost
    assert parser.stats.dropped_packets == 4


def test_sequence_wraparound_is_not_a_drop():
    parser = FrameParser()
    frames = parser.feed(b"".join(make_frames(4, start_seq=65534)))
    assert [f.seq for f in frames] == [65534, 65535, 0, 1]
    assert parser.stats.dropped_packets == 0


def test_backwards_sequence_counts_as_reset():
    parser = FrameParser()
    parser.feed(b"".join(make_frames(3, start_seq=500)))
    parser.feed(b"".join(make_frames(3, start_seq=0)))
    assert parser.stats.seq_resets == 1
    assert parser.stats.dropped_packets == 0


def test_bad_length_byte_triggers_resync():
    good = make_frames(1)[0]
    parser = FrameParser()
    frames = parser.feed(SYNC + b"\xff" + good)
    assert len(frames) == 1
    assert parser.stats.bad_length == 1


def test_unknown_payload_length_is_counted_not_decoded():
    body = HEADER_STRUCT.pack(3, 0, 0) + b"\x01\x02\x03"
    odd = SYNC + body + CRC_STRUCT.pack(crc16_ccitt(body))
    parser = FrameParser()
    frames = parser.feed(odd + make_frames(1, start_seq=1)[0])
    assert [f.seq for f in frames] == [1]
    assert parser.stats.unsupported_payload == 1


def test_fuzz_random_garbage_between_frames():
    rng = random.Random(2025)
    raw = make_frames(2000)
    stream = bytearray()
    for frame in raw:
        if rng.random() < 0.4:
            garbage = bytes(rng.getrandbits(8) for _ in range(rng.randint(1, 40)))
            if rng.random() < 0.3:
                garbage += SYNC  # deliberately plant false sync words
            stream += garbage
        stream += frame
    # A false header right before the end could make the parser wait for more data,
    # exactly as it would on a live link. Zero padding cannot contain a sync word.
    stream += bytes(80)

    parser = FrameParser()
    frames = feed_in_random_chunks(parser, bytes(stream), rng)

    assert [f.seq for f in frames] == list(range(2000))
    assert all(f.sample == SAMPLE for f in frames)
    assert parser.stats.dropped_packets == 0
    assert parser.stats.bytes_skipped > 0


def test_fuzz_bit_flips_inside_frames():
    rng = random.Random(7)
    raw = [bytearray(f) for f in make_frames(1000)]
    corrupted = set(rng.sample(range(1, 999), 50))  # never the first or last frame
    for index in corrupted:
        raw[index][rng.randrange(len(raw[index]))] ^= 1 << rng.randrange(8)

    parser = FrameParser()
    frames = feed_in_random_chunks(parser, b"".join(raw) + bytes(80), rng)

    expected = [i for i in range(1000) if i not in corrupted]
    assert [f.seq for f in frames] == expected
    assert parser.stats.dropped_packets == len(corrupted)
