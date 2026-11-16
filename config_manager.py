"""Persistence for the application's per-user configuration.

The file is deliberately located from the environment on every call.  This
is useful both for installed applications and for tests which change the
environment after importing this module.

It also owns dictation-modes secrets (D9 / A3 K1-K8): the per-vendor
``api_keys_protected`` envelope, the per-current-provider derived
``api_key``, and per-load deep-copied container defaults (A3 s1.1).
"""

import base64
import ctypes
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

import dictation_config
import providers


CONFIG_FILE = "config.json"  # legacy basename (and the name used by callers)
CONFIG_SUBDIR = "WhisperLive"

# Captured once at import: unlike the environment lookups below, the running
# platform never changes.  Tests patch this flag instead of ``os.name`` so
# they can exercise the Windows branches without breaking ``pathlib`` (which
# reads ``os.name`` globally when constructing paths).
_IS_WINDOWS = os.name == "nt"

DEFAULT_CONFIG = {
    "api_key": "",
    # --- provider presets (T-2026-11-16-provider-presets) ---
    "provider": "custom",        # preset id or "custom" (legacy default = exact old behaviour)
    "provider_variant": "",      # endpoint variant id ("" = provider default / N-A)
    "provider_base_url": "",
    "model": "",
    "transcription_model": "",   # stage-1 model; effective only for split-mode presets
    "target_language": "English",
    "shortcut": "ctrl+space",
    "mode": "toggle",
    "minimize_to_tray": True,
    "start_minimized": False,
    "run_at_startup": False,
    "theme": "dark",
    "installed_release_tag": "",
    # --- dictation modes (T-2026-11-16-dictation-modes; A3 s1.1 authoritative) ---
    "dictation_mode": "local",         # U4: local | chunked | live (default = local)
    "cleanup_mode": "cloud",           # D2: cloud | local-ollama | off (cleanup ON)
    "model_size": "auto",              # auto | fast | standard (faster-whisper tier)
    "chunked_engine": "",              # "" = inherit "provider" (never drifts, A3 s1.2)
    "chunked_asr_model": "",           # "" = engine-registry default (Groq ASR model)
    "live_engine": "local",            # local | gemini-live | openai-realtime (fail closed)
    "polish_mode": "insert",           # U3: insert | replace-experimental
    "gemini_consent": False,           # U2: DEFAULT OFF (free tier trains on content)
    "gemini_paid_key_confirmed": False,  # U2: explicit attestation, literal True only
    "cleanup_model": "",               # "" = inherit "model" (stage-2 semantics)
    "model_store_dir": "",             # "" = platform DATA dir default (devops s3)
    "local_models": {},                # model_id -> absent | ready | stale (M21)
    "api_keys_protected": {},          # D9: vendor id -> "dpapi:"/"base64:" envelope
    "first_run_guidance_seen": False,  # M22: kind one-time guidance flag
    "quota_limits": {},                # M23: "<engine_id>.<window_id>" -> int > 0
}


def _fresh_defaults():
    """DEFAULT_CONFIG with fresh containers — ``copy()`` is SHALLOW (A3 s1.1 hazard)."""
    return {key: (dict(value) if isinstance(value, dict) else value)
            for key, value in DEFAULT_CONFIG.items()}


logger = logging.getLogger(__name__)


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _windows_dpapi(protect, value):
    """Call user-scoped DPAPI, returning None rather than leaking exceptions."""
    if not _IS_WINDOWS:
        return None
    try:
        crypt = ctypes.windll.crypt32
        kernel = ctypes.windll.kernel32
        # Declare the Win32 ABI explicitly.  Without this, ctypes uses an
        # ``int`` return type by default, which truncates BOOL/HLOCAL values
        # on 64-bit Windows and makes LocalFree unsafe.
        crypt.CryptProtectData.argtypes = [
            ctypes.POINTER(_DATA_BLOB), ctypes.c_wchar_p,
            ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.POINTER(_DATA_BLOB),
        ]
        crypt.CryptProtectData.restype = ctypes.c_int
        crypt.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DATA_BLOB), ctypes.c_wchar_p,
            ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.POINTER(_DATA_BLOB),
        ]
        crypt.CryptUnprotectData.restype = ctypes.c_int
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        raw = value if isinstance(value, bytes) else value.encode("utf-8")
        source = ctypes.create_string_buffer(raw)
        in_blob = _DATA_BLOB(len(raw), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
        out_blob = _DATA_BLOB()
        if protect:
            ok = crypt.CryptProtectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob))
        else:
            ok = crypt.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob))
        if not ok or not out_blob.pbData:
            return None
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel.LocalFree(out_blob.pbData)
    except Exception:
        return None


