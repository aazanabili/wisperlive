"""Pystray tray icon adapter (lazy + tolerant).

pystray invokes menu callbacks on its own thread, so every handler is
marshaled through the unified `call_in_ui` (passed in from main - this
module imports no app code). Lazy pystray/PIL imports inside the function:
without a usable tray backend the app keeps running with a normal taskbar
presence, so failures return None (window never unreachable).
"""

DISPLAY_NAME = "WhisperLive"  # tray identifier + tooltip (intentionally distinct from startup.APP_NAME)


def create_tray_icon(call_in_ui, image_path, on_restore, on_quit, on_polish=None):
    """Build, start (detached), and return the tray icon, or None on failure.

    `image_path` is the resolved icon asset path (main computes it with
    resource_path) - keeping PIL inside this function preserves upstream's
    graceful degradation without tray.py importing app code.
    `on_polish` (R-U6) adds the "Polish last dictation" item when provided —
    the callback is wired by the UI composition once a line of headroom exists
    there; without it the item is omitted (never a dead menu entry).
    """
    try:
        import pystray
        from PIL import Image

        image = Image.open(image_path).convert("RGBA")
    except Exception:
        # Without a usable tray backend the app keeps running with a
        # normal taskbar presence; hiding is disabled so the window
        # cannot become unreachable.
        return None
    items = [
        pystray.MenuItem("Show WhisperLive", lambda _icon, _item: call_in_ui(on_restore), default=True),
    ]
    if on_polish is not None:                       # R-U6: present only when wired
        items.append(pystray.MenuItem(
            "Polish last dictation", lambda _icon, _item: call_in_ui(on_polish)))
    items.append(pystray.MenuItem("Quit", lambda _icon, _item: call_in_ui(on_quit)))
    icon = pystray.Icon(
        DISPLAY_NAME, image, DISPLAY_NAME,
        menu=pystray.Menu(*items),
    )
    icon.run_detached()
    return icon
