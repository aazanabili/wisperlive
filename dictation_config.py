"""Pure config normalization for the dictation modes + secret-map helpers (A3 final).

WHY this module exists: `providers.normalize_config` must apply rules M13-M23
while `providers.py` stays a stdlib-only leaf (R-13 exact public surface AND
the AST purity guard `test_ast_purity_guard_imports_only_dataclasses_typing`),
and `config_manager` needs the SAME K1-K8 secret-map logic wrapped around its
DPAPI protect/unprotect (the I/O tier keeps those callables).  Orchestrator
ruling Option D (plan R-3): the rules and pure K cores live HERE; providers
delegates (internal, private alias); config_manager injects its callables —
exactly ONE implementation of each rule, no drift.

Contract (A3 N1): every function NEVER RAISES, is idempotent where it
normalizes, and leaves unknown keys round-tripping untouched (M12).  Only the
explicit K helpers below touch the secret envelope (`api_key` /
`api_key_protected` / `api_keys_protected` — M12b keeps `providers` blind to
it).  K5/K6 are release-lifecycle rules (Releases B/C) and are intentionally
NOT active in Release A; K7's dialog wiring lands with the Settings phase.

PURITY: this module imports NOTHING (not even stdlib) — no I/O, no clock, no
tkinter, and no `providers` import (that would be circular): M16's preset-id
set is injected by the caller instead.
"""

# M13-M23 domains (A3 s1.1 / Round-2 R2): stored values validate against these
# exact tuples; unknown values degrade to the fail-closed default (N1), never raise.
_MODES = ("local", "chunked", "live")                 # dictation_mode (U4 default "local")
_CLEANUP = ("cloud", "local-ollama", "off")           # cleanup_mode (D2 default "cloud")
_SIZES = ("auto", "fast", "standard")                 # model_size
_LIVE = ("local", "gemini-live", "openai-realtime")   # live_engine (Phase 1 ships "local")
_POLISH = ("insert", "replace-experimental")          # polish_mode (U3: no "off" value)
_MODEL_STATES = ("absent", "ready", "stale")          # local_models values (M21)
_GROQ_WHISPER_ENGINE = "groq-whisper"                 # M16: dedicated-ASR engine id


def _choice(value, default, options):
    """M13/M14/M15/M18 core: absent/non-str/blank/unknown -> ``default``;
    a valid value passes byte-identical (canonical values never carry spaces)."""
    if isinstance(value, str):
        text = value.strip()
        if text in options:
            return text
    return default


def normalize_dictation_keys(config: dict, preset_ids=()) -> dict:
    """Apply M13-M23 to ``config`` IN PLACE and return it — never raises,
    idempotent, unknown keys untouched (M12), secret envelope untouched (M12b).

    ``preset_ids`` is the provider-registry id set for M16 (injected by the
    caller — this module must not import ``providers``)."""
    try:
        result = config if isinstance(config, dict) else dict(config)
    except (TypeError, ValueError, RuntimeError):
        result = {}
    # M13/M14/M15/M18: mode/size/engine enums — absent, non-str, blank, or
    # unknown -> fail-closed default (U4 "local", D2 "cloud", Phase-1 "local").
    result["dictation_mode"] = _choice(result.get("dictation_mode"), "local", _MODES)
    result["cleanup_mode"] = _choice(result.get("cleanup_mode"), "cloud", _CLEANUP)
    result["model_size"] = _choice(result.get("model_size"), "auto", _SIZES)
    result["live_engine"] = _choice(result.get("live_engine"), "local", _LIVE)
    # M16: "" = inherit "provider" and is NEVER rewritten to a preset id (no
    # drift, A3 s1.2); valid preset ids and "groq-whisper" pass byte-identical.
    engine = result.get("chunked_engine")
    engine = engine.strip() if isinstance(engine, str) else ""
    result["chunked_engine"] = (engine if engine == "" or engine == _GROQ_WHISPER_ENGINE
                                or engine in preset_ids else "")
    # M17: string settings fail closed to "" — a non-str never reaches .strip()
    # downstream; valid strings pass through AS-IS (no stripping).
    for key in ("chunked_asr_model", "cleanup_model", "model_store_dir"):
        if not isinstance(result.get(key), str):
            result[key] = ""
    # M19: EXACT values only (U3) — " insert " with spaces is not valid.
    polish = result.get("polish_mode")
    result["polish_mode"] = polish if polish in _POLISH else "insert"
    # M20/M22: literal True only — "yes"/1/"true" all become False (U2 fail-closed).
    for key in ("gemini_consent", "gemini_paid_key_confirmed", "first_run_guidance_seen"):
        result[key] = result.get(key) is True
    # M21: readiness cache — non-dict -> {}; only str -> known-state entries
    # (crash residue such as "downloading" drops to absent); unknown model_ids live.
    models = result.get("local_models")
    result["local_models"] = ({k: v for k, v in models.items()
                               if isinstance(k, str) and v in _MODEL_STATES}
                              if isinstance(models, dict) else {})
    # M23: quota overrides — only str -> positive int entries (bool is not a limit).
    limits = result.get("quota_limits")
    result["quota_limits"] = ({k: v for k, v in limits.items()
                               if isinstance(k, str) and type(v) is int and v > 0}
                              if isinstance(limits, dict) else {})
    return result