def _protect_api_key(value):
    protected = _windows_dpapi(True, value)
    if protected is not None:
        return "dpapi:" + base64.b64encode(protected).decode("ascii")
    if _IS_WINDOWS:
        # Never downgrade a Windows user's secret to plaintext (or a
        # reversible encoding) when DPAPI is unavailable.
        raise RuntimeError("Windows DPAPI is unavailable")
    # Non-Windows has no stdlib DPAPI.  This is only an opaque storage
    # fallback; Windows always takes the DPAPI branch above.
    return "base64:" + base64.b64encode(value.encode("utf-8")).decode("ascii")


def _unprotect_api_key(value):
    if not isinstance(value, str):
        logger.warning("Stored API key has an invalid protected value; ignoring it")
        return ""
    try:
        if value.startswith("dpapi:"):
            plain = _windows_dpapi(False, base64.b64decode(value[6:], validate=True))
            if plain is None:
                logger.warning("Unable to decrypt stored API key with Windows DPAPI; ignoring it")
                return ""
            return plain.decode("utf-8")
        if value.startswith("base64:"):
            return base64.b64decode(value[7:], validate=True).decode("utf-8")
    except (ValueError, TypeError, UnicodeError, RuntimeError):
        logger.warning("Unable to decode stored API key; ignoring it")
    else:
        logger.warning("Stored API key uses an unknown protection format; ignoring it")
    return ""


def _config_directory():
    if _IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            return Path.home() / "AppData" / "Local" / CONFIG_SUBDIR
    else:
        # Follow the XDG Base Directory Specification on Linux and other
        # POSIX systems.
        base = os.environ.get("XDG_CONFIG_HOME")
        if not base:
            return Path.home() / ".config" / CONFIG_SUBDIR
    return Path(base) / CONFIG_SUBDIR


def get_config_path():
    return _config_directory() / CONFIG_FILE


def _legacy_paths():
    # Keep the order explicit: executable directory, source directory, cwd.
    paths = [Path(sys.executable).resolve().parent, Path(__file__).resolve().parent, Path(os.getcwd()).resolve()]
    result = []
    for directory in paths:
        candidate = directory / CONFIG_FILE
        if candidate not in result:
            result.append(candidate)
    return result


def _read(path):
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def _normalized(config):
    result = _fresh_defaults()
    result.update(config)
    if "api_key_protected" in result:
        result["api_key"] = _unprotect_api_key(result.pop("api_key_protected"))
    # Pure M1-M12 migration + registry resolution (never raises; api_key
    # handling above untouched — M12b keeps providers.py blind to secrets).
    result = providers.normalize_config(result)
    # K1 + D9 warning (dictation_config pure cores): str->str envelopes only;
    # ONE aggregated count per load, NEVER key bytes; STORED entries (pre-K2)
    # so a fresh migration cannot double-count within the same load.
    keys_map = dictation_config.coerce_key_map(result.get("api_keys_protected"))
    result["api_keys_protected"] = keys_map
    base64_count = dictation_config.base64_fallback_count(keys_map)
    if base64_count:
        logger.warning("%d stored API key(s) use the base64 fallback (obfuscation only) "
                       "on this platform; consider OS keyring integration.", base64_count)
    # K2 (K7 guard) / K3: legacy migrates only under the configured provider;
    # the map is authoritative for derived api_key (missing slot keeps legacy).
    vendor = providers.resolve_provider_id(result.get("provider"))
    keys_map = dictation_config.migrate_legacy_key(
        keys_map, result.get("api_key"), vendor, _protect_api_key)
    result["api_keys_protected"] = keys_map
    derived = dictation_config.derive_current_key(keys_map, vendor, _unprotect_api_key)
    if derived is not None:
        result["api_key"] = derived
    return result


