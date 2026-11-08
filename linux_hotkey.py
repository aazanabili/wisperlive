"""Global hotkeys and input synthesis for Linux.

Talks to X11 (libX11 for hotkeys and key state, libXtst for paste) through
ctypes, so no additional Python packages are required.  Wayland-only
sessions are not supported: X11 or Xwayland must be available for the
global shortcut and for pasting into other applications.

Only module imports are safe without the X11 libraries; the libraries
themselves are loaded lazily on first use.
"""

import ctypes
import ctypes.util
import threading
import traceback

from windows_hotkey import HotkeyError, parse_shortcut_parts

# X11 modifier masks (X.h).
SHIFT_MASK = 1 << 0
LOCK_MASK = 1 << 1
CONTROL_MASK = 1 << 2
ALT_MASK = 1 << 3  # Mod1Mask, the usual Alt/Super-Alt placement
NUMLOCK_MASK = 1 << 4  # Mod2Mask, where NumLock lives on virtually all layouts
SUPER_MASK = 1 << 6  # Mod4Mask, the usual Super/Windows key placement

KEY_PRESS = 2
GRAB_MODE_ASYNC = 1

BAD_ACCESS = 10

KEYSYM_CONTROL_L = 0xFFE3
KEYSYM_F1 = 0xFFBE

# Named keys use X keysyms; single ASCII characters use their code point,
# which equals the X keysym for Latin-1.
KEYSYMS = {
    "backspace": 0xFF08,
    "tab": 0xFF09,
    "enter": 0xFF0D,
    "return": 0xFF0D,
    "pause": 0xFF13,
    "caps lock": 0xFFE5,
    "esc": 0xFF1B,
    "escape": 0xFF1B,
    "space": 0x0020,
    "page up": 0xFF55,
    "prior": 0xFF55,
    "page down": 0xFF56,
    "next": 0xFF56,
    "end": 0xFF57,
    "home": 0xFF50,
    "left": 0xFF51,
    "up": 0xFF52,
    "right": 0xFF53,
    "down": 0xFF54,
    "print screen": 0xFF61,
    "insert": 0xFF63,
    "delete": 0xFFFF,
    "num lock": 0xFF7F,
    "scroll lock": 0xFF14,
    ";": 0x003B,
    "semicolon": 0x003B,
    "=": 0x003D,
    "equal": 0x003D,
    ",": 0x002C,
    "comma": 0x002C,
    "-": 0x002D,
    "minus": 0x002D,
    ".": 0x002E,
    "period": 0x002E,
    "/": 0x002F,
    "slash": 0x002F,
    "`": 0x0060,
    "grave": 0x0060,
    "[": 0x005B,
    "bracketleft": 0x005B,
    "\\": 0x005C,
    "backslash": 0x005C,
    "]": 0x005D,
    "bracketright": 0x005D,
    "'": 0x0027,
    "apostrophe": 0x0027,
}

MODIFIERS = {
    "alt": ALT_MASK,
    "ctrl": CONTROL_MASK,
    "control": CONTROL_MASK,
    "shift": SHIFT_MASK,
    "windows": SUPER_MASK,
    "win": SUPER_MASK,
}


class _XAnyEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
    ]


class _XErrorEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("resourceid", ctypes.c_ulong),
        ("serial", ctypes.c_ulong),
        ("error_code", ctypes.c_ubyte),
        ("request_code", ctypes.c_ubyte),
        ("minor_code", ctypes.c_ubyte),
    ]


_ERROR_HANDLER_TYPE = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

_x11_lib = None
_xtst_lib = None


def _load_library(name, soname):
    path = ctypes.util.find_library(name)
    return ctypes.CDLL(path or soname)


def _x11():
    global _x11_lib
    if _x11_lib is None:
        lib = _load_library("X11", "libX11.so.6")
        # Xlib needs XInitThreads() before any other call so the Tk thread
        # can query key state while the hotkey thread waits for events.
        lib.XInitThreads()
        lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        lib.XOpenDisplay.restype = ctypes.c_void_p
        lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        lib.XDefaultRootWindow.restype = ctypes.c_ulong
        lib.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        lib.XKeysymToKeycode.restype = ctypes.c_ubyte
        lib.XGrabKey.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ulong,
            ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ]
        lib.XUngrabKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ulong]
        lib.XSetErrorHandler.argtypes = [_ERROR_HANDLER_TYPE]
        lib.XSetErrorHandler.restype = _ERROR_HANDLER_TYPE
        lib.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.XPending.argtypes = [ctypes.c_void_p]
        lib.XPending.restype = ctypes.c_int
        lib.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        lib.XQueryKeymap.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        lib.XFlush.argtypes = [ctypes.c_void_p]
        _x11_lib = lib
    return _x11_lib


def _xtst():
    global _xtst_lib
    if _xtst_lib is None:
        lib = _load_library("Xtst", "libXtst.so.6")
        lib.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        _xtst_lib = lib
    return _xtst_lib


