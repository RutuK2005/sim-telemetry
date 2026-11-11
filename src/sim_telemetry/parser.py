"""Streaming frame parser that survives corrupted, split and missing data.

Serial links deliver bytes, not messages: one ``read()`` can return half a
frame, three frames, or line noise. :class:`FrameParser` keeps an internal
buffer, finds frames in it, verifies their CRC and reports what went wrong.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .protocol import (
    CRC_SIZE,
    CRC_STRUCT,
    HEADER_SIZE,
    HEADER_STRUCT,
    MAX_PAYLOAD_SIZE,
    PAYLOAD_SIZE,
    SEQ_MODULO,
    SYNC,
    Frame,
    crc16_ccitt,
    decode_payload,
)


@dataclass(slots=True)
class ParserStats:
    """Running counters describing link health."""

    frames_ok: int = 0
    crc_errors: int = 0
    bad_length: int = 0
    unsupported_payload: int = 0
    bytes_skipped: int = 0
    dropped_packets: int = 0
    seq_resets: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


class FrameParser:
    """Incremental decoder: call :meth:`feed` with any chunk of bytes, get frames back."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._last_seq: int | None = None
        self.stats = ParserStats()

    def feed(self, data: bytes | bytearray) -> list[Frame]:
        """Append ``data`` to the buffer and return every complete, valid frame found."""
        self._buffer.extend(data)
        frames: list[Frame] = []
        while True:
            frame = self._next_frame()
            if frame is None:
                return frames
            frames.append(frame)

    def _skip(self, count: int) -> None:
        del self._buffer[:count]
        self.stats.bytes_skipped += count

    def _next_frame(self) -> Frame | None:
        buf = self._buffer
        while True:
            # 1. Align the buffer so it starts with the sync word.
            start = buf.find(SYNC)
            if start < 0:
                # Keep the last byte: it may be the first half of a sync word.
                if len(buf) > 1:
                    self._skip(len(buf) - 1)
                return None
            if start > 0:
                self._skip(start)

            # 2. Wait for a full header, then sanity-check the length byte.
            if len(buf) < HEADER_SIZE:
                return None
            length, seq, timestamp_ms = HEADER_STRUCT.unpack_from(buf, len(SYNC))
            if length > MAX_PAYLOAD_SIZE:
                self.stats.bad_length += 1
                self._skip(1)  # this "sync" was a false match; search again after it
                continue

            # 3. Wait for the whole frame, then verify the CRC.
            frame_len = HEADER_SIZE + length + CRC_SIZE
            if len(buf) < frame_len:
                return None
            body = memoryview(buf)[len(SYNC) : frame_len - CRC_SIZE]
            (received_crc,) = CRC_STRUCT.unpack_from(buf, frame_len - CRC_SIZE)
            crc_ok = crc16_ccitt(body) == received_crc
            payload = bytes(body[HEADER_STRUCT.size :])
            body.release()  # a live memoryview would block resizing the bytearray
            if not crc_ok:
                # Skip only one byte: a real frame may start inside the bytes we rejected.
                self.stats.crc_errors += 1
                self._skip(1)
                continue

            del buf[:frame_len]
            if length != PAYLOAD_SIZE:
                # Valid frame, but a payload layout this host does not understand.
                self.stats.unsupported_payload += 1
                continue

            self._track_sequence(seq)
            self.stats.frames_ok += 1
            return Frame(seq=seq, timestamp_ms=timestamp_ms, sample=decode_payload(payload))

    def _track_sequence(self, seq: int) -> None:
        if self._last_seq is not None:
            gap = (seq - self._last_seq - 1) % SEQ_MODULO
            if gap < SEQ_MODULO // 2:
                self.stats.dropped_packets += gap
            else:
                # The sequence went backwards: most likely the device rebooted.
                self.stats.seq_resets += 1
        self._last_seq = seq
