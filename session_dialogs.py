"""Session UX dialogs + status vocabulary (A5A7 s2/s3; D4/D6/U1 bindings).

WHY: the dialog layer is event-driven over the backend's public facts —
``note_status`` routes the session/actor status copies to the focus-guard
overlay (D4) and the quota choice dialog (D6, never a silent failover), and
``maybe_show_first_run`` renders the one-time kind guidance for U1 (in-memory
flag per A5A7 R2.2: at most once per app run; the persistent status hint is
the durable signal).  ``set_session_status`` is the KeyError-proof mapping
layer onto views.set_status's fixed 4-key palette.

Only the focus overlay refuses grab/focus (PRODUCT: never steal focus); the
other dialogs are ordinary transient windows.  No polling lives here (R2.4:
focus detection belongs to ``focus_guard``).
"""

import tkinter as tk

from llm_errors import DictationError, LLMError, ModelNotDownloadedError
from themes import COLORS

PALETTES = ("ready", "recording", "processing", "error")     # views.set_status's fixed keys

# A5A7 s2.1 status vocabulary: exact copy, palette inside the 4-key whitelist.
STATUS_VOCAB = {
    "recording": ("recording", "Recording in progress. Press the shortcut to stop."),
    "draining": ("processing", "Finishing the last words\u2026"),
    "typing": ("recording", "Live \u2014 typing confirmed phrases\u2026"),
    "paused_focus": ("processing",
                     "Dictation paused: window changed. Resume, or copy to clipboard."),
    "quota_exhausted": ("error", "{engine} quota used up until {reset}. Choose how to continue."),
    "degraded": ("processing", "Running reduced: {reason}."),
    "processing": ("processing", "Processing your recording\u2026"),
    "ready": ("ready", "Done. Ready for the next recording."),
    "blocked_first_run": ("processing",
                          "Local model not downloaded yet \u2014 open Settings \u2192 Models \u2192 Download."),
    "error": ("error", "Something needs attention. Open Settings to review it."),
}

# Status-text markers -> event routing (note_status).  PAUSE_MARKER must keep
# matching typer_actor's focus copy — QA drift-checks both strings together.
PAUSE_MARKER = "target window changed"
QUOTA_MARKER = "Quota exhausted"

_guidance_shown = False               # U1: at most ONCE per app run (in-memory)


def set_session_status(app, vocab_id, **facts) -> bool:
    """Map a vocabulary id onto the existing 4-key palette (KeyError-proof:
    unknown ids or missing format facts never raise and never invent a state).
    Returns False when nothing was rendered."""
    entry = STATUS_VOCAB.get(vocab_id)
    if entry is None:
        return False
    palette, template = entry
    if palette not in PALETTES:
        palette = "processing"
    try:
        text = template.format(**facts)
    except (KeyError, IndexError, ValueError):
        text = str(facts.get("detail", template))
    app.set_status(text, palette)
    return True


def note_status(app, text) -> None:
    """Event router (A5A7 R2.4): the backend surfaces pauses/quota through the
    status line; this turns them into the owning dialogs.  Idempotent."""
    if not isinstance(text, str):
        return
    if PAUSE_MARKER in text:
        show_focus_overlay(app)
    elif text.startswith(QUOTA_MARKER):
        show_quota_dialog(app)


def _joined_transcript(app) -> str:
    session = getattr(app, "dictation", None)
    if session is None:
        return ""
    return " ".join(item.text for item in session.emissions if item.text).strip()


def _engine_label(engine_id: str) -> str:
    """Human engine label from the providers registry (no duplicated names)."""
    import providers
    if engine_id == "groq-whisper":
        return "Groq Whisper"
    preset = providers.get_provider(engine_id.split(":", 1)[-1])
    return preset.label if preset is not None else engine_id


