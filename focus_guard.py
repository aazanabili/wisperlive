"""Foreground-window identity + low-level key invalidation (D4 / D3, A6 s4.2-4.3).

WHY: every emission must land in the SAME window it was aimed at (D4 focus
guard), and any real user keystroke must invalidate the Polish span registry
(D3 — the target document changed under us).  Two mechanisms:

* ``check()`` raises ``FocusLostError`` when the foreground window differs
  from the session target captured on the FIRST emission (A6 s4.3).  Seams are
  INJECTED / lazy: Win32 ``GetForegroundWindow`` via ctypes; X11
  ``_NET_ACTIVE_WINDOW`` via raw libX11 (linux_hotkey style, no new deps).
  When no seam exists (Wayland-only, unknown OS) the guard DEGRADES HONESTLY:
  typing is never blocked on unobservable focus, ``unchanged()`` returns False
  (replace-paste needs PROOF, so polish falls back to insert), and a
  ``PLATFORM_REQUIRED`` note is logged once.

* ``start_key_hook()`` installs ONE ``WH_KEYBOARD_LL`` chain on Windows: events
  flagged ``LLKHF_INJECTED`` (0x10) are OUR OWN SendInput output and are
  ignored; any other key event calls the injected ``on_key`` callback (span
  invalidation).  Non-Windows: documented no-op — span validity then rests on
  focus + TTL only (A6 s4.2 approximation; no root-required X11 key tap).
"""

import ctypes
import logging
import os
import sys
import threading

from llm_errors import FocusLostError

logger = logging.getLogger(__name__)

WH_KEYBOARD_LL = 13
HC_ACTION = 0
LLKHF_INJECTED = 0x10              # A6 s4.2: our own injected keys carry this flag
WM_QUIT = 0x0012
HOOK_NOTE = ("WhisperLive: no low-level key hook on this platform (PLATFORM_REQUIRED); "
             "Polish spans rely on focus + TTL validity only (A6 s4.2 fallback).")
SEAM_NOTE = ("WhisperLive: foreground-window identity unavailable "
             "(PLATFORM_REQUIRED); focus guard degrades to TTL-only span validity.")

_windows_libs = {}                  # lazy ctypes cache (never at import time)
_seam_note_logged = False
_hook_note_logged = False


def _windows_foreground() -> int:
    """Win32 GetForegroundWindow() as a mockable seam (hwnd int, 0 = none)."""
    user32 = _windows_libs.get("user32")
    if user32 is None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        _windows_libs["user32"] = user32
    handle = user32.GetForegroundWindow()
    return int(handle or 0)


def _x11_foreground() -> int:
    """X11 _NET_ACTIVE_WINDOW via raw libX11 (linux_hotkey style; no new deps).

    Returns the active window id, or 0 when there is no X display (Wayland
    session, headless) — callers degrade honestly instead of guessing.
    """
    prop = ctypes.c_void_p()
    display = None
    try:
        import ctypes.util
        path = ctypes.util.find_library("X11")
        if not path:
            return 0
        lib = ctypes.CDLL(path)
        lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        lib.XOpenDisplay.restype = ctypes.c_void_p
        lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        lib.XDefaultRootWindow.restype = ctypes.c_ulong
        lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        lib.XInternAtom.restype = ctypes.c_ulong
        lib.XGetWindowProperty.restype = ctypes.c_int
        lib.XGetWindowProperty.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long,
            ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_void_p)]
        lib.XFree.argtypes = [ctypes.c_void_p]
        display = lib.XOpenDisplay(None)
        if not display:
            return 0
        atom = lib.XInternAtom(display, b"_NET_ACTIVE_WINDOW", 1)  # only-if-exists
        if not atom:
            return 0
        root = lib.XDefaultRootWindow(display)
        actual_type = ctypes.c_ulong()
        actual_format = ctypes.c_int()
        nitems = ctypes.c_ulong()
        after = ctypes.c_ulong()
        status = lib.XGetWindowProperty(
            display, root, atom, 0, 1, 0, 6,       # 6 = XA_WINDOW
            ctypes.byref(actual_type), ctypes.byref(actual_format),
            ctypes.byref(nitems), ctypes.byref(after), ctypes.byref(prop))
        if status != 0 or not prop or not nitems.value:
            return 0
        return int(ctypes.cast(prop, ctypes.POINTER(ctypes.c_ulong))[0])
    except Exception:                               # no X / Wayland / lib missing
        return 0
    finally:
        if display is not None:
            try:
                if prop:
                    lib.XFree(prop)
                lib.XCloseDisplay(display)
            except Exception:
                logger.debug("X11 cleanup failed", exc_info=True)


def default_foreground_seam():
    """Platform foreground seam, or None when the OS offers none honestly."""
    if os.name == "nt":
        return _windows_foreground
    if sys.platform.startswith("linux"):
        return _x11_foreground
    return None


