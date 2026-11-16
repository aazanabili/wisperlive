"""Polish / translate-last-dictation over the session span registry (D3/U3).

WHY: Polish re-runs the mode's cleanup over the MEMORY-ONLY last dictation and
re-inserts the result — default ``polish_mode="insert"`` pastes a NEW insert
and leaves it on the clipboard (U3 safe insert + clipboard); ``replace-
experimental`` may select-back ONLY while a span is provably valid (registry
TTL + focus unchanged + Windows) and otherwise AUTO-FALLS-BACK to insert — we
never guess at another app's selection (D3).

This module owns the per-session ``TypedSpan`` registry (A6 s4.1) whose pure
ops live in ``dictation_types`` (``register``/``invalidate``/``latest_valid``);
the keyboard hook and focus guard feed ``invalidate_all`` through callbacks.
Cleanup runs the existing ``llm_api.post_process_text``: cloud against the
current provider, ``off`` verbatim, and (Phase 8) ``local-ollama`` against the
Ollama endpoint behind the shared :func:`require_ollama` presence gate — an
absent Ollama raises the typed error, NEVER a silent cloud fallback (D6).
"""

import logging
import os
import time
import urllib.error
import urllib.request
from typing import Tuple

from dictation_types import TypedSpan, latest_valid, register, invalidate
from llm_errors import EngineUnavailableError
import llm_api

logger = logging.getLogger(__name__)

INSERT_MODE = "insert"
REPLACE_EXPERIMENTAL = "replace-experimental"

OLLAMA_BASE_URL = "http://localhost:11434/v1"       # providers.ollama-local registry endpoint
_OLLAMA_DOWN = (
    "Ollama is not running. Start it with 'ollama serve' (or install Ollama from "
    "ollama.com), or choose Cloud cleanup in Settings.")


def require_ollama(base_url: str = OLLAMA_BASE_URL, timeout: float = 2.0) -> str:
    """Phase-8 presence gate: ONE bounded GET to the Ollama endpoint BEFORE any local
    cleanup call (D6: absent/unreachable -> typed, actionable ``EngineUnavailableError``
    — never a silent fallback to cloud).  Any HTTP answer (including a 404 on the bare
    base) proves the server is up; only connection failures/timeouts count as absent.
    D11: log line carries the failure TYPE only."""
    request = urllib.request.Request(base_url, headers={"User-Agent": "WhisperLive"})
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            pass
    except urllib.error.HTTPError:
        pass                                  # server answered (even 404) -> present
    except (urllib.error.URLError, OSError) as error:
        logger.info("Ollama presence check failed (%s)", type(error).__name__)
        raise EngineUnavailableError(_OLLAMA_DOWN, engine_id="cleanup",
                                     reason="ollama unreachable") from error
    return base_url


class SpanRegistry:
    """Session span registry (A6 s4.1 / D3): bounded, newest-first tuple state
    wrapped around the PURE ``dictation_types`` ops.  Spans are recorded ONLY
    after a successful injection (the actor guarantees that ordering); any real
    user key (LL hook) or focus change invalidates them."""

    def __init__(self):
        self._spans: Tuple[TypedSpan, ...] = ()
        self._next_id = 1

    @property
    def spans(self) -> Tuple[TypedSpan, ...]:
        return self._spans

    def record_typed(self, emission_seq: int, text: str, insertion_mode: str,
                     now_epoch: int) -> TypedSpan:
        """Record ONE successfully injected emission (post-injection only).

        ``char_count`` = UTF-16 units sent through SendInput; ``length`` =
        characters for the select-back span math (A3 s2.6 — both preserved).
        """
        span = TypedSpan(
            span_id=self._next_id,
            emission_seq=int(emission_seq),
            char_count=len(text.encode("utf-16-le")) // 2,
            word_count=len(text.split()),
            start=0,                               # whole-run paste (0 = caret insert)
            length=len(text),
            insertion_mode=insertion_mode or INSERT_MODE,
            created_at_epoch=int(now_epoch),
            valid=True,
        )
        self._next_id += 1
        self._spans = register(self._spans, span)  # newest-first; rule (a) invalidates older
        return span

    def invalidate_all(self) -> None:
        """Focus change / any real key press / recording restart (A3 s2.6)."""
        self._spans = invalidate(self._spans, all_=True)

    def invalidate(self, span_id) -> None:
        self._spans = invalidate(self._spans, span_id=span_id)

    def latest_valid(self, now_epoch: int) -> TypedSpan | None:
        return latest_valid(self._spans, now_epoch)