def _choice_dialog(app, title, body, buttons, *, escape_index=0):
    """Generic transient choice window: ``buttons`` = [(label, primary, enabled,
    command), ...]; exactly one primary (DESIGN.md rule 4).  Escape activates
    ``escape_index`` (never a destructive default)."""
    dialog = tk.Toplevel(app.root)
    dialog.title(title)
    dialog.transient(app.root)
    dialog.configure(bg=COLORS["surface"])
    dialog.resizable(False, False)
    tk.Label(dialog, text=title, bg=COLORS["surface"], fg=COLORS["text"],
             font=("Segoe UI Semibold", 11), wraplength=420, justify=tk.LEFT).pack(
        fill=tk.X, padx=20, pady=(18, 6))
    tk.Label(dialog, text=body, bg=COLORS["surface"], fg=COLORS["muted"],
             font=("Segoe UI", 9), wraplength=420, justify=tk.LEFT).pack(
        fill=tk.X, padx=20, pady=(0, 10))
    row = tk.Frame(dialog, bg=COLORS["surface"])
    row.pack(fill=tk.X, padx=20, pady=(0, 18))

    def wrap(command):
        def run(_event=None):
            if command is not None:
                command()
            dialog.destroy()
        return run

    for label, primary, enabled, command in reversed(buttons):
        button = app.create_button(row, label, wrap(command), secondary=not primary)
        button.pack(side=tk.RIGHT, padx=(8, 0))
        if not enabled:
            button.configure(state=tk.DISABLED)
    escape_command = wrap(buttons[escape_index][3])
    dialog.bind("<Escape>", escape_command)
    dialog.protocol("WM_DELETE_WINDOW", escape_command)
    return dialog


