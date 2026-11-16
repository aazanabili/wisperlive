"""LocalAgreement-2 streaming engine for LIVE dictation (A6 s3c; D12 caps).

WHY this shape: live mode types CONFIRMED phrases while recording.  A rolling
tail buffer is decoded through the LOCAL engine seam and only the word-level
longest common prefix of TWO consecutive hypotheses (LocalAgreement-2) is ever
emitted, so a hypothesis that flips between decodes is NEVER typed (U6 raw +
honest streaming), and the confirmed audio is scrolled out of the buffer by the
last confirmed word's END timestamp (word-precise — no blind overlap).

Contracts honored here:
- Adapter over the ``dictation_types.LiveStreamEngine`` port
  (``start(config, emit)`` / ``push_audio`` / ``flush`` / ``close``).
- The decoder is INJECTABLE (dev venv has no faster-whisper: tests drive a fake
  through the same seam) and defaults to ``local_engine.LocalEngine`` — its
  lazy ``_load_backend``/model-dir resolution/RTF hook are REUSED, never
  duplicated; this module imports NO ML package at top level (D8).
- D12 CPU cap: only the base/small tiers ever resolve — via the capability
  probe, NEVER an AVX2/ISA claim.  Adaptive cadence: decode cost clamped to
  <= 50 % of the interval (see ``adaptive_interval``).
- RAW output only (U6/D2): engine words verbatim, whitespace-joined — cleanup
  and translation exist only via the manual "Polish last dictation" escape.
- D11: every log line carries sizes/durations/RTF/counts only — never audio.
"""

import logging
import threading
import time
from dataclasses import replace
from typing import Callable, Optional, Tuple

from dictation_types import EmissionOut, SessionConfig, SegmentIn, WordSpan
from local_engine import BASE_MODEL_ID, MODEL_IDS, LocalEngine, capability_probe

logger = logging.getLogger(__name__)

BYTES_PER_S = 16000 * 2                 # 16 kHz mono s16le (pcm_stream contract)
MIN_INTERVAL = 0.5                      # D12 clamp: adaptive cadence floor (annex)
MAX_INTERVAL = 3.0                      # D12 clamp: adaptive cadence ceiling
DEFAULT_INTERVAL = 1.0                  # first cadence, before any RTF exists
MAX_TAIL_S = 30.0                       # A6 s3c: bounded tail -> force confirm-all
FLUSH_TIMEOUT_S = 30.0                  # bounded synchronous confirm-all (A6 s2.5)
CLOSE_JOIN_S = 5.0                      # bounded decode-thread join (A6 s2.5)
_LIVE_SIZES = ("fast", "standard", "auto")
_TIER_OF = {BASE_MODEL_ID: "fast", MODEL_IDS["standard"]: "standard"}


def live_model_id(model_size, probe=None) -> str:
    """D12 CPU CAP (A6 s3c): live resolves ONLY to ``base``/``small`` — turbo/
    large tiers never resolve and unknown values coerce to ``auto`` (N1: never
    raises).  ``auto``/``standard`` pick small ONLY when the capability probe
    says the CPU keeps up (A6 s3a downgrade — a probe, never an ISA/AVX2
    claim); ``fast`` skips the probe entirely (base is always safe)."""
    size = model_size if model_size in _LIVE_SIZES else "auto"
    if size == "fast":
        return BASE_MODEL_ID
    capable, _reason = (probe or capability_probe)()
    return MODEL_IDS["standard"] if capable else BASE_MODEL_ID


def adaptive_interval(tail_audio_s: float, rtf) -> Optional[float]:
    """D12 HAZARD (adaptive cadence): ``interval = clamp(0.5, 3.0, cost / 0.5)``
    with ``cost = tail_audio_s x rtf_ema`` — a decode may consume at most half
    of its own cadence, and the value is re-evaluated after EVERY decode.
    Returns None while the RTF is unmeasured: the caller keeps its current
    cadence (never a fabricated number — honest unknown)."""
    if isinstance(rtf, (int, float)) and rtf > 0 and tail_audio_s > 0:
        return max(MIN_INTERVAL, min(MAX_INTERVAL, (tail_audio_s * float(rtf)) / 0.5))
    return None


def _norm(word: WordSpan) -> str:
    return word.text.strip().casefold()


def _lcp(prev: Tuple[WordSpan, ...], hyp: Tuple[WordSpan, ...]) -> Tuple[WordSpan, ...]:
    """LA-2 word-level longest common prefix: a prefix is CONFIRMED only when
    two consecutive hypotheses agree on it — diverging words stay untyped."""
    limit = min(len(prev), len(hyp))
    count = 0
    while count < limit and _norm(prev[count]) == _norm(hyp[count]):
        count += 1
    return hyp[:count]


