"""Pure ports and immutable data for the three dictation modes (A3 Round 2 R1).

WHY this module: exactly ONE implementation type per concept — the immutable
session snapshot (D10), the ``SegmentIn -> EmissionOut`` engine port (A6 s1.2),
and the text-free polish span registry (U3) — so no second, drifting copy of
any contract ever exists across classic/chunked/live modes.

PURITY: standard library ONLY (``dataclasses``/``typing``/``types``) plus the
pure ``providers`` registry leaf for the R-3 snapshot builder (A3 s2.1
allowlist: pure leaves only) — no I/O, no tkinter, no ``config_manager``/
``llm_api``/engine imports, and no clock reads: every "now" value arrives as
an argument.

D11 in the types themselves: every transcript-bearing field is declared
``field(repr=False)``, so an accidental ``logging``/f-string of an event can
never leak text; ``TypedSpan`` carries NO text field at all.

PHASE-1 BOUNDARY (reported to the orchestrator): ``build_session_config`` /
``resolve_chunked_engine`` (A3 R1) resolve values through the provider
registry — a non-stdlib import this module's purity gate forbids — so they
land with Phase 2 where ``main``/``dictation_session`` consume them; all
field schemas they must fill are already pinned here.
"""

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Callable, Mapping, Optional, Protocol, Tuple

import providers


@dataclass(frozen=True)
class WordSpan:
    """One word timing (A3 s2.3); offsets are milliseconds within the segment."""

    start_ms: int
    end_ms: int
    text: str = field(repr=False)           # D11: never in logs/reprs


@dataclass(frozen=True)
class SegmentIn:
    """ONE merged unit of speech going INTO an engine (A6 s1.2).

    Audio lives IN MEMORY only (D10) — ``audio_ref``/temp-WAV handles were
    retired for the new modes; the classic file path keeps ``main``'s
    caller-deletes contract (D1).  ``segment_seq`` is monotonic and assigned
    by the session, giving D5's ordered-emission guarantee its base.
    """

    segment_seq: int                        # monotonic, assigned by the session (D5)
    pcm16: bytes = field(repr=False)        # 16 kHz mono s16le — never echoed (D11)
    speech_start_s: float                   # VAD-trimmed speech span inside pcm16
    speech_end_s: float
    audio_seconds: float                    # billing/quota input (ledger applies floors)


EMISSION_KINDS = ("phrase", "polish_insert", "correction")


@dataclass(frozen=True)
class EmissionOut:
    """ONE output unit from ANY engine (A6 s1.2 port + A3 ordering extras, R-U1).

    Ordering contract: the session assigns ``emission_seq`` (monotonic via
    ``next_seq``); consumers apply strictly by it, drop duplicate replays
    (idempotence), and buffer bounded gaps.  ``text`` is D11-hidden from
    reprs.  Live mode emits confirmed phrases only (U6) — no interim events
    exist, so ``is_final`` flips to ``False`` only for streaming confirmations.
    """

    text: str = field(repr=False)           # D11: never in logs/reprs
    segment_seq: int = -1                   # ties back to the SegmentIn; -1 = live
    emission_seq: int = -1                  # session-assigned ordering key (R-U1)
    kind: str = "phrase"                    # one of EMISSION_KINDS
    replaces_seq: int = -1                  # "correction" -> emission_seq replaced
    is_final: bool = True                   # streaming confirmation = False (A6 name)
    words: Tuple[WordSpan, ...] = ()        # optional word timings (trim + span math)


SEGMENT_STATUS = ("pending", "transcribing", "confirmed", "failed", "dropped")

# Legal SEGMENT_STATUS jumps (A3 s2.3): anything -> pending is illegal, a
# confirmed/failed/dropped segment is terminal, and confirmed -> transcribing
# (the annex's named example) never happens.  set_status enforces this table.
_LEGAL_SEGMENT_JUMPS = {
    "pending": ("transcribing", "failed", "dropped"),
    "transcribing": ("confirmed", "failed", "dropped"),
    "confirmed": (),
    "failed": (),
    "dropped": (),
}