def show_focus_overlay(app):
    """D4 focus-guard overlay: Resume / Copy to clipboard / Discard — Escape is
    Resume (never destructive).  No grab, no focus_force: the overlay must not
    fight the user for focus (PRODUCT).  Idempotent (one overlay at a time)."""
    existing = getattr(app, "_focus_overlay", None)
    if existing is not None:
        try:
            existing.lift()
            return existing
        except tk.TclError:
            pass
    session = getattr(app, "dictation", None)
    app.beep(600, 120)
    overlay = tk.Toplevel(app.root)
    overlay.overrideredirect(True)
    overlay.configure(bg=COLORS["indicator"])
    overlay.attributes("-topmost", True)
    overlay.update_idletasks()
    width = 460
    height = 132
    x = max(0, (overlay.winfo_screenwidth() - width) // 2)
    y = max(0, overlay.winfo_screenheight() - height - 70)   # bottom-center, above the pill
    overlay.geometry(f"{width}x{height}+{x}+{y}")
    tk.Label(overlay, text="Dictation paused: window changed", bg=COLORS["indicator"],
             fg=COLORS["text"], font=("Segoe UI Semibold", 11)).pack(
        anchor=tk.W, padx=16, pady=(14, 4))
    tk.Label(overlay, text="WhisperLive stopped sending text because the active window "
             "changed. What you said so far is kept.", bg=COLORS["indicator"],
             fg=COLORS["indicator_text"], font=("Segoe UI", 9), wraplength=420,
             justify=tk.LEFT).pack(anchor=tk.W, padx=16)

    def resume(_event=None):
        # No status write here: Copy/Discard set theirs first, and Resume
        # re-baselines the actor's focus target on the CURRENT window (D4) so
        # an accepted resume actually re-arms typing.
        overlay.destroy()
        app._focus_overlay = None
        if session is not None and hasattr(session, "resume_focus"):
            session.resume_focus()

    def _copy():
        import pyperclip
        text = _joined_transcript(app)
        if text:
            pyperclip.copy(text)
        if session is not None:
            session.abort_all("focus_changed")
        app.set_status("Copied to clipboard. Ready for the next recording.", "ready")

    def discard():
        if session is not None:
            session.abort_all("focus_changed")
        app.set_status("Discarded. Ready for the next recording.", "ready")

    buttons = tk.Frame(overlay, bg=COLORS["indicator"])
    buttons.pack(anchor=tk.W, padx=16, pady=(10, 14))
    app.create_button(buttons, "Resume dictation",
                      lambda: resume(), secondary=False).pack(side=tk.LEFT, padx=(0, 8))
    app.create_button(buttons, "Copy to clipboard",
                      lambda: (_copy(), resume()), secondary=True).pack(side=tk.LEFT, padx=(0, 8))
    app.create_button(buttons, "Discard",
                      lambda: (discard(), resume()), secondary=True).pack(side=tk.LEFT)
    overlay.bind("<Escape>", resume)
    app._focus_overlay = overlay
    set_session_status(app, "paused_focus")
    return overlay


def show_quota_dialog(app):
    """D6 choice dialog (NO silent failover): Continue on local / Finish at
    stop / Copy to clipboard and stop; Escape = Finish at stop (keeps data,
    changes nothing).  Reads the session's latest exhaustion notice for facts."""
    existing = getattr(app, "_quota_dialog", None)
    if existing is not None:
        try:
            existing.lift()
            return existing
        except tk.TclError:
            pass
    session = getattr(app, "dictation", None)
    notices = [n for n in getattr(session, "notices", ()) if isinstance(n, dict)
               and n.get("kind") == "quota_exhausted"]
    notice = notices[-1] if notices else {}
    engine = _engine_label(notice.get("engine_id", ""))
    reset = notice.get("reset_hint", "") or "the vendor reset"
    set_session_status(app, "quota_exhausted", engine=engine, reset=reset)

    try:                                        # local availability = pure preflight (no download)
        if session is None:
            raise DictationError("no session")
        session.preflight({**getattr(app, "config", {}), "dictation_mode": "local"})
        local_ok = True
    except (DictationError, LLMError):
        local_ok = False

    def continue_local():
        app.config["dictation_mode"] = "local"
        from config_manager import save_config
        save_config(app.config)
        app.set_status("Settings saved. They apply to the next recording.", "ready")

    def copy_and_stop():
        import pyperclip
        text = _joined_transcript(app)
        if text:
            pyperclip.copy(text)
        if session is not None:
            session.abort_all("quota_choice")
        app.set_status("Copied to clipboard. Ready for the next recording.", "ready")

    def finish_at_stop():                       # default: drain keeps going, nothing lost
        if session is not None:
            session.request_stop()

    dialog = _choice_dialog(
        app, "Cloud quota exhausted",
        f"{engine} has no quota left until {reset}. WhisperLive never switches "
        "engines on its own.\nSuggested: copy to clipboard and stop \u2014 nothing "
        "is billed twice.",
        [("Continue on local", True, local_ok, continue_local if local_ok else None),
         ("Finish at stop", False, True, finish_at_stop),
         ("Copy to clipboard and stop", False, True, copy_and_stop)],
        escape_index=1)
    if not local_ok:
        tk.Label(dialog, text="On-device model not downloaded yet.",
                 bg=COLORS["surface"], fg=COLORS["muted"], font=("Segoe UI", 9),
                 wraplength=420, justify=tk.LEFT).pack(fill=tk.X, padx=20, pady=(0, 14))
    app._quota_dialog = dialog
    return dialog


def polish_from_tray(app):
    """Tray / R-U6 entry (A5A7 2.5): run Polish over the last dictation; with
    nothing to polish the exact kind copy renders (never a silent no-op)."""
    session = getattr(app, "dictation", None)
    result = None
    if session is not None:
        try:
            result = session.polish_last_dictation()
        except (DictationError, LLMError) as error:   # cleanup/transport failures are user-safe
            return app.set_status(str(error), "error")
    if result is None:
        app.set_status("Nothing to polish yet. Dictate something first.", "processing")
    return result


def maybe_show_first_run(app, session) -> bool:
    """U1 first-run guidance (A5A7 s2.2): pure ``session.preflight`` check —
    NEVER downloads (D7) — then a kind one-time dialog; the exact persistent
    status copy renders every blocked attempt.  In-memory once-flag (R2.2:
    at most one dialog per app run).  True when the attempt was model-blocked."""
    global _guidance_shown
    if session is None:
        return False
    try:
        session.preflight(getattr(app, "config", {}))
        return False                            # not model-blocked (other guards own their copy)
    except ModelNotDownloadedError:
        pass
    except (DictationError, LLMError):
        return False                            # session already posted its own status
    set_session_status(app, "blocked_first_run")  # exact A5A7 copy every attempt
    if _guidance_shown:
        return True                             # status+beep only afterwards (no dialog spam)
    _guidance_shown = True

    def open_models():
        try:
            app.show_settings(tab="models")
        except TypeError:                       # pre-tab signature (defensive, never crashes)
            app.show_settings()

    _choice_dialog(
        app, "One-time model download",
        "On-device dictation needs a speech model (about 401 MB \u2014 exact size "
        "shown before download). Nothing downloads until you press Download, and "
        "you can pause, resume, or delete it in Settings.",
        [("Open Settings \u2014 Models", True, True, open_models),
         ("Not now", False, True, None)],
        escape_index=1)
    return True