def apply_polish(cleaned_text: str, *, registry: SpanRegistry, mode: str,
                 focus_unchanged: bool, tap_back, type_text, clipboard,
                 now_epoch: int, on_windows: bool) -> str:
    """U.3/D3 apply: returns ``"insert"`` (default), ``"replace"`` (proven
    select-back), or ``"none"`` (empty text).  Any doubt -> safe insert.

    ``tap_back``/``type_text`` are injected seams (Windows SendInput paths);
    ``clipboard`` is the ONE session clipboard (insert leaves the text copied).
    """
    if not isinstance(cleaned_text, str) or not cleaned_text:
        return "none"
    span = registry.latest_valid(now_epoch)
    replace_ok = (mode == REPLACE_EXPERIMENTAL and on_windows and focus_unchanged
                  and span is not None)
    if replace_ok:
        try:
            tap_back(span.length)                  # SHIFT+VK_LEFT x length chars (U.4)
            type_text(cleaned_text)
        except Exception as error:                 # any doubt -> NEVER half-replaced
            logger.warning("Select-back failed; falling back to safe insert: %s",
                           type(error).__name__)
        else:
            registry.invalidate_all()              # the target changed under us now
            return "replace"
    # Default / fallback: safe INSERT at caret + leave it on the clipboard (U3).
    clipboard.put_and_paste(cleaned_text)
    registry.invalidate_all()                      # insertion itself shifts spans
    return "insert"


def polish_last(last_text: str, actor, live_config) -> str:
    """U.4 entry (footer button / tray item): re-run the mode's cleanup over the
    memory-only last dictation, then :func:`apply_polish`.

    ``actor`` is the session's ``TyperActor`` (duck-typed: registry, focus
    guard, clipboard, direct sender).  Raises ``EngineUnavailableError`` when
    Ollama is unreachable (local-ollama) and propagates ``LLMError`` from
    cleanup — a cleanup failure NEVER silently polishes the raw text (pipeline
    invariant).  Returns the apply result ("insert" | "replace" | "none").
    """
    source = live_config if isinstance(live_config, dict) else {}
    cleanup = source.get("cleanup_mode")
    if cleanup == "local-ollama":
        # Phase 8: presence gate FIRST (typed + actionable), then the classic
        # chat path against the Ollama endpoint — no key (registry: key-free),
        # model = cleanup_model ("" inherits the post_model per the config doc).
        cleaned = llm_api.post_process_text(
            last_text, source.get("target_language", ""), "", require_ollama(),
            source.get("cleanup_model") or source.get("model", ""))
    elif cleanup == "off":
        cleaned = last_text                         # user chose raw output (D2 off)
    else:
        # Cloud cleanup via the CLASSIC stage 2 (translation preserved); reads
        # the CURRENT settings (user-triggered action, not the session snapshot).
        cleaned = llm_api.post_process_text(
            last_text,
            source.get("target_language", ""),
            source.get("api_key", ""),
            source.get("provider_base_url", ""),
            source.get("model", ""),
        )
    return apply_polish(
        cleaned,
        registry=actor.registry,
        mode=source.get("polish_mode") or INSERT_MODE,
        focus_unchanged=actor.focus_guard.unchanged(),   # PROOF or insert-fallback
        tap_back=actor.tap_back,
        type_text=actor.type_text,
        clipboard=actor.clipboard,
        now_epoch=int(time.time()),
        on_windows=(os.name == "nt"),
    )
