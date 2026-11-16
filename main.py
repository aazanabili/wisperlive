import ctypes
import logging
import os
import queue
import sys
import threading

try:
    import tkinter as tk
except ImportError:  # tkinter is only required to run the app, not to import it
    tk = None

if os.name == "nt":
    import winsound
else:
    winsound = None

import app_logging
from audio_recorder import AudioRecorder, RecorderError
from auto_typer import PasteError, paste_text
from config_manager import load_config, save_config
from indicator import RecordingIndicator
from llm_api import DIALECT_OPENAI, LLMError, process_audio
import providers
from settings_view import SettingsDialogMixin
from single_instance import ALREADY_RUNNING_MESSAGE, acquire_instance_lock
from startup import set_startup_registration
from themes import COLORS, THEMES
from tray import create_tray_icon
from views import MainUiMixin

logger = logging.getLogger(__name__)

APP_ID = "AbdullatifZanabili.WhisperLive"


def resource_path(relative_path):
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


def _notify_already_running():
    """Explain why the app is not starting: terminal output plus a GUI dialog."""
    logger.error(ALREADY_RUNNING_MESSAGE)
    if tk is None:
        return
    try:
        from tkinter import messagebox
        dialog = tk.Tk()
        dialog.withdraw()
        messagebox.showerror("WhisperLive", ALREADY_RUNNING_MESSAGE, master=dialog)
        dialog.destroy()
    except Exception:
        logger.warning("Could not display the single-instance dialog", exc_info=True)


def _acquire_single_instance_lock_or_exit():
    """Keep a single running instance per user session (see issue #14).

    Returns the held lock, or exits with code 1 after notifying the user.
    """
    try:
        lock = acquire_instance_lock()
    except OSError as exc:
        logger.error("Could not acquire the single-instance lock: %s", exc)
        raise SystemExit(1)
    if lock is None:
        _notify_already_running()
        raise SystemExit(1)
    return lock


