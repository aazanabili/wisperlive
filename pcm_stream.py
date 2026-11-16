"""In-memory PCM capture plumbing: bounded block feed + byte-bounded ring.

WHY: the dictation modes keep audio IN MEMORY only (D10 — no temp WAV for the
new paths) under two hard bounds: the pre-segment block queue is BOUNDED with
drop-NEWEST so the PortAudio callback never blocks (A6 s2.2: 256 x ~20 ms
blocks ~ 5 s), and the ring is byte-bounded at 32 MiB with drop-OLDEST plus a
counted, reportable overflow (devops U.3 / D5 — NEVER silent loss: the session
surfaces ``overflowed`` as a degraded notice).  The classic temp-WAV flow in
``audio_recorder`` stays untouched for the classic path (D1).

Tier: pure-ish data structures (stdlib ``collections``/``queue``/``threading``
only) — no disk, no audio-library imports.  Capture lives in
``audio_recorder``'s block-callback stream mode; orchestration (who reads whom)
lives in ``dictation_session``.  Threading contract: ONE producer + ONE consumer
per structure (PortAudio thread offers, segmenter thread takes and owns the
ring's sole writer/reader); counters are lock-guarded, buffer access relies on
that single-owner discipline.
"""

import queue
import threading
import wave
from collections import deque
from io import BytesIO

SAMPLE_RATE = 16000
CHANNELS = 1
BLOCK_MS = 20                                    # PortAudio callback cadence (A6 s2.2)
BLOCK_SAMPLES = SAMPLE_RATE * BLOCK_MS // 1000   # 320
BLOCK_BYTES = BLOCK_SAMPLES * 2                  # s16le mono = 640 B
MAX_PCM_RING_BYTES = 32 * 1024 * 1024            # D10/devops HARD memory ceiling
BLOCK_QUEUE_MAX = 256                            # ~5 s of 20 ms blocks (A6 s2.2)

# End-of-stream marker returned by BlockQueue.take() (never a payload).
END_OF_STREAM = object()


class BlockQueue:
    """Bounded feed between the capture callback and the segmenter.

    ``offer`` runs on the PortAudio thread: it NEVER blocks — on overflow the
    NEWEST block is dropped and counted (drop-NEWEST, A6 s2.2); the session
    turns a non-zero ``dropped_blocks`` into a degraded notice (D5: never
    silent loss).  ``take`` runs on the segmenter thread with a timeout and
    reports ``END_OF_STREAM`` once ``close()`` has drained the tail.
    """

    def __init__(self, maxsize: int = BLOCK_QUEUE_MAX):
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, int(maxsize)))
        self._closed = False
        self._counter_lock = threading.Lock()
        self.dropped_blocks = 0

    def offer(self, block) -> bool:
        """Callback side: False means the block was dropped under backpressure
        (counted, never silent, never blocking the audio thread)."""
        payload = bytes(block) if isinstance(block, (bytearray, memoryview)) else block
        if not isinstance(payload, bytes):
            return False
        try:
            self._queue.put_nowait(payload)
            return True
        except queue.Full:
            with self._counter_lock:
                self.dropped_blocks += 1
            return False

    def take(self, timeout: float = 0.25):
        """Segmenter side: ``bytes`` | ``END_OF_STREAM`` | ``None`` (timeout)."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return END_OF_STREAM if self._closed else None

    def close(self) -> None:
        """Mark end-of-stream; the tail already queued drains first (a full
        queue still reports END_OF_STREAM once observed empty)."""
        self._closed = True
        try:
            self._queue.put_nowait(END_OF_STREAM)
        except queue.Full:
            pass

    @property
    def closed(self) -> bool:
        return self._closed


class PcmRing:
    """Byte-bounded s16le ring behind ``Segment.audio_ref`` (D10/devops U.3).

    Append-only; ``read_range`` COPIES an absolute byte span for dispatch
    (engines own their ``SegmentIn.pcm16`` bytes — workers never touch the
    ring).  Under the ceiling the OLDEST blocks fall off with a counted
    ``dropped_bytes`` and ``overflowed`` flag: the caller must surface that as
    a degraded notice (\"audio dropped under load\") — never a silent loss.
    """

    def __init__(self, max_bytes: int = MAX_PCM_RING_BYTES):
        self.max_bytes = max(1, int(max_bytes))
        self._blocks: deque = deque()
        self._retained = 0
        self._base_offset = 0      # absolute byte offset of the first retained byte
        self._write_offset = 0     # absolute offset AFTER the last append (write cursor)
        self.dropped_bytes = 0

    @property
    def write_offset(self) -> int:
        return self._write_offset

    @property
    def overflowed(self) -> bool:
        return self.dropped_bytes > 0

    @property
    def retained_bytes(self) -> int:
        return self._retained

    def append(self, block: bytes) -> None:
        block = bytes(block)
        if not block:
            return
        self._blocks.append(block)
        self._retained += len(block)
        self._write_offset += len(block)
        while self._retained > self.max_bytes and self._blocks:
            oldest = self._blocks.popleft()
            self._retained -= len(oldest)
            self._base_offset += len(oldest)
            self.dropped_bytes += len(oldest)

    def read_range(self, start: int, end: int) -> bytes:
        """Copy ``[start, end)`` absolute bytes; ranges evicted by overflow
        return only the surviving part (the overflow flag carries the loss)."""
        if end <= start or not self._blocks:
            return b""
        parts = []
        cursor = self._base_offset
        for block in self._blocks:
            block_end = cursor + len(block)
            if block_end <= start:
                cursor = block_end
                continue
            if cursor >= end:
                break
            low = max(0, start - cursor)
            high = min(len(block), end - cursor)
            parts.append(block[low:high])
            cursor = block_end
        return b"".join(parts)

    def clear(self) -> None:
        self._blocks.clear()
        self._retained = 0
        # Offsets intentionally keep counting: spans are session-scoped and a
        # cleared ring belongs to a torn-down session (fresh session = fresh ring).


def pcm16_to_wav_bytes(pcm16: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    """s16le mono PCM -> in-memory WAV container (stdlib ``wave``, NO disk).

    Shared by ``local_engine`` (faster-whisper accepts a file-like) and
    ``whisper_http`` (multipart file part) so both wire paths see identical
    bytes (V1/V4-verified input shapes).
    """
    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(2)                      # s16
        wav.setframerate(sample_rate)
        wav.writeframes(bytes(pcm16))
    return buffer.getvalue()
