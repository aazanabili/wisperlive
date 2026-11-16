"""Provider preset registry and pure config-migration helpers.

WHY: the settings dialog, ``config_manager``, and the recording pipeline
need ONE authoritative description of the eight cloud presets (plus Custom)
with zero I/O, UI toolkit, or transport code (plan §2.1 / A3 §2.1).  This
module imports only ``dataclasses`` + ``typing`` + the pure
``dictation_config`` rule leaf (M13-M23) — no network/tkinter, no
``config_manager``/``llm_api``/``main`` (acyclic, so config_manager imports
THIS); every function is deterministic, thread-safe, and NEVER raises on
malformed input — config handling degrades, never aborts startup.
``normalize_config`` implements rules M1–M12 (plan §3.1): rewrites only the
five managed keys {provider, provider_variant, provider_base_url, model,
transcription_model}, deletes nothing, never touches api_key fields (M12);
dialect tags are plain literals duplicated in ``llm_payloads`` (no
cross-imports; QA pins both copies equal — R-11).
"""

from dataclasses import dataclass
from typing import Literal, Mapping, Optional, Tuple

from dictation_config import normalize_dictation_keys as _normalize_dictation_keys

CUSTOM_PROVIDER_ID = "custom"
DEFAULT_PROVIDER_ID = CUSTOM_PROVIDER_ID          # backward-compatible default (rule M1)
MODE_SINGLE: Literal["single"] = "single"
MODE_SPLIT: Literal["split"] = "split"

# Wire-dialect tags (plan R5): plain literals duplicated in llm_payloads.py.
DIALECT_OPENAI = "openai"        # generic OpenAI-compatible chat shape (today's behaviour)
DIALECT_MIMO_ASR = "mimo-asr"    # Xiaomi MiMo ASR dialect

# Advisory copy for key-required presets with an empty key (A5A7 §3); Save is
# NEVER blocked on it (D2: the key stays optional at the transport level).
_KEY_REQUIRED_HINT = "An API key is required by this provider; add one before recording."


@dataclass(frozen=True)
class EndpointVariant:
    """One selectable endpoint of a preset (only Xiaomi MiMo has four)."""

    id: str                 # stored in config "provider_variant"
    label: str              # approved UI copy (A5A7 §1)
    base_url: str           # https, no trailing slash/query/userinfo/fragment (H4-safe)
    key_hint: str           # static key-format hint ("tp-xxxxx"); never a secret, never persisted


@dataclass(frozen=True)
class ProviderPreset:
    """One provider preset row of the registry (plan §1.4)."""

    id: str                                 # stored in config "provider"
    label: str                              # approved UI display label (plan §1.4)
    base_url: str                           # default endpoint ("" for custom)
    variants: Tuple[EndpointVariant, ...]   # () when the preset has one fixed URL
    default_variant_id: str                 # "" iff variants == (); else a member of variants
    dialect: str                            # DIALECT_OPENAI | DIALECT_MIMO_ASR (plan R5)
    model_field_mode: Literal["single", "split"]
    transcription_models: Tuple[str, ...]   # curated stage-1 suggestions (MiMo only)
    post_process_models: Tuple[str, ...]    # curated stage-2/single suggestions (MiMo only)
    default_model: str                      # "" unless curated (MiMo: mimo-v2.6-flash)
    default_transcription_model: str        # "" unless split-curated (MiMo: mimo-v2.5-asr)
    api_key_required: bool                  # UI hint ONLY; the runtime key stays optional (D2)
    api_key_hint: str                       # static hint text; never derived from a real key


def _single(pid: str, label: str, url: str, *, key_required: bool = True, hint: str = "") -> ProviderPreset:
    """Single-URL, single-model preset row — six of the eight share this shape."""
    return ProviderPreset(
        id=pid, label=label, base_url=url, variants=(), default_variant_id="", dialect=DIALECT_OPENAI,
        model_field_mode=MODE_SINGLE, transcription_models=(), post_process_models=(),
        default_model="", default_transcription_model="", api_key_required=key_required,
        api_key_hint=hint)


# Custom sentinel: a registry row for the UI, but get_provider() returns None
# for it — Custom has no preset endpoint (URL is user-owned), key optional (D2).
_CUSTOM = _single(CUSTOM_PROVIDER_ID, "Custom (OpenAI-compatible URL)", "", key_required=False)