@dataclass(frozen=True)
class Segment:
    """Session-side lifecycle record for ONE speech unit (A3 s2.3) — the single
    segment contract behind ``SEGMENT_STATUS``; engines receive the audio-bearing
    ``SegmentIn`` instead.  ``audio_ref`` is an OPAQUE handle (ring-range id or
    temp-WAV path) — NEVER audio bytes and never transcript (D10/D11); the
    creator of whatever it points at deletes it (caller-deletes, D1)."""

    session_id: str
    seq: int                              # 0-based, strictly +1 within a session (gapless)
    audio_ref: str                        # opaque handle only
    start_ms: int                         # monotonic offsets within the session clock
    end_ms: int
    words: Tuple[WordSpan, ...] = ()      # word timestamps (empty allowed; never fabricated)
    status: str = "pending"               # one of SEGMENT_STATUS
    error_kind: str = ""                  # "" | taxonomy class name — NO message bodies (D11)


def set_status(segment: Segment, status: str, *, error_kind: str = "") -> Segment:
    """Pure SEGMENT_STATUS transition (A3 s2.3): returns a NEW ``Segment``.

    Illegal jumps (anything -> pending, confirmed -> transcribing, leaving a
    terminal state) raise ``ValueError`` — programmer error, NOT a user path,
    so the never-raise (N1) rule deliberately does not apply here.  ``error_kind``
    must be a bare taxonomy class name (whitespace rejected — D11: message
    bodies could carry transcript-adjacent content) and is only retained for
    ``status == "failed"``.
    """
    if not isinstance(status, str) or status not in SEGMENT_STATUS:
        raise ValueError(f"unknown segment status: {status!r}")
    if status not in _LEGAL_SEGMENT_JUMPS[segment.status]:
        raise ValueError(f"illegal segment transition: {segment.status} -> {status}")
    if not isinstance(error_kind, str) or (error_kind and not error_kind.replace("_", "").isalnum()):
        raise ValueError("error_kind must be a plain taxonomy class name (no message bodies)")
    return replace(segment, status=status,
                   error_kind=error_kind if status == "failed" else "")


@dataclass(frozen=True)
class SessionConfig:
    """Immutable session snapshot (D10): built on the UI thread at recording
    START, after the hard pre-flight guard and before the recorder starts;
    workers/engines read ONLY this copy, so mid-session Settings edits apply
    to the NEXT recording.

    Field values arrive already resolved (no registry import here — see the
    module note); ``api_keys`` is least-privilege — only vendors this session
    may call.  ``gemini_enabled`` is U2's fail-closed gate (consent OR
    paid-key attestation).  NO ``to_dict``/``asdict`` exists: the snapshot is
    deliberately not serializable (D10 + D11).
    """

    session_id: str                         # uuid4 hex, assigned at recording start
    started_at_epoch: int                   # wall clock for display/reset hints ONLY
    dictation_mode: str                     # local | chunked | live (M13 domain)
    model_size: str                         # auto | fast | standard
    chunked_engine: str                     # RESOLVED concrete engine ("" applied by builder)
    chunked_asr_model: str                  # resolved per A3 s1.2 ("" = registry default)
    provider_id: str
    provider_variant: str
    provider_base_url: str
    dialect: str                            # providers.dialect_for_preset(provider_id)
    transcription_model: str                # effective stage-1 (transcription_model or model)
    post_model: str                         # stage-2 / cleanup chat model (today's model)
    live_engine: str
    cleanup_mode: str                       # cloud | local-ollama | off (D2)
    cleanup_model: str                      # "" = inherit post_model
    target_language: str
    polish_mode: str                        # insert | replace-experimental (U3)
    api_keys: Mapping[str, str] = field(repr=False)   # D11 secret: MappingProxyType
    gemini_enabled: bool                    # U2: gemini_consent OR paid-key attestation

    def __post_init__(self) -> None:
        # D10 mechanics: copy + freeze the mapping once, so later live-config
        # mutation cannot alias through; mutating the proxy raises TypeError.
        object.__setattr__(self, "api_keys", MappingProxyType(dict(self.api_keys)))