def parse_shortcut(shortcut):
    """Return the X11 modifier mask and keysym for a shortcut string."""
    modifiers, key_name = parse_shortcut_parts(shortcut)

    modifier_mask = 0
    for name in modifiers:
        modifier_mask |= MODIFIERS[name]

    if len(key_name) == 1 and key_name.isascii() and key_name.isalnum():
        keysym = ord(key_name)
    elif key_name.startswith("f") and key_name[1:].isdigit() and 1 <= int(key_name[1:]) <= 35:
        keysym = KEYSYM_F1 + int(key_name[1:]) - 1
    else:
        keysym = KEYSYMS.get(key_name)

    if keysym is None:
        raise HotkeyError(f"Unsupported shortcut key: {key_name}")
    return modifier_mask, keysym


class LinuxGlobalHotkey:
    """Register a global X11 hotkey; same interface as WindowsGlobalHotkey."""

    def __init__(self, shortcut, callback):
        self.modifiers, self.keysym = parse_shortcut(shortcut)
        self.callback = callback
        self._thread = None
        self._display = None
        self._keycode = 0
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._startup_error = None
        self._error_handler = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._ready.clear()
        self._stop.clear()
        self._startup_error = None
        self._thread = threading.Thread(target=self._run, name="whisperlive-hotkey", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=2):
            self.stop()
            raise HotkeyError("X11 did not respond while registering the shortcut")
        if self._startup_error:
            error = self._startup_error
            self.stop()
            raise error

    def stop(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._thread = None

    def is_main_key_pressed(self):
        display = self._display
        if not display or not self._keycode:
            return False
        keys = (ctypes.c_ubyte * 32)()
        _x11().XQueryKeymap(display, keys)
        return bool(keys[self._keycode // 8] & (1 << (self._keycode % 8)))

    def _record_error(self, errors, event_ptr):
        if event_ptr:
            event = ctypes.cast(event_ptr, ctypes.POINTER(_XErrorEvent)).contents
            errors.append(event.error_code)
        return 0

    def _run(self):
        x11 = _x11()
        grabbed_masks = []
        try:
            self._display = x11.XOpenDisplay(None)
            display = self._display
            if not display:
                self._startup_error = HotkeyError(
                    "X11 display is unavailable (a Wayland-only session?)"
                )
                return
            self._keycode = x11.XKeysymToKeycode(display, self.keysym)
            if not self._keycode:
                self._startup_error = HotkeyError("Could not register shortcut: unknown key")
                return
            root = x11.XDefaultRootWindow(display)

            errors = []
            self._error_handler = _ERROR_HANDLER_TYPE(
                lambda _display_ptr, event_ptr: self._record_error(errors, event_ptr)
            )
            x11.XSetErrorHandler(self._error_handler)
            try:
                # Grab the shortcut for every CapsLock/NumLock state so the
                # hotkey works regardless of the lock keys.
                for noise in (0, LOCK_MASK, NUMLOCK_MASK, LOCK_MASK | NUMLOCK_MASK):
                    mask = self.modifiers | noise
                    x11.XGrabKey(
                        display, self._keycode, mask, root, True,
                        GRAB_MODE_ASYNC, GRAB_MODE_ASYNC,
                    )
                    grabbed_masks.append(mask)
                x11.XSync(display, False)
                if errors:
                    self._startup_error = HotkeyError(
                        "Could not register shortcut: it is already in use"
                    )
            finally:
                # Registration is finished (success or failure): release
                # start(), which waits for this before the event loop runs.
                self._ready.set()

            if self._startup_error:
                return

            event_buffer = (ctypes.c_long * 24)()
            while not self._stop.is_set():
                while x11.XPending(display) > 0:
                    x11.XNextEvent(display, ctypes.byref(event_buffer))
                    event = ctypes.cast(event_buffer, ctypes.POINTER(_XAnyEvent)).contents
                    if event.type == KEY_PRESS:
                        try:
                            self.callback()
                        except Exception:
                            # A UI callback failure must not unregister the shortcut permanently.
                            traceback.print_exc()
                self._stop.wait(0.01)
        except OSError as error:
            self._startup_error = HotkeyError(f"Could not register shortcut: {error}")
        finally:
            display = self._display
            if display:
                root = x11.XDefaultRootWindow(display)
                for mask in grabbed_masks:
                    x11.XUngrabKey(display, self._keycode, mask, root)
                x11.XSync(display, False)
                x11.XCloseDisplay(display)
                self._display = None
            self._ready.set()


def send_paste():
    """Synthesise Ctrl+V through XTEST on the active X11 display."""
    x11 = _x11()
    xtst = _xtst()
    display = x11.XOpenDisplay(None)
    if not display:
        raise HotkeyError("X11 display is unavailable (a Wayland-only session?)")
    try:
        ctrl_keycode = x11.XKeysymToKeycode(display, KEYSYM_CONTROL_L)
        v_keycode = x11.XKeysymToKeycode(display, ord("v"))
        xtst.XTestFakeKeyEvent(display, ctrl_keycode, True, 0)
        xtst.XTestFakeKeyEvent(display, v_keycode, True, 0)
        xtst.XTestFakeKeyEvent(display, v_keycode, False, 0)
        xtst.XTestFakeKeyEvent(display, ctrl_keycode, False, 0)
        x11.XFlush(display)
    finally:
        x11.XCloseDisplay(display)