# THE registry — ids, labels, and URLs exactly per plan §1.4 (ruling R7,
# verified 2026-11-16); insertion order == UI order, custom NOT included.
_MIMO_VARIANTS = (
    EndpointVariant("api", "API (Pay-as-you-go)", "https://api.xiaomimimo.com/v1", ""),
    EndpointVariant("token-plan-ams", "Token Plan — Europe (AMS)", "https://token-plan-ams.xiaomimimo.com/v1", "tp-xxxxx"),
    EndpointVariant("token-plan-sgp", "Token Plan — Singapore (SGP)", "https://token-plan-sgp.xiaomimimo.com/v1", "tp-xxxxx"),
    EndpointVariant("token-plan-cn", "Token Plan — China (CN)", "https://token-plan-cn.xiaomimimo.com/v1", "tp-xxxxx"),
)
_PRESETS: Tuple[ProviderPreset, ...] = (
    ProviderPreset(
        id="xiaomi-mimo", label="Xiaomi MiMo", base_url="https://api.xiaomimimo.com/v1",
        variants=_MIMO_VARIANTS, default_variant_id="api",
        dialect=DIALECT_MIMO_ASR, model_field_mode=MODE_SPLIT,
        transcription_models=("mimo-v2.5-asr",),          # TTS trio deliberately excluded (D2)
        post_process_models=("mimo-v2.6-flash", "mimo-v2.6-pro"),
        default_model="mimo-v2.6-flash", default_transcription_model="mimo-v2.5-asr",
        api_key_required=True,
        api_key_hint="Token Plan keys look like tp-xxxxx; use the cluster URL matching your plan."),
    _single("openai", "OpenAI", "https://api.openai.com/v1"),
    _single("google-gemini", "Google Gemini",
            "https://generativelanguage.googleapis.com/v1beta/openai"),
    # A6 canonical base — https://api.deepseek.com (…/v1 is an alias); plan §1.4 corrected by ruling R-2.
    _single("deepseek", "DeepSeek", "https://api.deepseek.com"),
    _single("groq", "Groq", "https://api.groq.com/openai/v1"),
    _single("mistral", "Mistral", "https://api.mistral.ai/v1"),
    _single("openrouter", "OpenRouter", "https://openrouter.ai/api/v1"),
    _single("ollama-local", "Ollama (local)", "http://localhost:11434/v1",
            key_required=False, hint="Local server: the API key is ignored and may be left empty."),
)

# Module-private O(1) indexes; built once from the frozen registry and never
# mutated (read-only shared state — thread-safe by construction).
_BY_ID = {preset.id: preset for preset in _PRESETS}
_KNOWN = frozenset(_BY_ID) | {CUSTOM_PROVIDER_ID}
_LABEL_TO_ID = {preset.label: preset.id for preset in _PRESETS}
_LABEL_TO_ID[_CUSTOM.label] = _CUSTOM.id


def _text(value: object) -> str:
    """Stripped string for any input; non-strings become '' (never raises)."""
    return value.strip() if isinstance(value, str) else ""


def list_providers() -> Tuple[ProviderPreset, ...]:
    """The eight presets in UI order; the custom sentinel is excluded."""
    return _PRESETS


def provider_choices() -> Tuple[Tuple[str, str], ...]:
    """``((id, label), ...)`` for the provider combobox: presets, custom last."""
    return tuple((preset.id, preset.label) for preset in _PRESETS) + ((_CUSTOM.id, _CUSTOM.label),)


def get_provider(provider_id: object) -> Optional[ProviderPreset]:
    """The preset for an id; strips strings, None for custom/unknown/non-str."""
    if not isinstance(provider_id, str):
        return None
    return _BY_ID.get(provider_id.strip())


def is_known_provider(provider_id: object) -> bool:
    """True for any preset id or the 'custom' sentinel."""
    return isinstance(provider_id, str) and provider_id.strip() in _KNOWN


def variant_options(provider_id: object) -> Tuple[EndpointVariant, ...]:
    """Endpoint variants of a preset; () for custom and single-URL presets."""
    preset = get_provider(provider_id)
    return preset.variants if preset is not None else ()


def model_field_mode(provider_id: object) -> str:
    """'split' | 'single'; 'single' for custom/unknown (Fetch visibility derives from this)."""
    preset = get_provider(provider_id)
    return preset.model_field_mode if preset is not None else MODE_SINGLE


def resolve_provider_id(value: object) -> str:
    """Rules M1/M2/M3: absent/non-str/empty/unknown -> 'custom'."""
    if isinstance(value, str) and value.strip() in _KNOWN:
        return value.strip()
    return CUSTOM_PROVIDER_ID