def get_vendor_key(config, vendor):
    """K7 read: decrypted key for ONE vendor slot; '' when missing/undecryptable."""
    return dictation_config.read_vendor_key(
        config.get("api_keys_protected"), vendor, _unprotect_api_key)


def set_vendor_key(config, vendor, plaintext):
    """K7 write: protect plaintext into the vendor slot (raises like save_config
    — never a plaintext downgrade) and sync derived api_key for the current
    provider so switches never carry keys across.  False = invalid slot."""
    updated = dictation_config.with_vendor_key(
        config.get("api_keys_protected"), vendor, plaintext, _protect_api_key)
    if updated is None:
        return False
    config["api_keys_protected"] = updated
    if vendor == providers.resolve_provider_id(config.get("provider")):
        config["api_key"] = plaintext if isinstance(plaintext, str) else ""
    return True


def _disk_config(config):
    result = dict(config)
    api_key = result.pop("api_key", "")
    # K4 dual-write (Release A): map authoritative on disk AND legacy blob mirrors
    # the CURRENT provider's slot (downgrade safety); never plaintext on disk.
    blob = _protect_api_key(api_key if isinstance(api_key, str) else "")
    result["api_keys_protected"] = dictation_config.disk_key_map(
        result.get("api_keys_protected"), providers.resolve_provider_id(result.get("provider")), blob)
    result["api_key_protected"] = blob
    return result


def _atomic_write(path, config, no_overwrite=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    reservation = False
    if no_overwrite:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            reservation = True
        except FileExistsError:
            return False
    temporary = None
    replaced = False
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{CONFIG_FILE}.", dir=str(path.parent), text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(_disk_config(config), handle, indent=4, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        replaced = True
        return True
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        if no_overwrite and reservation and not replaced:
            try:
                path.unlink()
            except OSError:
                pass


def save_config(config):
    """Save the current (plaintext-in-memory) config atomically."""
    try:
        _atomic_write(get_config_path(), config)
    except (OSError, TypeError, ValueError, RuntimeError):
        return False
    return True


def load_config():
    target = get_config_path()
    try:
        if target.exists():
            raw_config = _read(target)
            config = _normalized(raw_config)
            # Older builds stored api_key directly; rewrite on first read to remove
            # plaintext.  K8 (dictation_config) also fires for files predating the
            # vendor map: the first post-upgrade load backfills it exactly once.
            if dictation_config.needs_secret_rewrite(raw_config):
                if not save_config(config):
                    logger.warning("Could not rewrite the legacy plaintext API key configuration")
            return config
        for legacy in _legacy_paths():
            if legacy.exists():
                try:
                    config = _normalized(_read(legacy))
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    continue
                try:
                    migrated = _atomic_write(target, config, no_overwrite=True)
                except (OSError, TypeError, ValueError, RuntimeError):
                    # Leave the legacy source untouched.  In particular, a
                    # failed DPAPI operation must not produce a plaintext
                    # target or escape as an unhandled startup exception.
                    logger.warning("Could not migrate the legacy configuration; keeping the legacy file")
                    return config
                if not migrated:
                    # Another process may have completed migration first.
                    # Prefer the canonical file rather than returning the
                    # lower-priority legacy value.
                    if target.exists():
                        try:
                            return _normalized(_read(target))
                        except (OSError, ValueError, TypeError, json.JSONDecodeError):
                            logger.warning("Could not read the concurrently migrated configuration")
                return config
        config = _fresh_defaults()
        if not save_config(config):
            logger.warning("Could not create the default configuration")
        return config
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return _fresh_defaults()
