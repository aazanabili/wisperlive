"""Silero-VAD segmentation over raw ONNX — session INJECTED (D8: torch FORBIDDEN).

WHY: speech units must be cut at VAD boundaries so consecutive segments own
DISJOINT audio spans; word timestamps are then trimmed by midpoint against the
unit's own span (D5) — NO blind 1 s overlap, never fabricated timings.

Design contract:
* This module NEVER downloads or opens model files (``model_store`` is Phase 5)
  and NEVER imports ``torch`` (D8) or ``onnxruntime``: the session is an
  INJECTED dependency (production passes an ``onnxruntime.InferenceSession``
  over the manifest-verified ``silero_vad.onnx``; tests pass fakes).
* The default input-feed builder INTROSPECTS ``session.get_inputs()`` so any
  silero variant's input names/state shapes work without hardcoded tensor
  layouts; an ``encoder`` override covers exotic graphs.
* Pure core: stdlib only (``struct``/``dataclasses``/``typing``), no disk, no
  network, no clock reads — byte offsets are derived from bytes fed, not time.
"""

import struct
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

SAMPLE_RATE = 16000
WINDOW_SAMPLES = 512                       # silero decode window @16 kHz (32 ms)
WINDOW_BYTES = WINDOW_SAMPLES * 2          # s16le mono
DEFAULT_THRESHOLD = 0.5                    # speech probability cutoff
DEFAULT_ONSET_WINDOWS = 3                  # ~96 ms above threshold to OPEN a unit
DEFAULT_PAUSE_WINDOWS = 16                  # ~0.5 s below threshold to CLOSE it (D5 cut)
MAX_UNIT_S = 30.0                          # merge cap per unit (A6 s2.2)
MAX_MERGE_PAUSE_S = 2.0                    # a closed unit waits this long for a tail merge
_FLOATS_PER_SAMPLE = 1.0 / 32768.0


@dataclass(frozen=True)
class RawSpan:
    """Absolute byte range of ONE speech span inside the session stream."""

    start_byte: int
    end_byte: int

    @property
    def duration_s(self) -> float:
        return (self.end_byte - self.start_byte) / (SAMPLE_RATE * 2)