def resolve_variant_id(provider_id: object, variant_id: object = "") -> str:
    """Rules M5/M6: valid id/label -> itself; invalid/blank/non-str -> preset
    default; custom/no-variant preset -> ''."""
    preset = get_provider(provider_id)
    if preset is None or not preset.variants:
        return ""
    text = _text(variant_id)
    for variant in preset.variants:
        if text in (variant.id, variant.label):
            return variant.id
    return preset.default_variant_id


def resolve_base_url(provider_id: object, variant_id: object = "",
                     fallback_base_url: object = "") -> str:
    """Rule M7: a known preset resolves to its registry endpoint for the resolved
    variant (stored value ignored — registry wins); fallback passthrough otherwise."""
    preset = get_provider(provider_id)
    if preset is None:
        return fallback_base_url if isinstance(fallback_base_url, str) else ""
    if preset.variants:
        wanted = resolve_variant_id(preset.id, variant_id)
        return next((variant.base_url for variant in preset.variants if variant.id == wanted),
                    preset.base_url)
    return preset.base_url


def resolve_transcription_model(provider_id: object, transcription_model: object,
                                model: object = "") -> str:
    """EFFECTIVE stage-1 model (mirrors runtime ``transcription_model or model``):
    split preset -> stored value, else curated default, else ``model``;
    single/custom/unknown -> ``model`` (M4/M11 already cleared stray values)."""
    preset = get_provider(provider_id)
    if preset is not None and preset.model_field_mode == MODE_SPLIT:
        stored = _text(transcription_model) or preset.default_transcription_model
        if stored:
            return stored
    return _text(model)


def dialect_for_preset(provider_id: object) -> str:
    """Dialect tag for a preset; custom/unknown -> 'openai' (today's shape)."""
    preset = get_provider(provider_id)
    return preset.dialect if preset is not None else DIALECT_OPENAI


# A3 §2.4 spells the same helper ``dialect_for`` — one implementation, two
# names, so the plan and the contract surface cannot drift.
dialect_for = dialect_for_preset


def normalize_config(config: dict) -> dict:
    """Apply migration rules M1-M23 (plan §3.1) in place: managed keys only,
    deletes nothing, never touches ``api_key`` (M12), idempotent, never raises (N1)."""
    try:
        result = config if isinstance(config, dict) else dict(config)
    except (TypeError, ValueError, RuntimeError):
        result = {}                     # unusable input -> managed keys only
    # M1/M2/M3: provider absent | non-str | blank | unknown id -> "custom".
    pid = resolve_provider_id(result.get("provider"))
    result["provider"] = pid
    preset = _BY_ID.get(pid)
    if preset is None:
        # M4: custom touches nothing else (URL/model byte-identical) — only the two managed strays (R3 IFF).
        result["provider_variant"] = ""
        result["transcription_model"] = ""
    else:
        # M5/M6: variant -> valid id, preset default, or "" (never a dangling id).
        result["provider_variant"] = resolve_variant_id(pid, result.get("provider_variant"))
        # M7: the registry endpoint wins; the stored file value is discarded.
        result["provider_base_url"] = resolve_base_url(pid, result["provider_variant"])
        if preset.model_field_mode == MODE_SPLIT:
            # M8/M9: split presets always store both curated stage models.
            if not _text(result.get("model")):
                result["model"] = preset.default_model
            if not _text(result.get("transcription_model")):
                result["transcription_model"] = preset.default_transcription_model
        else:
            # M10 fail-closed "" / M11: no stage-1 value leaks across vendors (R3 IFF at load).
            if not _text(result.get("model")):
                result["model"] = ""
            result["transcription_model"] = ""
    # M13-M23 dictation keys: EVERY provider (M4 fall-through, R-U3 — byte-identity scoped to legacy keys).
    return _normalize_dictation_keys(result, _KNOWN)