def resolve_chunked_engine(config: Mapping) -> str:
    """A3 s1.2 pure resolution at snapshot time: ``""`` (or garbage) inherits
    ``provider`` (incl. ``custom``) — never rewritten to a preset id, so the
    stored value cannot drift; a stored registry preset id or ``"groq-whisper"``
    passes through.  Never raises (N1)."""
    source: Mapping = config if isinstance(config, Mapping) else {}
    stored = source.get("chunked_engine")
    if isinstance(stored, str):
        text = stored.strip()
        if text == "groq-whisper" or providers.is_known_provider(text):
            return text
    return providers.resolve_provider_id(source.get("provider"))


def build_session_config(config: Mapping, *, session_id: str, started_at_epoch: int,
                         api_keys: Mapping) -> SessionConfig:
    """D10 snapshot builder (A3 s2.2 / plan R-3): resolve EVERY field from the
    live config ON THE UI THREAD at recording start; workers then read only the
    returned frozen object (mid-session Settings edits apply to the NEXT
    recording).  ``api_keys`` is the decrypted least-privilege map for exactly
    the vendors this session may call — supplied by the I/O tier, so this pure
    builder never touches ``config_manager``/DPAPI.  Field values are assumed
    load-normalized (M13-M23); this builder only coerces types (N1).
    """
    source: Mapping = config if isinstance(config, Mapping) else {}

    def _text(key: str) -> str:
        value = source.get(key)
        return value if isinstance(value, str) else ""

    provider_id = providers.resolve_provider_id(source.get("provider"))
    provider_variant = providers.resolve_variant_id(provider_id, source.get("provider_variant"))
    return SessionConfig(
        session_id=session_id,
        started_at_epoch=started_at_epoch if isinstance(started_at_epoch, int) else 0,
        dictation_mode=_text("dictation_mode") or "local",
        model_size=_text("model_size") or "auto",
        chunked_engine=resolve_chunked_engine(source),
        chunked_asr_model=_text("chunked_asr_model"),
        provider_id=provider_id,
        provider_variant=provider_variant,
        provider_base_url=providers.resolve_base_url(provider_id, provider_variant),
        dialect=providers.dialect_for_preset(provider_id),
        # Effective stage-1 (transcription_model or model) mirrors the runtime
        # fallback so chunked chat units keep today's two-stage semantics (D1).
        transcription_model=providers.resolve_transcription_model(
            provider_id, source.get("transcription_model"), source.get("model")),
        post_model=_text("model").strip(),
        live_engine=_text("live_engine") or "local",
        cleanup_mode=_text("cleanup_mode") or "cloud",
        cleanup_model=_text("cleanup_model"),
        target_language=_text("target_language"),
        polish_mode=_text("polish_mode") or "insert",
        api_keys=api_keys,
        # U2 fail-closed: literal True on EITHER flag (M20 already normalized both).
        gemini_enabled=(source.get("gemini_consent") is True
                        or source.get("gemini_paid_key_confirmed") is True),
    )


SPAN_REGISTRY_CAPACITY = 4                 # A3 s2.6: newest-first, drop-oldest
SPAN_TTL_S = 600                           # 10 min: stale spans never drive select-back