def coerce_key_map(value) -> dict:
    """K1: secret-map coercion — only ``str -> str`` envelope entries survive
    (never raises; non-map input becomes an empty map)."""
    return ({key: entry for key, entry in value.items()
             if isinstance(key, str) and isinstance(entry, str)}
            if isinstance(value, dict) else {})


def base64_fallback_count(keys_map) -> int:
    """D9 warning input: stored entries using the base64 fallback — a COUNT
    ONLY, so the aggregated per-load warning can never leak key bytes (D11)."""
    return sum(1 for entry in coerce_key_map(keys_map).values()
               if entry.startswith("base64:"))


def migrate_legacy_key(keys_map, legacy, vendor, protect) -> dict:
    """K2 (with K7's misattribution guard): the legacy single key migrates ONLY
    under the currently configured provider, and an existing slot always wins
    (idempotent — legacy never overwrites).  A protect failure (Windows DPAPI
    unavailable) returns the map unchanged: the value stays an in-memory legacy
    and the disk write fails loudly instead of downgrading to plaintext."""
    updated = coerce_key_map(keys_map)
    if not isinstance(legacy, str) or not legacy or vendor in updated:
        return updated
    try:
        return coerce_key_map({**updated, vendor: protect(legacy)})
    except RuntimeError:
        return updated


def derive_current_key(keys_map, vendor, unprotect):
    """K3: the map is authoritative — decrypted key for the CURRENT provider.
    Returns ``None`` when that vendor has NO slot (caller keeps its in-memory
    legacy value: the DPAPI-down fallback), and an existing slot always beats
    any legacy blob — the legacy value never bypasses the map."""
    entry = coerce_key_map(keys_map).get(vendor)
    return unprotect(entry) if isinstance(entry, str) else None


def read_vendor_key(keys_map, vendor, unprotect) -> str:
    """K7 read: decrypted key for ONE vendor slot; ``''`` when missing or
    undecryptable — a corrupt entry fails only itself, other vendors intact."""
    entry = coerce_key_map(keys_map).get(vendor)
    return unprotect(entry) if isinstance(entry, str) else ""


def with_vendor_key(keys_map, vendor, plaintext, protect):
    """K7 write: protect ``plaintext`` into the vendor slot and return the NEW
    map; ``None`` = invalid slot (caller reports False).  Protect failures
    PROPAGATE — a Windows DPAPI failure must never downgrade to plaintext."""
    if not isinstance(vendor, str) or not vendor.strip():
        return None
    if not isinstance(plaintext, str):
        plaintext = ""
    updated = coerce_key_map(keys_map)
    updated[vendor] = protect(plaintext)
    return updated


def disk_key_map(keys_map, vendor, blob) -> dict:
    """K4 dual-write (Release A): per-vendor map for disk — the CURRENT
    provider's slot takes the fresh blob, other vendors pass through untouched;
    ``str -> str`` only; plaintext never stored."""
    updated = coerce_key_map(keys_map)
    if isinstance(vendor, str) and isinstance(blob, str):
        updated[vendor] = blob
    return updated


def needs_secret_rewrite(raw_config) -> bool:
    """K8: rewrite once on first load of a plaintext or pre-map file (strip
    plaintext from disk / backfill the vendor map); later loads see the map and
    skip — the second load performs no write (idempotent)."""
    if not isinstance(raw_config, dict):
        return False
    return "api_key" in raw_config and (
        "api_key_protected" not in raw_config or "api_keys_protected" not in raw_config)
