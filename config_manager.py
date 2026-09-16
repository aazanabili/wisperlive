"""Persistence for the application's per-user configuration.

The file is deliberately located from the environment on every call.  This
is useful both for installed applications and for tests which change the
environment after importing this module.
"""

import base64
import ctypes
import json
import logging
import os
import sys
import tempfile
from pathlib import Path


CONFIG_FILE = "config.json"  # legacy basename (and the name used by callers)
CONFIG_SUBDIR = "WhisperLive"

DEFAULT_CONFIG = {
    "api_key": "",
    "target_language": "English",
    "shortcut": "ctrl+space",
    "mode": "toggle",
    "minimize_to_tray": True,
    "start_minimized": False,
    "run_at_startup": False,
    "theme": "dark",
    "installed_release_tag": "",
}


logger = logging.getLogger(__name__)


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _windows_dpapi(protect, value):
    """Call user-scoped DPAPI, returning None rather than leaking exceptions."""
    if os.name != "nt":
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
    if os.name == "nt":
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
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / CONFIG_SUBDIR
    return Path.home() / "AppData" / "Local" / CONFIG_SUBDIR


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
    result = DEFAULT_CONFIG.copy()
    result.update(config)
    if "api_key_protected" in result:
        result["api_key"] = _unprotect_api_key(result.pop("api_key_protected"))
    return result


def _disk_config(config):
    result = dict(config)
    api_key = result.pop("api_key", "")
    result["api_key_protected"] = _protect_api_key(api_key if isinstance(api_key, str) else "")
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
            # Older builds stored api_key directly.  Rewrite that file on
            # successful read so merely loading the app removes plaintext
            # secrets from disk (and remains safe if saving fails).
            if "api_key" in raw_config and "api_key_protected" not in raw_config:
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
        config = DEFAULT_CONFIG.copy()
        if not save_config(config):
            logger.warning("Could not create the default configuration")
        return config
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return DEFAULT_CONFIG.copy()
