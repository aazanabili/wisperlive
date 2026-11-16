"""Chunked cloud engine — D1: the classic two-stage pipeline IS this engine.

WHY: war-room ruling D1 retains the shipped pipeline as the CHUNKED mode
implementation (never deleted): for chat-completions preset engines ``process``
reuses ``llm_api.transcribe_audio`` + ``llm_api.post_process_text`` VERBATIM
(temp-WAV, caller-deletes — the classic contract the tests pin); for
``groq-whisper`` it drives the new multipart transport in-memory.

Cleanup policy (D2): ``cleanup_mode="cloud"`` runs the existing
``post_process_text`` (translation preserved); ``"off"`` emits raw;
``"local-ollama"`` (Phase 8) reuses the SAME chat path against the Ollama
endpoint behind the shared ``polish_engine.require_ollama`` presence gate.
A cleanup failure NEVER silently falls back to raw — it raises, and the
session dispositions that unit through the degraded path (pipeline invariant).

Per-engine minimum segment sizes (D5) are enforced by the SEGMENTER merge;
``min_segment_seconds`` publishes the table the session feeds into it.
"""

import logging
import os
import tempfile
from typing import Tuple

from dictation_types import EmissionOut, SessionConfig, SegmentIn
from llm_errors import EngineUnavailableError, LLMConfigError
import llm_api
import pcm_stream
import providers
import whisper_http
from llm_payloads import groq_language
from polish_engine import require_ollama
from vad_segmenter import trim_word_timings

logger = logging.getLogger(__name__)

# D5 per-engine minimums (war-room: Groq bills a 10 s floor — V1 verified;
# Gemini chunked 20 s; MiMo 5 s).  Keyed by the RESOLVED chunked engine id.
_MIN_SEGMENT_S = {
    "groq-whisper": 10.0,
    "google-gemini": 20.0,
    "xiaomi-mimo": 5.0,
}


def min_segment_seconds(chunked_engine_id: object) -> float:
    """Minimum merged-unit length for this engine (0.0 = VAD boundaries only)."""
    return _MIN_SEGMENT_S.get(chunked_engine_id, 0.0) if isinstance(chunked_engine_id, str) else 0.0


def quota_engine_id(config: SessionConfig) -> str:
    """D6 engine vocabulary (A3 R1): ``groq-whisper`` or ``chat:<provider>``."""
    if config.chunked_engine == "groq-whisper":
        return "groq-whisper"
    return f"chat:{config.chunked_engine or config.provider_id}"


class ChunkedEngine:
    """SegmentEngine for ``dictation_mode="chunked"`` (A6 s3b)."""

    def __init__(self, *, temp_dir: str = ""):
        self._config: SessionConfig | None = None
        self._temp_dir = temp_dir
        self._groq = False

    def start(self, config: SessionConfig) -> None:
        self._config = config
        self._groq = config.chunked_engine == "groq-whisper"
        if self._groq and not config.api_keys.get("groq"):
            # Preflight should have caught this; fail closed again here (U4/D6 —
            # never dispatch a billable request without its credential).
            raise LLMConfigError("Add your Groq API key in Settings before using Groq transcription.")

    def close(self) -> None:
        self._config = None

    def _require(self) -> SessionConfig:
        if self._config is None:
            raise EngineUnavailableError("Chunked engine was not started.",
                                         engine_id="chunked", reason="not started")
        return self._config

    def process(self, segment: SegmentIn) -> list:
        config = self._require()
        if self._groq:
            raw, words = self._transcribe_groq(config, segment)
        else:
            raw, words = self._transcribe_chat(config, segment)
        text = self._cleanup(config, raw)
        if not text.strip():
            return []
        return [EmissionOut(text.strip(), segment_seq=segment.segment_seq,
                            kind="phrase", is_final=True, words=words)]

    # --- groq-whisper: NEW multipart transport (in-memory, D1 carve-out) -----
    def _transcribe_groq(self, config: SessionConfig, segment: SegmentIn) -> Tuple[str, tuple]:
        wav = pcm_stream.pcm16_to_wav_bytes(segment.pcm16)
        result = whisper_http.transcribe_wav(
            wav,
            api_key=config.api_keys.get("groq", ""),
            # The dedicated ASR endpoint is Groq's registry URL — never the
            # chat provider's base URL (chunked engine may differ from provider).
            base_url=providers.resolve_base_url("groq"),
            model=config.chunked_asr_model or whisper_http.DEFAULT_MODEL,
            # D5 midpoint trim applies to vendor words exactly like local (no overlap).
            language=groq_language(config.target_language),
        )
        kept = trim_word_timings(
            result.words, unit_offset_s=segment.speech_start_s,
            speech_start_s=segment.speech_start_s, speech_end_s=segment.speech_end_s,
            extract=lambda word: (word.start_ms / 1000.0, word.end_ms / 1000.0),
        )
        return result.text, tuple(kept)

    # --- chat-completions: the CLASSIC pipeline verbatim (D1) ----------------
    def _transcribe_chat(self, config: SessionConfig, segment: SegmentIn) -> Tuple[str, tuple]:
        # D1 keeps the classic FILE contract for this path: create temp WAV,
        # run the untouched llm_api stages, DELETE in finally (caller-deletes).
        fd, path = tempfile.mkstemp(prefix="whisperlive-chunked-", suffix=".wav",
                                    dir=self._temp_dir or None)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(pcm_stream.pcm16_to_wav_bytes(segment.pcm16))
            raw = llm_api.transcribe_audio(
                path,
                config.api_keys.get(config.provider_id, ""),
                config.provider_base_url,
                config.transcription_model,
                dialect=config.dialect,
                target_language=config.target_language,
            )
        finally:
            try:
                os.unlink(path)
            except OSError:
                logger.warning("Could not delete temp file %s", os.path.basename(path))
        return raw, ()

    def _cleanup(self, config: SessionConfig, raw: str) -> str:
        """D2 cleanup: cloud runs the CLASSIC stage 2 (translation preserved); local-ollama
        reuses the SAME chat path against the Ollama endpoint behind the shared presence
        gate; off is raw.  A cleanup failure RAISES (never a silent raw fallback, A6 s3b)."""
        if config.cleanup_mode == "off":
            return raw
        if config.cleanup_mode == "local-ollama":
            # Phase 8: bounded presence GET first (typed, actionable on absence —
            # D6: never a silent cloud fallback); no key; cleanup_model -> post_model.
            return llm_api.post_process_text(
                raw, config.target_language, "", require_ollama(),
                config.cleanup_model or config.post_model)
        if config.cleanup_mode != "cloud":
            raise EngineUnavailableError(
                "Cleanup mode is not available yet; choose Cloud cleanup in Settings.",
                engine_id="cleanup", reason=str(config.cleanup_mode),
            )
        return llm_api.post_process_text(
            raw,
            config.target_language,
            config.api_keys.get(config.provider_id, ""),
            config.provider_base_url,
            config.post_model,
        )