@dataclass(frozen=True)
class TypedSpan:
    """Registry record for ONE typed emission (A6 s4.1 merged with A3 s2.6).

    Deliberately carries NO text (D11): polish operates on text passed
    explicitly by the caller.  ``created_at_epoch`` is injected clock input —
    this module never reads a clock.
    """

    span_id: int                            # unique within the session registry
    emission_seq: int                       # EmissionOut.emission_seq this span covers
    char_count: int                         # UTF-16 units typed (SendInput accounting)
    word_count: int                         # select-back bound (legacy sketch, A6 s4.5)
    start: int                              # offset within the inserted run (0 = whole run)
    length: int                             # chars inserted — SHIFT+VK_LEFT x length (U.4)
    insertion_mode: str                     # "insert" | "replace-experimental" (mode at typing)
    created_at_epoch: int                   # TTL input (caller's injected now)
    valid: bool = True                      # cleared by invalidation (hook/focus/user edit)


# A3's name for the same merged dataclass: one implementation, two names, so
# the plan and the contract surface cannot drift (providers.dialect_for precedent).
PolishSpan = TypedSpan


def register(registry, span):
    """Add ``span`` as newest; drop-oldest beyond ``SPAN_REGISTRY_CAPACITY``.

    Registering a NEWER emission also invalidates every earlier span
    (validity rule (a): the caret moved when the new text was typed — never
    guess at shifted offsets).  Pure: returns a NEW tuple.
    """
    prior = tuple(registry)
    if len(prior) >= SPAN_REGISTRY_CAPACITY:
        prior = prior[:SPAN_REGISTRY_CAPACITY - 1]      # tuple is newest-first: drop the tail
    invalidated = tuple(replace(old, valid=False) for old in prior)
    return (span,) + invalidated


def invalidate(registry, *, span_id=None, all_=False):
    """Pure invalidation (returns a NEW registry): ``all_`` clears every span
    (focus change / recording restart / session end per A3 s2.6), ``span_id``
    clears one; no selector or no match leaves the registry unchanged."""
    spans = tuple(registry)
    if all_:
        return tuple(replace(span, valid=False) for span in spans)
    if span_id is None:
        return spans
    return tuple(replace(span, valid=False) if span.span_id == span_id else span
                 for span in spans)


def latest_valid(registry, now_epoch, ttl_s=SPAN_TTL_S) -> Optional[TypedSpan]:
    """Newest span still valid AND within TTL (rules (c)/(d)); ``None`` means
    Polish must degrade to safe-insert — never guess at the target's selection."""
    for span in registry:                   # newest first
        if span.valid and now_epoch - span.created_at_epoch <= ttl_s:
            return span
    return None


def next_seq(latest: int) -> int:
    """Next strictly increasing ``emission_seq`` (0-based: ``next_seq(-1) == 0``).
    Garbage input restarts at 0 (never raises, N1)."""
    return latest + 1 if isinstance(latest, int) else 0


def validate_emission(event, span_registry=()) -> bool:
    """Pure emission gate (A3 s2.4): a known ``kind``, and a ``correction``
    may ONLY replace an emission covered by a VALID span — a dangling
    ``replaces_seq`` is rejected.  Never raises on malformed input (N1)."""
    if getattr(event, "kind", None) not in EMISSION_KINDS:
        return False
    if event.kind != "correction":
        return True
    return any(span.valid and span.emission_seq == getattr(event, "replaces_seq", -1)
               for span in (span_registry or ()))


class SegmentEngine(Protocol):
    """Batch engines: local faster-whisper and chunked cloud (A6 s1.2)."""

    def start(self, config: SessionConfig) -> None: ...

    def process(self, segment: SegmentIn) -> list[EmissionOut]: ...

    def close(self) -> None: ...


class LiveStreamEngine(Protocol):
    """Streaming engines: LocalAgreement-2 local; WS cloud in Phase 7 (A6 s1.2)."""

    def start(self, config: SessionConfig, emit: Callable[[EmissionOut], None]) -> None: ...

    def push_audio(self, pcm16: bytes) -> None: ...

    def flush(self) -> None: ...            # VAD-end / stop: force confirm-all

    def close(self) -> None: ...
