"""Clipboard save/restore and keystroke injection primitives (Windows/Linux).

The classic one-shot ``paste_text`` (with its restore timer) lives here beside
the streaming primitives: the ONE ``SessionClipboard`` (D4, restore-once) and
the Win32 ``SendInput``/``KEYEVENTF_UNICODE`` unicode typing + select-back
seams that ``typer_actor`` drives — policy/ordering stay in the actor.
"""

import ctypes
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


class SessionClipboard:
    """ONE session-scoped clipboard manager (D4 / A6 s4.4).

    Streaming modes ban PER-PHRASE restore timers: the session owns the
    clipboard from ``open()`` (first use) until ONE ``close()`` at session end,
    whose restore uses the classic equality guard so a user copy made in the
    meantime is never clobbered.  Repeated ``close()`` is a no-op — exactly one
    restore per session, ever.
    """

    def __init__(self):
        self._saved: str | None = None
        self._last_text: str | None = None
        self._opened = False
        self._closed = False

    def open(self) -> None:
        """Save the current clipboard ONCE (session start / first use)."""
        if self._opened or self._closed:
            return
        self._saved = _read_clipboard_safe()  # None = unreadable -> close() skips restore
        self._opened = True

    def put_and_paste(self, text: str) -> None:
        """Copy the phrase and paste it (Linux/X11 streaming path, D4).

        NO restore timer is scheduled here (BANNED in streaming): the clipboard
        intentionally keeps OUR text until the session closes.  Raises
        ``PasteError`` before any keystroke on copy/paste failure.
        """
        self.open()
        _copy_with_retry(text)
        self._last_text = text
        try:
            _send_paste_keystroke()
        except Exception as error:
            raise PasteError("Could not send the paste keystroke to the active window.") from error

    def copy_only(self, text: str) -> None:
        """Copy WITHOUT pasting and WITHOUT restarting the session scope — used
        to preserve undelivered phrases at session end (after the single
        restore), so nothing typed is ever silently lost (D4)."""
        if not text:
            return
        self.open()
        _copy_with_retry(text)
        self._last_text = text

    def close(self) -> None:
        """Restore the pre-session clipboard EXACTLY ONCE (idempotent)."""
        if not self._opened or self._closed:
            return
        self._closed = True
        if self._saved is None or self._last_text is None:
            return  # nothing readable, or nothing of ours was copied
        try:
            if pyperclip.paste() == self._last_text:  # equality guard (classic rule)
                pyperclip.copy(self._saved)
        except pyperclip.PyperclipException:
            logger.debug("Clipboard restore skipped (clipboard unavailable)")


# --- keystroke injection primitives (D4: extracted from typer_actor) --------
# The actor owns POLICY/ordering; these own the raw platform injection so the
# clipboard module stays the single keystroke home and both modules stay small.

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_SHIFT = 0x10
VK_LEFT = 0x25
MAX_INPUT_EVENTS = 256             # bounded batches per SendInput call (A6 s4.3)
_input_types = None                # lazy ctypes INPUT/KEYBDINPUT cache (Windows only)


def _win_input_types():
    """Build (once) the Win32 INPUT/KEYBDINPUT ctypes layout for SendInput.

    The full INPUT union includes MOUSEINPUT/HARDWAREINPUT so the struct size
    matches the ABI — an undersized INPUT array would corrupt SendInput.
    """
    global _input_types
    if _input_types is not None:
        return _input_types

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", ctypes.c_uint16), ("wScan", ctypes.c_uint16),
                    ("dwFlags", ctypes.c_uint32), ("time", ctypes.c_uint32),
                    ("dwExtraInfo", ctypes.c_size_t)]

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_int32), ("dy", ctypes.c_int32),
                    ("mouseData", ctypes.c_uint32), ("dwFlags", ctypes.c_uint32),
                    ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_size_t)]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", ctypes.c_uint32), ("wParamL", ctypes.c_uint16),
                    ("wParamH", ctypes.c_uint16)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]

    class _INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", ctypes.c_uint32), ("u", _INPUTUNION)]

    _input_types = (_INPUT, _KEYBDINPUT)
    return _input_types


def _send_input_batch(events) -> int:
    """Send ``(wVk, wScan, dwFlags)`` triples via user32.SendInput (Windows).

    Mockable ctypes wrapper (tests patch THIS seam): chunks to
    ``MAX_INPUT_EVENTS`` per call, returns what the OS accepted, and raises
    ``PasteError`` when fewer events were accepted than sent — a half-sent
    injection must surface, never pretend success.
    """
    if os.name != "nt" or not events:
        return 0
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    input_type, _keybd = _win_input_types()
    accepted = 0
    for start in range(0, len(events), MAX_INPUT_EVENTS):
        chunk = events[start:start + MAX_INPUT_EVENTS]
        array = (input_type * len(chunk))()
        for index, (virtual, scan, flags) in enumerate(chunk):
            array[index].type = INPUT_KEYBOARD
            array[index].ki.wVk = virtual
            array[index].ki.wScan = scan
            array[index].ki.dwFlags = flags
        sent = int(user32.SendInput(len(chunk), array, ctypes.sizeof(input_type)))
        accepted += sent
        if sent != len(chunk):
            break
    if accepted != len(events):
        raise PasteError("Could not inject keystrokes into the active window.")
    return accepted


def send_unicode_text(text: str) -> None:
    """Type ``text`` via KEYEVENTF_UNICODE (D4): one key-down + key-up pair per
    UTF-16 code unit — scan codes CANNOT emit Arabic, which is why this exists.
    ``_send_input_batch`` is the mocked test seam for the wide-char payload."""
    if not text:
        return
    encoded = text.encode("utf-16-le")            # surrogate pairs -> 2 units
    events = []
    for offset in range(0, len(encoded), 2):
        unit = encoded[offset] | (encoded[offset + 1] << 8)
        events.append((0, unit, KEYEVENTF_UNICODE))
        events.append((0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    _send_input_batch(events)


def tap_shift_left(count: int) -> None:
    """SELECT-BACK helper (U.4): SHIFT held + ``count`` x VK_LEFT taps — char-
    precise, bounded by the polish caller to one span.  Windows-only; Linux
    polish stays insert-only (A6 s4.2), and the caller falls back on error."""
    if count <= 0:
        return
    if os.name != "nt":
        raise PasteError("Select-back replace is available on Windows only; using insert instead.")
    events = [(VK_SHIFT, 0, 0)]
    for _ in range(int(count)):
        events.append((VK_LEFT, 0, 0))
        events.append((VK_LEFT, 0, KEYEVENTF_KEYUP))
    events.append((VK_SHIFT, 0, KEYEVENTF_KEYUP))
    _send_input_batch(events)
