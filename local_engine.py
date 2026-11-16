"""faster-whisper adapter for LOCAL dictation — lazy imports (D8: torch FORBIDDEN).

WHY this shape: this module must IMPORT CLEANLY on machines WITHOUT the ML
extras: no top-level ``faster_whisper``/``numpy``/``ctranslate2`` import ever
runs.  The backend loads inside ``start()``; a missing extra raises
``EngineUnavailableError`` with an actionable message so LOCAL is hidden WITH
a reason instead of crashing at record time (D8).

D7 contract: models load ONLY from the verified local directory —
faster-whisper's Hugging Face auto-download is bypassed entirely by passing an
explicit directory, and an absent/unready model raises
``ModelNotDownloadedError`` (download lives in Settings/model_store, Phase 5 —
this adapter never fetches).

Numbers-only logging (D11): RTF ratios and durations only, never audio or
transcript content.
"""

import io
import logging
import os
import time
from collections import deque
from typing import Optional, Tuple

from dictation_types import EmissionOut, SessionConfig, SegmentIn, WordSpan
from llm_errors import EngineUnavailableError, ModelNotDownloadedError
from pcm_stream import pcm16_to_wav_bytes
from vad_segmenter import trim_word_timings

logger = logging.getLogger(__name__)

ENGINE_ID = "local"
MODEL_IDS = {
    "fast": "faster-whisper-base",       # A6 s3a table
    "standard": "faster-whisper-small",
    "auto": "faster-whisper-small",      # may DOWNGRADE to base via the probe (D12)
}
BASE_MODEL_ID = "faster-whisper-base"
_EXTRAS_MESSAGE = (
    "Local dictation needs the optional ML extras (faster-whisper, ctranslate2). "
    "Install them with: pip install -r requirements-ml.txt, then restart the app."
)

RTF_WINDOW = 8                            # measured-RTF samples (D12 adaptive cadence)


def _load_backend():
    """Lazy backend resolution (D8): returns the WhisperModel class or raises
    ``EngineUnavailableError`` with an actionable reason — NEVER an ImportError."""
    try:
        from faster_whisper import WhisperModel  # optional extras (requirements-ml.txt)
    except ImportError as error:
        raise EngineUnavailableError(
            _EXTRAS_MESSAGE, engine_id=ENGINE_ID, reason="faster-whisper not installed"
        ) from error
    return WhisperModel


def capability_probe() -> Tuple[bool, str]:
    """(capable_for_small_tier, reason) — the D12 CAPABILITY probe.

    Honest by design: it asks ctranslate2 what the CPU actually supports and
    NEVER claims AVX2/ISA facts (unverified folklore); a missing extra or a
    failed probe lands conservatively in the fast/base tier with a reason.
    """
    try:
        import ctranslate2
    except ImportError:
        return False, "Optional ML extras are not installed."
    try:
        compute_types = ctranslate2.get_supported_compute_types("cpu")
    except Exception:                      # probe failure must never crash startup
        return False, "CPU compute probe failed; using the fast model tier."
    if "int8" in compute_types:
        return True, ""
    return False, "This CPU lacks int8 support; using the fast model tier."


def resolve_model_id(model_size) -> str:
    """model_size -> manifest model_id (A6 s3a): fast=base, standard=small,
    auto=small ONLY when the capability probe says the machine keeps up (D12:
    probe, not ISA claims — conservative base otherwise).  Accepts the size
    STRING or a ``SessionConfig``-like object carrying ``model_size``."""
    size = model_size if isinstance(model_size, str) else getattr(model_size, "model_size", "auto")
    if not isinstance(size, str):
        size = "auto"
    if size == "fast":
        return MODEL_IDS["fast"]
    if size == "standard":
        return MODEL_IDS["standard"]
    capable, _reason = capability_probe()
    return MODEL_IDS["auto"] if capable else BASE_MODEL_ID


def default_store_dir(configured: object = "") -> str:
    """Platform model-store root (devops s3): configured value wins, else
    ``%LOCALAPPDATA%\\WhisperLive\\models`` / ``$XDG_DATA_HOME/WhisperLive/models``
    else ``~/.local/share/WhisperLive/models`` (DATA dir, NOT config dir)."""
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return os.path.join(base, "WhisperLive", "models")
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "WhisperLive", "models")