class WhisperLiveApp(MainUiMixin, SettingsDialogMixin):
    def __init__(self, root):
        self.root = root
        self.root.title("WhisperLive")
        self.root.geometry("700x720")
        self.root.minsize(620, 620)

        self.config = load_config()
        COLORS.clear()
        COLORS.update(THEMES.get(self.config.get("theme"), THEMES["dark"]))
        self.root.configure(bg=COLORS["background"])
        if os.name == "nt":
            self.root.iconbitmap(resource_path("assets/whisperlive.ico"))
        self.window_icon = None
        try:
            from PIL import Image, ImageTk
            self.window_icon = ImageTk.PhotoImage(Image.open(resource_path("assets/whisperlive.png")))
            self.root.iconphoto(True, self.window_icon)
        except Exception:
            # The window icon is cosmetic; missing Pillow or a picky window
            # manager must not stop the app from starting.
            self.window_icon = None
        self.recorder = AudioRecorder()
        self.is_processing = False
        self.is_exiting = False
        self._ui_queue = queue.Queue()
        self.root.after(50, self._drain_ui_queue)
        self.root.report_callback_exception = self._log_callback_exception
        self.hotkey = None
        self.hold_poll_after_id = None
        self.status_text = "Ready  |  Listening for your shortcut"
        self.status_state = "ready"
        self.is_capturing_shortcut = False
        self.capture_modifiers = set()

        self.setup_styles()
        self.setup_ui()
        self.fit_window_to_content()
        self.indicator = RecordingIndicator(self.root)
        self.tray_icon = create_tray_icon(
            self.call_in_ui,
            resource_path("assets/whisperlive.png"),
            self.restore_window,
            self.quit_app,
        )
        self.setup_hotkeys()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Unmap>", self.on_window_unmap)

        if self.config.get("start_minimized"):
            self.root.after(150, self.hide_window)

    def call_in_ui(self, callback):
        """Run a callable on the Tk main thread. Worker/hotkey/tray threads must
        never touch Tk directly (root.after from another thread can deadlock Tcl).
        Strict no-op once the app is exiting (M2 quit-race contract)."""
        if self.is_exiting:
            return
        self._ui_queue.put(callback)

    def _drain_ui_queue(self):
        while True:
            try:
                callback = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception:
                logger.exception("Unhandled error in a scheduled UI callback")
        if not self.is_exiting:
            self.root.after(50, self._drain_ui_queue)

    def _log_callback_exception(self, exc, value, traceback):
        logger.error("Uncaught exception in a Tk callback", exc_info=(exc, value, traceback))

    def save_and_apply(self):
        previous_startup = self.config.get("run_at_startup", False)
        self.clear_hotkeys()
        self.collect_form_values()
        requested_startup = self.config["run_at_startup"]

        # Update startup registration first, then persist the matching
        # configuration.  This keeps the checkbox, config file, and startup
        # entry from claiming different states when either operation fails.
        registry_ok = set_startup_registration(requested_startup)
        if not registry_ok:
            self.config["run_at_startup"] = previous_startup
            self.run_at_startup_var.set(previous_startup)

        try:
            saved = bool(save_config(self.config))
        except Exception:
            saved = False

        rollback_ok = True
        if registry_ok and not saved:
            rollback_ok = set_startup_registration(previous_startup)
            if rollback_ok:
                self.config["run_at_startup"] = previous_startup
                self.run_at_startup_var.set(previous_startup)

        self.setup_hotkeys()
        if saved and registry_ok:
            message = "Changes saved. Your shortcut is ready."
            state = "ready"
        elif saved:
            message = "Changes saved. Startup registration could not be updated; the startup setting was not changed."
            state = "error"
        elif registry_ok and rollback_ok:
            message = "Changes could not be saved. Startup registration was restored."
            state = "error"
        elif registry_ok:
            message = "Changes could not be saved, and startup registration could not be restored."
            state = "error"
        else:
            message = "Changes could not be saved. Startup registration could not be updated."
            state = "error"
        self.set_status(message, state)

    def start_hold_recording(self):
        self.start_recording()
        if self.recorder.is_recording:
            self.poll_hold_shortcut()

    def poll_hold_shortcut(self):
        self.hold_poll_after_id = None
        if self.hotkey and self.hotkey.is_main_key_pressed():
            self.hold_poll_after_id = self.root.after(20, self.poll_hold_shortcut)
        else:
            self.stop_and_process()

    def toggle_recording(self):
        if self.is_processing:
            return
        if self.recorder.is_recording:
            self.stop_and_process()
        else:
            self.start_recording()

    def beep(self, frequency, duration_ms):
        if winsound is not None:
            winsound.Beep(frequency, duration_ms)
        else:
            # The Tk bell is the closest portable equivalent on Linux and,
            # unlike winsound.Beep, must be scheduled on the UI thread.
            self.call_in_ui(self.root.bell)

    def start_recording(self):
        if self.recorder.is_recording or self.is_processing:
            return
        # D2 (Option A): the API key is optional - local providers work keyless.
        # Two-stage presets (MiMo) stage a separate stage-1 model, so the guard
        # checks the EFFECTIVE models: stage 1 = transcription_model or model
        # (fail-closed on both), stage 2 = model.  The message stays verbatim.
        effective_transcription = (
            self.config.get("transcription_model", "").strip() or self.config.get("model", "").strip()
        )
        effective_post = self.config.get("model", "").strip()
        if not (self.config.get("provider_base_url", "").strip() and effective_transcription and effective_post):
            self.set_status("Open Settings and set the provider URL and model before recording.", "error")
            self.beep(400, 300)
            return
        self.beep(1000, 100)
        self.recorder.start_recording()
        self.set_status("Recording in progress. Press the shortcut to stop.", "recording")
        self.indicator.show("Recording", "Listening for your voice", "recording")

    def stop_and_process(self):
        if not self.recorder.is_recording:
            return
        self.beep(800, 100)
        try:
            audio_file = self.recorder.stop_recording()
        except RecorderError as error:
            self.set_status(str(error), "error")
            self.beep(400, 300)
            return
        self.is_processing = True  # UI thread - the only writer besides _on_processing_finished
        self.set_status("Processing your recording...", "processing")
        self.indicator.show("Processing", "Turning speech into text", "processing")
        # Snapshot config ON THE UI THREAD; the worker never reads the live dict.
        language = self.config.get("target_language", "English")
        api_key = self.config.get("api_key", "").strip()
        base_url = self.config.get("provider_base_url", "").strip()
        model = self.config.get("model", "").strip()
        # Stage-1 model + wire dialect (MiMo split presets); derived here so the
        # worker thread never touches the live config dict.
        transcription_model = self.config.get("transcription_model", "").strip()
        dialect = providers.dialect_for_preset(self.config.get("provider", "custom"))
        threading.Thread(
            target=self.process_audio_thread,
            args=(audio_file, language, api_key, base_url, model),
            kwargs={"dialect": dialect, "transcription_model": transcription_model},
            daemon=True,
        ).start()

    def process_audio_thread(self, audio_file, language, api_key, base_url, model, *,
                             dialect=DIALECT_OPENAI, transcription_model=""):
        # worker thread: no unguarded Tk access, no live config reads, no is_processing writes
        try:
            # Forward only stage options that differ from process_audio's
            # defaults: a plain single-stage recording keeps the exact
            # 5-argument call shape, while MiMo's dialect and stage-1 model
            # travel as keyword arguments (behaviourally identical either way).
            stage_options = {}
            if dialect != DIALECT_OPENAI:
                stage_options["dialect"] = dialect
            if transcription_model:
                stage_options["transcription_model"] = transcription_model
            text = process_audio(audio_file, language, api_key, base_url, model, **stage_options)
            if text:
                if self.is_exiting:  # M2: quit raced the request - no post-shutdown paste/beep
                    return
                paste_text(text)
                self.beep(1200, 200)
            self.call_in_ui(lambda: self.set_status("Done. Ready for the next recording.", "ready"))
        except (LLMError, PasteError, ValueError) as error:
            message = f"Could not process recording: {error}"  # upstream copy; all three are user-safe
            self.call_in_ui(lambda: self.set_status(message, "error"))
        except Exception:
            logger.exception("Unexpected error while processing the recording")
            message = "Something went wrong while processing the recording."
            self.call_in_ui(lambda: self.set_status(message, "error"))
        finally:
            # Single-owner rule: the caller deletes the temp file it received.
            if audio_file:
                try:
                    os.remove(audio_file)
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.warning("Could not delete temp file %s", os.path.basename(audio_file))
            # D7: completion is ALWAYS posted - no path may leave is_processing stuck.
            self.call_in_ui(self._on_processing_finished)

    def _on_processing_finished(self):
        # UI THREAD (queued) - the single writer of is_processing after startup.
        self.is_processing = False
        self.indicator.hide()

    def quit_app(self):
        self.is_exiting = True
        self.clear_hotkeys()
        self.indicator.hide()
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.destroy()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format=app_logging.LOG_FORMAT)
    app_logging.setup_logging()
    threading.excepthook = app_logging.log_thread_exception
    if tk is None:
        raise SystemExit("WhisperLive requires tkinter; install the python3-tk package.")
    if os.name == "nt":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        # Notify Explorer that the executable's multi-size icon has changed, including List view's 16 px cache.
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
    instance_lock = _acquire_single_instance_lock_or_exit()
    root = tk.Tk()
    app = WhisperLiveApp(root)
    root.mainloop()
    instance_lock.release()
