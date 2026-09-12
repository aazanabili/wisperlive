import ctypes
import os
import threading
import traceback
from ctypes import wintypes


WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
HOTKEY_ID = 1

MODIFIERS = {
    "alt": MOD_ALT,
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "shift": MOD_SHIFT,
    "windows": MOD_WIN,
    "win": MOD_WIN,
}

VIRTUAL_KEYS = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "pause": 0x13,
    "caps lock": 0x14,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "page up": 0x21,
    "prior": 0x21,
    "page down": 0x22,
    "next": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "print screen": 0x2C,
    "insert": 0x2D,
    "delete": 0x2E,
    "num lock": 0x90,
    "scroll lock": 0x91,
    ";": 0xBA,
    "semicolon": 0xBA,
    "=": 0xBB,
    "equal": 0xBB,
    ",": 0xBC,
    "comma": 0xBC,
    "-": 0xBD,
    "minus": 0xBD,
    ".": 0xBE,
    "period": 0xBE,
    "/": 0xBF,
    "slash": 0xBF,
    "`": 0xC0,
    "grave": 0xC0,
    "[": 0xDB,
    "bracketleft": 0xDB,
    "\\": 0xDC,
    "backslash": 0xDC,
    "]": 0xDD,
    "bracketright": 0xDD,
    "'": 0xDE,
    "apostrophe": 0xDE,
}


class HotkeyError(ValueError):
    pass


def parse_shortcut(shortcut):
    parts = [part.strip().lower() for part in shortcut.split("+") if part.strip()]
    if not parts:
        raise HotkeyError("Enter a shortcut")

    modifier_flags = 0
    key_name = None
    for part in parts:
        if part in MODIFIERS:
            modifier_flags |= MODIFIERS[part]
        elif key_name is None:
            key_name = part
        else:
            raise HotkeyError("A shortcut can contain only one non-modifier key")

    if key_name is None:
        raise HotkeyError("Add a non-modifier key to the shortcut")

    if len(key_name) == 1 and key_name.isascii() and key_name.isalnum():
        virtual_key = ord(key_name.upper())
    elif key_name.startswith("f") and key_name[1:].isdigit() and 1 <= int(key_name[1:]) <= 24:
        virtual_key = 0x70 + int(key_name[1:]) - 1
    else:
        virtual_key = VIRTUAL_KEYS.get(key_name)

    if virtual_key is None:
        raise HotkeyError(f"Unsupported shortcut key: {key_name}")
    return modifier_flags, virtual_key


class WindowsGlobalHotkey:
    def __init__(self, shortcut, callback):
        self.modifiers, self.virtual_key = parse_shortcut(shortcut)
        self.callback = callback
        self._thread = None
        self._thread_id = None
        self._ready = threading.Event()
        self._startup_error = None
        self._user32 = None

    def start(self):
        if os.name != "nt":
            raise HotkeyError("Global shortcuts are supported only on Windows")
        if self._thread and self._thread.is_alive():
            return

        self._ready.clear()
        self._startup_error = None
        self._thread = threading.Thread(target=self._run, name="whisperlive-hotkey", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=2):
            self.stop()
            raise HotkeyError("Windows did not respond while registering the shortcut")
        if self._startup_error:
            self.stop()
            raise self._startup_error

    def stop(self):
        if self._thread_id is not None and self._user32 is not None:
            self._user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._thread = None
        self._thread_id = None
        self._user32 = None

    def is_main_key_pressed(self):
        return bool(self._user32 and self._user32.GetAsyncKeyState(self.virtual_key) & 0x8000)

    def _run(self):
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        self._user32.RegisterHotKey.restype = wintypes.BOOL
        self._user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self._user32.UnregisterHotKey.restype = wintypes.BOOL
        self._user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        self._user32.GetMessageW.restype = wintypes.BOOL
        self._user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self._user32.PostThreadMessageW.restype = wintypes.BOOL
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self._thread_id = kernel32.GetCurrentThreadId()
        registered = False
        try:
            ctypes.set_last_error(0)
            registered = bool(
                self._user32.RegisterHotKey(
                    None, HOTKEY_ID, self.modifiers | MOD_NOREPEAT, self.virtual_key
                )
            )
            if not registered:
                error = ctypes.get_last_error()
                message = ctypes.FormatError(error).strip() if error else "the shortcut is already in use"
                self._startup_error = HotkeyError(f"Could not register shortcut: {message}")
                return
        finally:
            self._ready.set()

        message = wintypes.MSG()
        try:
            while True:
                result = self._user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                if message.message == WM_HOTKEY and message.wParam == HOTKEY_ID:
                    try:
                        self.callback()
                    except Exception:
                        # A UI callback failure must not unregister the shortcut permanently.
                        traceback.print_exc()
        finally:
            if registered:
                self._user32.UnregisterHotKey(None, HOTKEY_ID)