def validate_provider_selection(config: Mapping[str, object]) -> Tuple[str, ...]:
    """Advisory, non-mutating checks; ``()`` means OK.  Never raises; the
    copy mirrors llm_api wording and never blocks Save (the hard gates stay
    the recording guard and ``LLMConfigError``).  URL format validation
    stays in ``llm_api._endpoint`` (single H4 source)."""
    source: Mapping[str, object] = config if isinstance(config, Mapping) else {}
    issues = []
    raw_provider = source.get("provider")
    pid = raw_provider.strip() if isinstance(raw_provider, str) else ""
    if not pid:
        issues.append("Select a provider.")
        pid = CUSTOM_PROVIDER_ID
    elif pid != CUSTOM_PROVIDER_ID and pid not in _BY_ID:
        issues.append("The saved provider selection is not recognized; it will be treated as Custom.")
        pid = CUSTOM_PROVIDER_ID
    raw_variant = source.get("provider_variant")
    preset = _BY_ID.get(pid)
    if preset is not None and preset.variants:
        variant_id = _text(raw_variant)
        if variant_id and not any(variant_id == variant.id for variant in preset.variants):
            issues.append("The saved endpoint variant is not recognized; the default endpoint will be used.")
    if not _text(resolve_base_url(pid, raw_variant, source.get("provider_base_url"))):
        issues.append("Provider Base URL is not set.")
    if not _text(source.get("model")):
        issues.append("No model is selected. Open Settings, fetch models, and choose one.")
    if model_field_mode(pid) == MODE_SPLIT and not _text(source.get("transcription_model")):
        issues.append("No transcription model is selected for stage 1; open Settings and choose one.")
    return tuple(issues)


def display_names() -> Tuple[str, ...]:
    """Approved display labels in UI order; Custom last."""
    return tuple(preset.label for preset in _PRESETS) + (_CUSTOM.label,)


def provider_id_from_display(name: object) -> Optional[str]:
    """Display label -> id; unknown/None -> None (never guess an id)."""
    return _LABEL_TO_ID.get(name.strip()) if isinstance(name, str) else None


def needs_api_key(provider_id: object) -> bool:
    """False for custom/unknown (keyless always allowed, D2)."""
    preset = get_provider(provider_id)
    return bool(preset is not None and preset.api_key_required)


def key_hint(provider_id: object, api_key: object) -> str:
    """Hint when a key is required but the field is empty; '' otherwise —
    registry format hint (MiMo tp-xxxxx) else A5A7 §3 copy; never Ollama/Custom."""
    preset = get_provider(provider_id)
    if preset is None or not preset.api_key_required or _text(api_key):
        return ""
    return preset.api_key_hint or _KEY_REQUIRED_HINT


def initial_state(config: Mapping[str, object]) -> dict:
    """Pure dialog hydration WITHOUT URL inference (plan ruling R3): known id ->
    preset with its registry endpoint (readonly); missing/unknown -> custom with
    the stored URL verbatim.  Split shows stored models, curated defaults if empty."""
    source: Mapping[str, object] = config if isinstance(config, Mapping) else {}
    pid = resolve_provider_id(source.get("provider"))
    variant = resolve_variant_id(pid, source.get("provider_variant"))
    preset = _BY_ID.get(pid)
    raw_model = source.get("model")
    raw_transcription = source.get("transcription_model")
    if preset is not None and preset.model_field_mode == MODE_SPLIT:
        model = _text(raw_model) or preset.default_model
        transcription = _text(raw_transcription) or preset.default_transcription_model
    else:
        model = raw_model if isinstance(raw_model, str) else ""
        transcription = raw_transcription if isinstance(raw_transcription, str) else ""
    return {
        "provider": pid,
        "provider_variant": variant,
        "provider_base_url": resolve_base_url(pid, variant, source.get("provider_base_url")),
        "api_key": source.get("api_key") if isinstance(source.get("api_key"), str) else "",
        "model": model,
        "transcription_model": transcription,
    }


def save_payload(state: Mapping[str, object]) -> dict:
    """Ruling R3 save mapping — exactly the six contract keys.  Split:
    transcription row -> transcription_model, post row -> model; single/custom:
    model row -> model and transcription_model ALWAYS "" (the IFF invariant —
    a vendor switch can never leak a stage-1 model).  Presets re-resolve
    their URL; Custom keeps the entered URL; ids or display labels accepted."""
    source: Mapping[str, object] = state if isinstance(state, Mapping) else {}
    raw_provider = source.get("provider")
    pid = provider_id_from_display(raw_provider) or resolve_provider_id(raw_provider)
    variant = resolve_variant_id(pid, source.get("provider_variant"))
    payload = {
        "provider": pid,
        "provider_variant": variant,
        "provider_base_url": resolve_base_url(pid, variant, source.get("provider_base_url")).strip(),
        "api_key": _text(source.get("api_key")),
        "model": _text(source.get("model")),
        "transcription_model": _text(source.get("transcription_model")),
    }
    if model_field_mode(pid) != MODE_SPLIT:
        payload["transcription_model"] = ""
    return payload