def window_floats(pcm16_window: bytes) -> list:
    """s16le window -> normalized float list (pure; feeds the injected model)."""
    samples = struct.unpack("<%dh" % (len(pcm16_window) // 2), pcm16_window)
    return [sample * _FLOATS_PER_SAMPLE for sample in samples]


def _zeros(shape) -> list:
    """Nested zeros for a declared state shape (bounds: dims clamped, depth 4)."""
    if len(shape) > 4:
        return 0.0
    size = shape[0] if isinstance(shape[0], int) and 0 < shape[0] <= 4096 else 1
    if not shape[1:]:
        return [0.0] * size
    return [_zeros(shape[1:]) for _ in range(size)]


def default_feed(session, pcm16_window: bytes, sample_rate: int) -> dict:
    """Build the model feed from the SESSION's own declared inputs so silero
    variants (x/h/c/sr vs x/h/sr ...) need no hardcoded layout: ``x``-like
    inputs get the window, ``sr``-like inputs the rate, every remaining input
    (hidden state) zeros of its declared shape (first-call state init — silero
    carries state per call, not across calls, for our windowed cadence)."""
    if not hasattr(session, "get_inputs"):
        return {"x": [window_floats(pcm16_window)]}
    feed = {}
    for node in session.get_inputs():
        name = getattr(node, "name", "")
        shape = list(getattr(node, "shape", []) or [])
        lowered = name.lower()
        if lowered in ("x", "input", "audio", "speech"):
            feed[name] = [window_floats(pcm16_window)]
        elif lowered in ("sr", "sample_rate", "sampling_rate"):
            feed[name] = sample_rate
        else:
            feed[name] = _zeros(shape)
    return feed or {"x": [window_floats(pcm16_window)]}


def _probability(output) -> float:
    """Normalize any run() output ([[p]] / [p] / numpy scalars) to a float in
    [0, 1]; unusable output scores 0.0 (never raises out of the audio path)."""
    value = output
    while True:
        if hasattr(value, "tolist"):                 # numpy arrays/scalars
            value = value.tolist()
        if isinstance(value, (list, tuple)):
            if not value:
                return 0.0
            value = value[0]
            continue
        break
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if score < 0.0 else (1.0 if score > 1.0 else score)


class VadSegmenter:
    """Pause detector over an injected session: feed capture blocks, receive
    completed absolute ``RawSpan``s (audio bytes stay in the ``PcmRing`` — this
    object only tracks offsets, never payload).

    ``min_segment_s`` (per-engine, D5/A6 s3b) holds a just-closed short unit
    OPEN so the tail merges into the next speech run; a pause longer than
    ``MAX_MERGE_PAUSE_S`` flushes it anyway (the ledger's billing floor covers
    the short tail), and units force-split at ``MAX_UNIT_S``.
    """

    def __init__(self, session, *, threshold: float = DEFAULT_THRESHOLD,
                 onset_windows: int = DEFAULT_ONSET_WINDOWS,
                 pause_windows: int = DEFAULT_PAUSE_WINDOWS,
                 min_segment_s: float = 0.0,
                 sample_rate: int = SAMPLE_RATE,
                 encoder: Optional[Callable[[bytes], dict]] = None):
        self._session = session
        self._threshold = float(threshold)
        self._onset_windows = max(1, int(onset_windows))
        self._pause_windows = max(1, int(pause_windows))
        self._min_bytes = max(0.0, float(min_segment_s)) * sample_rate * 2
        self._sample_rate = int(sample_rate)
        self._window_bytes = WINDOW_SAMPLES * 2
        self._encoder = encoder
        # Offset state (all absolute byte offsets — no clock involved).
        self._window_buf = bytearray()
        self._stream_offset = 0      # bytes fed so far (next unconsumed byte)
        self._unit_start: Optional[int] = None
        self._first_hit: Optional[int] = None
        self._onset_hits = 0
        self._pause_hits = 0
        self._pause_run_start: Optional[int] = None

    @property
    def bytes_fed(self) -> int:
        return self._stream_offset

    def feed(self, block: bytes) -> Tuple[RawSpan, ...]:
        """Consume one capture block; return spans completed WITHIN it."""
        self._stream_offset += len(block)
        self._window_buf.extend(block)
        completed = []
        while len(self._window_buf) >= self._window_bytes:
            window = bytes(self._window_buf[:self._window_bytes])
            del self._window_buf[:self._window_bytes]
            window_start = self._stream_offset - len(self._window_buf) - self._window_bytes
            span = self._on_window(window, window_start)
            if span is not None:
                completed.append(span)
        return tuple(completed)

    def _on_window(self, window: bytes, window_start: int) -> Optional[RawSpan]:
        if self._encoder is not None:
            feed = self._encoder(window)
        else:
            feed = default_feed(self._session, window, self._sample_rate)
        score = _probability(self._session.run(None, feed))
        speaking = score >= self._threshold
        span = None
        if speaking:
            self._pause_hits = 0
            self._pause_run_start = None
            if self._unit_start is None:
                # Onset tracking: remember where the probable-speech run began
                # so the OPENED unit starts at first speech, not at confirmation.
                if self._first_hit is None:
                    self._first_hit = window_start
                self._onset_hits += 1
                if self._onset_hits >= self._onset_windows:
                    self._unit_start = self._first_hit
                    self._first_hit = None
                    self._onset_hits = 0
            elif window_start - self._unit_start >= MAX_UNIT_S * self._sample_rate * 2:
                # D5/A6 merge cap: force-split a unit at the 30 s bound.
                span = RawSpan(self._unit_start, window_start)
                self._unit_start = window_start
        else:
            self._onset_hits = 0
            self._first_hit = None
            if self._unit_start is None:
                return None
            if self._pause_hits == 0:
                self._pause_run_start = window_start
            self._pause_hits += 1
            pause_bytes = self._pause_hits * self._window_bytes
            pause_s = pause_bytes / (self._sample_rate * 2)
            if pause_bytes >= self._pause_windows * self._window_bytes:
                span = self._close_unit(self._pause_run_start or window_start, pause_s)
        return span

    def _close_unit(self, pause_start: int, pause_s: float) -> Optional[RawSpan]:
        """Close at the pause cut point; hold short units open for a tail merge
        up to MAX_MERGE_PAUSE_S (longer pauses flush — 'sent anyway' per D5/A6)."""
        start = self._unit_start
        if start is None:
            return None
        if pause_s < MAX_MERGE_PAUSE_S and (pause_start - start) < self._min_bytes:
            return None                     # sub-min tail: stay open for a merge
        self._unit_start = None
        if pause_start <= start:
            return None
        return RawSpan(start, pause_start)

    def flush(self) -> Optional[RawSpan]:
        """End of stream: close whatever is open at the current offset (the
        session appends the final candidate regardless of the min size)."""
        span = None
        if self._unit_start is not None and self._stream_offset > self._unit_start:
            end = self._pause_run_start or self._stream_offset
            if end > self._unit_start:
                span = RawSpan(self._unit_start, end)
        self._unit_start = None
        self._first_hit = None
        self._onset_hits = 0
        self._pause_hits = 0
        self._pause_run_start = None
        return span


def trim_word_timings(words, unit_offset_s: float, speech_start_s: float,
                      speech_end_s: float, extract: Optional[Callable] = None):
    """D5/A6 s3a midpoint trim: keep a word iff its MIDPOINT shifted by the
    unit's session offset falls INSIDE the unit's own speech span — seams emit
    each word exactly once across units (NO blind overlap, never fabricated).

    ``extract(word) -> (start_s, end_s)`` defaults to ``.start``/``.end``
    attributes, then ``["start"]``/``["end"]`` keys, then a ``(start, end)``
    sequence; unparseable entries are DROPPED (never guessed).
    """
    if unit_offset_s is None:
        unit_offset_s = 0.0

    def _bounds(word):
        if extract is not None:
            return extract(word)
        if hasattr(word, "start") and hasattr(word, "end"):
            return float(word.start), float(word.end)
        if isinstance(word, dict) and "start" in word and "end" in word:
            return float(word["start"]), float(word["end"])
        try:
            return float(word[0]), float(word[1])
        except (TypeError, ValueError, IndexError, KeyError):
            return None

    kept = []
    for word in words or ():
        bounds = _bounds(word)
        if bounds is None:
            continue
        start, end = bounds
        midpoint = (start + end) / 2.0 + unit_offset_s
        if speech_start_s <= midpoint <= speech_end_s:
            kept.append(word)
    return tuple(kept)
