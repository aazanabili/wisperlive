"""Clipboard save/restore around the paste keystroke (Windows/Linux)."""

import logging
import os
import threading
import time

import pyperclip

logger = logging.getLogger(__name__)

# Captured once (config_manager proven pattern): patchable in tests without
# touching global os.name (which would break pathlib).
_IS_WINDOWS = os.name == "nt"
# Lazy seam: importing keyboard on Linux needs root privileges; only Windows
# uses it. Linux pastes through XTEST via linux_hotkey.send_paste instead.
keyboard = None

CLIPBOARD_RETRY_ATTEMPTS = 2
CLIPBOARD_RETRY_DELAY_S = 0.05
PASTE_SETTLE_DELAY_S = 0.1        # existing 0.1 s behavior, now named
CLIPBOARD_RESTORE_DELAY_S = 1.0   # let the target app consume the paste before restoring


class PasteError(RuntimeError):
    """Pasting failed; str(error) is user-safe and may be shown in the UI."""


def _send_paste_keystroke() -> None:
    """Synthesise the paste keystroke on the current platform."""
    global keyboard
    if _IS_WINDOWS:
        if keyboard is None:
            import keyboard as _keyboard
            keyboard = _keyboard
        keyboard.send("ctrl+v")
    else:
        from linux_hotkey import send_paste  # upstream lazy import preserved
        send_paste()


def paste_text(text: str) -> None:
    if not text:
        return
    saved_clipboard = _read_clipboard_safe()  # None if unreadable; proceed without restore
    _copy_with_retry(text)                    # raises PasteError BEFORE any keystroke
    try:
        time.sleep(PASTE_SETTLE_DELAY_S)      # ensure clipboard is updated
        _send_paste_keystroke()
        logger.info("Pasted text into active window")
    except Exception as error:
        # Also converts linux_hotkey.HotkeyError / lazy-import OSError into
        # the single UI-safe PasteError surface.
        raise PasteError("Could not send the paste keystroke to the active window.") from error
    finally:
        _schedule_restore(saved_clipboard, text)  # runs even if send failed (clipboard holds our text)


def _read_clipboard_safe() -> str | None:
    try:
        return pyperclip.paste()
    except pyperclip.PyperclipException:
        logger.debug("Clipboard could not be read; paste will not be restored")
        return None


def _copy_with_retry(text: str) -> None:
    """Copy with a bounded retry for the transient 'clipboard busy' condition.
    Raises PasteError before any keystroke so stale clipboard content is never pasted."""
    last_error: pyperclip.PyperclipException | None = None
    for attempt in range(CLIPBOARD_RETRY_ATTEMPTS):
        try:
            pyperclip.copy(text)
            return
        except pyperclip.PyperclipException as error:
            last_error = error
            if attempt < CLIPBOARD_RETRY_ATTEMPTS - 1:
                time.sleep(CLIPBOARD_RETRY_DELAY_S)
    raise PasteError("The clipboard is busy. Please try again.") from last_error


def _schedule_restore(saved_clipboard: str | None, text: str) -> None:
    if saved_clipboard is None:
        return  # nothing readable to restore

    def restore() -> None:
        # Equality guard: restore only if nothing new was copied during the delay,
        # so a user copy made in the window is never clobbered.
        try:
            if pyperclip.paste() == text:
                pyperclip.copy(saved_clipboard)
        except pyperclip.PyperclipException:
            logger.debug("Clipboard restore skipped (clipboard unavailable)")

    timer = threading.Timer(CLIPBOARD_RESTORE_DELAY_S, restore)
    timer.daemon = True  # never hold the process open on quit
    timer.start()
