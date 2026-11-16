"""Autostart-at-sign-in registration: Windows Run key + Linux XDG autostart.

Cross-platform, module-level API only (no class state). `set_startup_registration`
dispatches on platform exactly as upstream `main.py` did; this is the only
module that touches `winreg` or writes `.desktop` entries.
"""

import os
import sys
from pathlib import Path

if os.name == "nt":
    import winreg
else:
    winreg = None

STARTUP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "WhisperLive"
SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")


def build_startup_command() -> str:
    executable = os.path.abspath(sys.executable)
    if getattr(sys, "frozen", False):
        return f'"{executable}"'
    # SCRIPT_PATH is a module global read at call time so tests can patch it.
    return f'"{executable}" "{SCRIPT_PATH}"'


def set_startup_registration(enabled: bool) -> bool:
    """Enable/disable run-at-sign-in. Returns False on OSError (unchanged semantics)."""
    if os.name != "nt":
        return set_linux_startup_registration(enabled)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, build_startup_command())
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


def set_linux_startup_registration(enabled: bool) -> bool:
    """Manage the XDG autostart entry, the Linux equivalent of the Run key."""
    autostart_dir = _linux_autostart_dir()
    desktop_file = autostart_dir / f"{APP_NAME}.desktop"
    try:
        if enabled:
            autostart_dir.mkdir(parents=True, exist_ok=True)
            desktop_file.write_text(
                "\n".join([
                    "[Desktop Entry]",
                    "Type=Application",
                    f"Name={APP_NAME}",
                    f"Exec={build_startup_command()}",
                    "Terminal=false",
                    "X-GNOME-Autostart-enabled=true",
                    "",
                ]),
                encoding="utf-8",
            )
        else:
            try:
                desktop_file.unlink()
            except FileNotFoundError:
                pass
        return True
    except OSError:
        return False


def _linux_autostart_dir():
    base = os.environ.get("XDG_CONFIG_HOME")
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "autostart"
