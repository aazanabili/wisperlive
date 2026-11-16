"""Pystray tray icon adapter (lazy + tolerant).

pystray invokes menu callbacks on its own thread, so every handler is
marshaled through the unified `call_in_ui` (passed in from main - this
module imports no app code). Lazy pystray/PIL imports inside the function:
without a usable tray backend the app keeps running with a normal taskbar
presence, so failures return None (window never unreachable).
"""

DISPLAY_NAME = "WhisperLive"  # tray identifier + tooltip (intentionally distinct from startup.APP_NAME)


def create_tray_icon(call_in_ui, image_path, on_restore, on_quit):
    """Build, start (detached), and return the tray icon, or None on failure.

    `image_path` is the resolved icon asset path (main computes it with
    resource_path) - keeping PIL inside this function preserves upstream's
    graceful degradation without tray.py importing app code.
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
    icon = pystray.Icon(
        DISPLAY_NAME, image, DISPLAY_NAME,
        menu=pystray.Menu(
            pystray.MenuItem("Show WhisperLive", lambda _icon, _item: call_in_ui(on_restore), default=True),
            pystray.MenuItem("Quit", lambda _icon, _item: call_in_ui(on_quit)),
        ),
    )
    icon.run_detached()
    return icon