class LiveLocalEngine:
    """``LiveStreamEngine`` over LA-2: rolling tail buffer, LCP confirmation,
    word-timestamp scroll, RTF-adaptive cadence, base/small CPU cap (D12).

    Threading: ONE internal decode thread owns all confirm/emit work (emissions
    are therefore strictly serialized); ``flush`` only signals it and waits
    (bounded), and ``close`` stops it with a bounded join (A6 s2.5/M2).
    """

    def __init__(self, *, decoder=None, store_dir: str = ""):
        self._decoder = decoder                # injected seam = test/dev fake
        self._store_dir = store_dir
        self._emit: Optional[Callable[[EmissionOut], None]] = None
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._tail = bytearray()               # audio since the last confirm
        self._prev: Tuple[WordSpan, ...] = ()   # H_(n-1), tail-relative words
        self._interval = DEFAULT_INTERVAL
        self._running = False
        self._flush_request = 0
        self._flush_done = 0
        self._thread: Optional[threading.Thread] = None
        self._clock = time.monotonic

    @property
    def decode_interval(self) -> float:
        """Current adaptive cadence in seconds (numbers only — D12 observability)."""
        with self._lock:
            return self._interval

    # ---------------- LiveStreamEngine port ---------------------------------
    def start(self, config: SessionConfig, emit: Callable[[EmissionOut], None]) -> None:
        """Arm the LA-2 loop.  The decoder loads LAZILY through ``local_engine``
        (D8: import-clean without the ML extras — a missing extra/model raises
        its typed error HERE, before any audio, so U1 fail-closed holds)."""
        if self._running:
            return
        if self._decoder is None:
            self._decoder = LocalEngine(store_dir=self._store_dir)
        tier = _TIER_OF[live_model_id(config.model_size)]   # D12: base/small ONLY
        self._decoder.start(replace(config, model_size=tier))
        self._emit = emit
        with self._cond:
            self._running = True
        self._thread = threading.Thread(target=self._loop, name="la2-decode", daemon=True)
        self._thread.start()

    def push_audio(self, pcm16: bytes) -> None:
        """Append captured audio to the tail (never blocks the capture feeder)."""
        if not pcm16:
            return
        with self._cond:
            if not self._running:
                return
            self._tail += bytes(pcm16)
            self._cond.notify()

    def flush(self) -> None:
        """Force confirm-all NOW (VAD speech-end / stop — A6 s3c): returns only
        after the tail was decoded and the final emission delivered (bounded
        wait), so the session can never finish ahead of its last phrase."""
        with self._cond:
            if not self._running:
                return
            self._flush_request += 1
            want = self._flush_request
            self._cond.notify()
            deadline = self._clock() + FLUSH_TIMEOUT_S
            while self._flush_done < want and self._running:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    logger.warning("LA-2 flush timed out")   # numbers only (D11)
                    break
                self._cond.wait(timeout=remaining)

    def close(self) -> None:
        """Stop the decode thread (bounded join) + close the decoder model.
        Idempotent; the session calls it on finish AND abort so a stale decode
        thread never outlives its session (M2/A6 s2.5)."""
        with self._cond:
            self._running = False
            self._cond.notify_all()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=CLOSE_JOIN_S)
        if self._decoder is not None:
            try:
                self._decoder.close()
            except Exception:                      # teardown never raises
                logger.warning("LA-2 decoder close failed", exc_info=True)
        self._emit = None

    # ---------------- decode thread -----------------------------------------
    def _loop(self) -> None:
        """Cadence thread: a CONDITION-DRIVEN timer (D12: no blind sleeps) —
        it waits until ``due``, waking early on push/flush, then decodes."""
        due = self._clock() + self._interval
        while True:
            with self._cond:
                while True:
                    if not self._running:
                        return
                    confirm_all = self._flush_request > self._flush_done
                    if confirm_all:
                        break
                    remaining = due - self._clock()
                    if remaining <= 0:
                        if self._tail:
                            break                  # cadence reached with audio
                        due = self._clock() + self._interval   # idle: no cadence debt
                    self._cond.wait(timeout=(due - self._clock()) if self._tail else None)
                snapshot = bytes(self._tail)        # copy: decode OUTSIDE the lock
            hyp = self._decode(snapshot)            # D12 hazard: never hold locks across a decode
            with self._cond:
                if not self._running:
                    return                          # closed mid-decode: drop the result
                try:
                    self._advance(hyp, confirm_all=confirm_all)
                except Exception:
                    logger.exception("LA-2 confirm step failed")   # types only (D11)
                finally:
                    if confirm_all:                 # always wake the flush waiter
                        self._flush_done = self._flush_request
                        self._cond.notify_all()
            due = self._clock() + self._interval

    def _decode(self, snapshot: bytes) -> Tuple[WordSpan, ...]:
        """One decode through the LOCAL seam (``SegmentIn -> EmissionOut``).
        Returns the word-level hypothesis; ``()`` = heard nothing or the decode
        failed (logged, tail kept and bounded by the cap).  Word timings are
        REQUIRED for a precise scroll, so text without timings is confirmed
        only via ``hyp or _prev`` at flush/cap — never fabricated mid-stream."""
        if not snapshot or self._decoder is None:
            return ()
        seconds = len(snapshot) / BYTES_PER_S
        try:
            emissions = self._decoder.process(SegmentIn(
                segment_seq=-1, pcm16=snapshot, speech_start_s=0.0,
                speech_end_s=seconds, audio_seconds=seconds)) or []
        except Exception:
            logger.exception("LA-2 decode failed")   # engine defect: cap bounds the tail
            return ()
        for emission in emissions:
            if emission.words:
                return tuple(emission.words)
        return ()

    def _advance(self, hyp: Tuple[WordSpan, ...], *, confirm_all: bool) -> None:
        """LA-2 step (caller holds the lock): confirm -> emit -> SCROLL the tail
        through the last confirmed word's END timestamp (A6 s3c: precise, no
        blind overlap).  Flush/VAD confirm-all prefers the CURRENT hypothesis
        and falls back to the last agreed remainder — engine output only."""
        if confirm_all:
            confirmed, final = hyp or self._prev, True
        else:
            confirmed, final = _lcp(self._prev, hyp), False
            if not confirmed and len(self._tail) / BYTES_PER_S >= MAX_TAIL_S:
                confirmed = hyp                    # D5/D12 cap: force confirm-all
        if confirmed:
            self._emit_confirmed(confirmed, hyp or confirmed, final=final)
        else:
            # LA-2 HAZARD: compare CONSECUTIVE hypotheses over the SAME tail —
            # a non-confirming decode still replaces _prev, so a flip can never
            # confirm against a stale, non-adjacent hypothesis.
            self._prev = hyp
            if len(self._tail) / BYTES_PER_S >= MAX_TAIL_S and not hyp:
                # Cap + EMPTY hypothesis (engine heard nothing): trim the oldest
                # half — bounded memory without inventing text (D5).
                del self._tail[: len(self._tail) // 2]
                logger.info("LA-2 tail trimmed at cap tail_s=%.1f",
                            len(self._tail) / BYTES_PER_S)
        interval = adaptive_interval(len(self._tail) / BYTES_PER_S, self._rtf())
        if interval is not None:
            self._interval = interval              # D12: re-evaluated after every decode

    def _emit_confirmed(self, confirmed, rest, *, final: bool) -> None:
        """RAW emission (U6/D2: engine words verbatim, whitespace-joined only —
        no cleanup/translation ever) + the tail scroll; single decode thread."""
        text = " ".join(word.text.strip() for word in confirmed if word.text.strip())
        if not text:
            return                                 # never emit or scroll empties
        drop = min(len(self._tail), confirmed[-1].end_ms * BYTES_PER_S // 1000)
        del self._tail[:drop]                      # scroll: audio drops with the words
        keep_ms = drop * 1000 // BYTES_PER_S
        self._prev = tuple(                        # surviving words, rebased to the tail
            replace(word, start_ms=word.start_ms - keep_ms, end_ms=word.end_ms - keep_ms)
            for word in rest if word.start_ms >= keep_ms)
        emit = self._emit
        if emit is not None:
            try:
                emit(EmissionOut(text=text, segment_seq=-1, kind="phrase",
                                 is_final=final, words=tuple(confirmed)))
            except Exception:                      # a callback defect never kills LA-2
                logger.warning("LA-2 emit callback failed", exc_info=True)
        logger.info("LA-2 confirmed words=%d final=%s tail_s=%.2f",  # D11: numbers only
                    len(confirmed), final, len(self._tail) / BYTES_PER_S)

    def _rtf(self):
        """Measured RTF from the decoder seam (D12); None while unmeasured."""
        getter = getattr(self._decoder, "estimated_rtf", None)
        return getter() if callable(getter) else None
