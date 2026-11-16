"""Typed error taxonomy for provider + dictation failures (pure leaf).

WHY this module exists: the classic pipeline's ``LLM*`` classes moved here
verbatim so ONE hierarchy serves both the classic path and the new dictation
modes, while ``llm_api.LLMError`` keeps its exact identity — ``llm_api``
re-exports every name (existing tests import the classes from there; plan
Step 1.4 / A3 T17 identity pins).

Per A3 Round 2 R3 the session-level umbrella ``DictationError`` is a SIBLING
base next to ``LLMError`` (both subclass ``RuntimeError``), never its parent:
the classic ``except (LLMError, PasteError, ValueError)`` boundaries in
``main`` keep their exact meaning, and ``dictation_session`` maps the
new-mode errors explicitly.

Pure: zero imports, no I/O, no logging — every message is user-safe English
copy with no transcript, no key material, and no URLs (D11).
"""


class DictationError(RuntimeError):
    """Session-level dictation failure with a user-safe message (A6 s6.1)."""


class LLMError(RuntimeError):
    """A provider failure with a message safe to show in the UI."""


class LLMConfigError(LLMError):
    """Missing or invalid local configuration (URL, model, ...)."""


class LLMAuthError(LLMError):
    """The provider rejected the API key (HTTP 401/403)."""


class LLMNetworkError(LLMError):
    """The provider could not be reached, timed out, or dropped the link."""


class LLMQuotaError(LLMError):
    """429 / per-engine budget exhausted (D6) — engine_id + reset hint.

    Raised by the transport when a 429 carries a parseable reset header and
    by ``quota_ledger.check_admission`` before dispatch.  The UI surfaces the
    engine + reset time and offers switch-to-local / finish-at-stop /
    abort-with-clipboard — NEVER a silent failover (D6).
    """

    def __init__(self, message: str, *, engine_id: str = "", reset_hint: str = "") -> None:
        super().__init__(message)
        self.engine_id = engine_id
        self.reset_hint = reset_hint


class ModelNotDownloadedError(LLMConfigError):
    """Recording guard: ``local_models[model_id] != "ready"`` (D7 — download
    happens from Settings only, never at record time)."""

    def __init__(self, message: str, *, model_id: str = "") -> None:
        super().__init__(message)
        self.model_id = model_id


class EngineUnavailableError(LLMConfigError):
    """Engine/mode hidden WITH its reason (D8): optional ML extras missing or
    the mode's phase not shipped yet — never a crash at record time."""

    def __init__(self, message: str, *, engine_id: str = "", reason: str = "") -> None:
        super().__init__(message)
        self.engine_id = engine_id
        self.reason = reason


class ConsentRequiredError(LLMConfigError):
    """U2 gate: Gemini engine selected without consent or paid-key attestation,
    raised BEFORE any network I/O (fail-closed, default OFF)."""

    def __init__(self, message: str, *, engine_id: str = "") -> None:
        super().__init__(message)
        self.engine_id = engine_id


class FocusLostError(DictationError):
    """Live typing lost its target window (D4): the phrase is NOT typed and
    there is no auto-retry — the text goes to the clipboard instead."""