class FocusGuard:
    """Per-session focus identity + span-invalidation hook (A6 s4.3/s4.2)."""

    def __init__(self, *, foreground_seam=None, on_key=None):
        self._seam = foreground_seam if foreground_seam is not None else default_foreground_seam()
        self._on_key = on_key
        self._target = None
        self._hook_thread = None
        self._hook_stop = None
        self._hook = None
        self._hook_thread_id = None
        self._hook_proc = None                      # keep a reference (ctypes GC rule)

    # ---------------- focus identity ---------------------------------------
    def check(self) -> None:
        """Raise ``FocusLostError`` when the target window changed (D4).

        Checked BEFORE EVERY emission.  Unobservable focus (no seam / seam
        returns nothing) never blocks typing — it degrades instead (documented
        PLATFORM_REQUIRED behavior).
        """
        if self._seam is None:
            return
        current = self._safe_current()
        if current in (None, 0):
            return                                  # unknown right now: degrade, don't block
        if self._target is None:
            self._target = current                  # first emission defines the target (A6 s4.3)
            return
        if current != self._target:
            raise FocusLostError(
                "Typing moved to another window. Re-focus the original window to "
                "continue; nothing was lost.")

    def unchanged(self) -> bool:
        """PROOF predicate for replace-experimental polish (A6 s4.5): only a
        measurable equal foreground counts as unchanged — unknown focus is NOT
        proof, so polish falls back to safe-insert."""
        if self._seam is None:
            return False
        current = self._safe_current()
        if current in (None, 0):
            return False
        if self._target is None:
            self._target = current
            return True
        return current == self._target

    def reset_target(self) -> None:
        """Forget the target (session start); the next check re-baselines."""
        self._target = None

    def _safe_current(self):
        global _seam_note_logged
        try:
            return self._seam()
        except Exception:
            if not _seam_note_logged:               # honest, logged ONCE
                _seam_note_logged = True
                logger.info(SEAM_NOTE)
            return None

    # ---------------- span invalidation hook (D3) ---------------------------
    def notify_key_event(self, injected: bool) -> bool:
        """Filter + dispatch: NON-injected keys invalidate Polish spans (our
        own SendInput output carries LLKHF_INJECTED and is ignored).  Returns
        whether the invalidation fired (testable seam for the hook proc)."""
        if injected:
            return False
        if self._on_key is not None:
            try:
                self._on_key()
            except Exception:                       # never raise into the hook chain
                logger.exception("Span invalidation callback failed")
        return True

    def start_key_hook(self) -> None:
        """Install ONE WH_KEYBOARD_LL chain (Windows only); honest no-op else."""
        global _hook_note_logged
        if os.name != "nt":
            if not _hook_note_logged:
                _hook_note_logged = True
                logger.info(HOOK_NOTE)
            return
        if self._hook_thread is not None:
            return
        self._hook_stop = threading.Event()
        self._hook_thread = threading.Thread(
            target=self._hook_loop, name="whisperlive-ll-keyhook", daemon=True)
        self._hook_thread.start()

    def stop_key_hook(self) -> None:
        """Unhook + end the message loop (bounded join; idempotent, never raises)."""
        thread = self._hook_thread
        self._hook_thread = None
        if self._hook_stop is not None:
            self._hook_stop.set()
        thread_id = self._hook_thread_id
        if thread_id:
            try:                                     # wake GetMessageW with WM_QUIT
                user32 = _windows_libs.get("user32") or ctypes.WinDLL("user32", use_last_error=True)
                _windows_libs.setdefault("user32", user32)
                user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
            except Exception:
                logger.debug("PostThreadMessageW failed during hook stop", exc_info=True)
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def _hook_loop(self) -> None:
        """WH_KEYBOARD_LL pump: install -> GetMessageW -> uninstall (finally)."""
        user32 = None
        try:
            user32 = _windows_libs.get("user32") or ctypes.WinDLL("user32", use_last_error=True)
            _windows_libs.setdefault("user32", user32)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

            class _KBDLLHOOKSTRUCT(ctypes.Structure):
                _fields_ = [("vkCode", ctypes.c_uint32), ("scanCode", ctypes.c_uint32),
                            ("flags", ctypes.c_uint32), ("time", ctypes.c_uint32),
                            ("dwExtraInfo", ctypes.c_size_t)]

            def _proc(n_code, w_param, l_param):
                try:
                    if n_code == HC_ACTION:
                        info = ctypes.cast(l_param,
                                           ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                        self.notify_key_event(bool(info.flags & LLKHF_INJECTED))
                except Exception:                    # never raise into the OS chain
                    logger.debug("LL key hook callback error", exc_info=True)
                return user32.CallNextHookEx(None, n_code, w_param, l_param)

            self._hook_proc = _WIN_HOOK_TYPE(_proc)   # ctypes keeps fn alive via ref
            self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._hook_proc, None, 0)
            self._hook_thread_id = kernel32.GetCurrentThreadId()
            message = _MSG()
            while not (self._hook_stop and self._hook_stop.is_set()):
                # GetMessageW blocks until PostThreadMessageW(WM_QUIT)/message.
                if not user32.GetMessageW(ctypes.byref(message), None, 0, 0):
                    break
        except Exception:
            logger.exception("Low-level key hook failed")   # degrade: TTL-only spans
        finally:
            hook = self._hook
            self._hook = None
            if hook and user32 is not None:
                try:
                    user32.UnhookWindowsHookEx(hook)
                except Exception:
                    logger.debug("UnhookWindowsHookEx failed", exc_info=True)


# ctypes callback/message types are declarations only (no OS call at import;
# WinDLL happens inside the hook thread).  MSG is the FULL Win32 layout — an
# undersized buffer passed to GetMessageW would be a memory-safety bug.
_WIN_HOOK_TYPE = ctypes.CFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                  ctypes.c_size_t, ctypes.c_size_t)


class _MSG(ctypes.Structure):
    class _POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint),
                ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_size_t),
                ("time", ctypes.c_uint32), ("pt", _POINT),
                ("lPrivate", ctypes.c_uint32)]