def find_model_dir(root: str, model_id: str) -> Optional[str]:
    """Locate a READY model directory (``<root>/<model_id>`` itself or its
    version subdir per the devops layout ``models/<id>/<manifest_version>/``).
    Returns None when absent — NEVER downloads (D7); the caller raises
    ``ModelNotDownloadedError``."""
    target = os.path.join(root, model_id)
    if os.path.isfile(os.path.join(target, "model.bin")):
        return target
    try:
        children = sorted(entry for entry in os.listdir(target)
                          if os.path.isdir(os.path.join(target, entry)))
    except OSError:
        return None
    for child in reversed(children):       # newest/highest version first
        candidate = os.path.join(target, child)
        if os.path.isfile(os.path.join(candidate, "model.bin")):
            return candidate
    return None


class LocalEngine:
    """SegmentEngine over faster-whisper (A6 s3a): ``word_timestamps=True``,
    independent units (``condition_on_previous_text=False`` — no drift across
    segments), measured RTF hook (D12), one model instance per session."""

    def __init__(self, *, store_dir: object = ""):
        self._store_dir = store_dir
        self._model = None
        self._model_id = ""
        self._rtf_samples = deque(maxlen=RTF_WINDOW)

    @property
    def model_id(self) -> str:
        return self._model_id

    def start(self, config: SessionConfig) -> None:
        if self._model is not None:
            return
        WhisperModel = _load_backend()
        model_id = resolve_model_id(config)
        root = default_store_dir(self._store_dir if isinstance(self._store_dir, str) else "")
        directory = find_model_dir(root, model_id)
        if directory is None:
            # D7: never fetch here — Settings/model_store owns the download.
            raise ModelNotDownloadedError(
                f"The local model '{model_id}' is not downloaded yet. "
                "Open Settings > Models to download it before recording.",
                model_id=model_id,
            )
        self._model = WhisperModel(directory, device="cpu", compute_type="int8")
        self._model_id = model_id
        logger.info("Local model loaded (model_id=%s)", model_id)  # ids only (D11)

    def process(self, segment: SegmentIn) -> list:
        if self._model is None:
            raise EngineUnavailableError(
                "Local engine was not started.", engine_id=ENGINE_ID, reason="not started"
            )
        # WAV in memory (V4: transcribe accepts file-like; no numpy in THIS code)
        wav_source = io.BytesIO(pcm16_to_wav_bytes(segment.pcm16))
        started = time.monotonic()
        # Language: AUTO-DTECT the spoken language — translation belongs to the
        # cleanup stage (D2), matching classic stage-1 semantics (raw, spoken
        # language; "Original language" path of A6 s3a).
        segment_iter, _info = self._model.transcribe(
            wav_source,
            word_timestamps=True,
            beam_size=5,
            vad_filter=False,                     # our own Silero segmentation ran already
            condition_on_previous_text=False,     # independent merged units — no drift
            language=None,
        )
        raw_words = []
        texts = []
        for piece in segment_iter:                # generator MUST be consumed here (V4)
            texts.append(piece.text or "")
            for word in (getattr(piece, "words", None) or ()):
                raw_words.append(word)
        decode_s = time.monotonic() - started
        if segment.audio_seconds > 0:
            self._rtf_samples.append(decode_s / segment.audio_seconds)
            logger.info("Local decode RTF=%.3f audio=%.2fs",  # numbers only (D11)
                        decode_s / segment.audio_seconds, segment.audio_seconds)
        text = "".join(texts).strip()
        if not text:
            return []
        # D5 midpoint trim: words carry over ONLY when their session-shifted
        # midpoint falls inside this unit's disjoint span (never fabricated).
        kept = trim_word_timings(
            raw_words,
            unit_offset_s=segment.speech_start_s,
            speech_start_s=segment.speech_start_s,
            speech_end_s=segment.speech_end_s,
        )
        words = tuple(
            WordSpan(start_ms=int(word.start * 1000), end_ms=int(word.end * 1000),
                     text=str(getattr(word, "word", "")))
            for word in kept if hasattr(word, "start") and hasattr(word, "end")
        )
        return [EmissionOut(text, segment_seq=segment.segment_seq, kind="phrase",
                            is_final=True, words=words)]

    def estimated_rtf(self) -> Optional[float]:
        """Measured decode-cost hook for LA-2/adaptive cadence (D12); None
        until at least one decode completed (never a fabricated number)."""
        if not self._rtf_samples:
            return None
        return sum(self._rtf_samples) / len(self._rtf_samples)

    def close(self) -> None:
        model = self._model
        self._model = None
        if model is not None:
            try:
                model.close()
            except Exception:                # teardown must never raise
                logger.warning("Local model close failed", exc_info=True)
