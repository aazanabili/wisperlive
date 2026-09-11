import ctypes
import math
import os
import sys
import threading
import tkinter as tk
from tkinter import ttk
import webbrowser
import winreg

import keyboard
import pystray
import winsound
from PIL import Image, ImageTk

from audio_recorder import AudioRecorder
from auto_typer import paste_text
from config_manager import load_config, save_config
from gemini_api import process_audio
from updater import RELEASES_PAGE, UpdateError, get_latest_release, is_newer_version
from version import APP_VERSION


THEMES = {
    "dark": {
        "background": "#10131A", "surface": "#181D27", "surface_hover": "#202735",
        "border": "#2C3444", "text": "#F4F7FB", "muted": "#A5B0C2",
        "accent": "#7C8CFF", "accent_hover": "#95A2FF", "success": "#49D39A",
        "warning": "#F5C761", "danger": "#FF667D", "success_bg": "#15251F",
        "warning_bg": "#322A17", "danger_bg": "#311B25", "indicator": "#0D1728",
        "indicator_border": "#27466F", "indicator_glow": "#1F4D84", "indicator_accent": "#76ABFF",
        "indicator_text": "#EDF4FF",
    },
    "light": {
        "background": "#F4F6FA", "surface": "#FFFFFF", "surface_hover": "#EDF1F7",
        "border": "#CED6E3", "text": "#172033", "muted": "#58657A",
        "accent": "#4B5FD5", "accent_hover": "#3D4FB6", "success": "#087A52",
        "warning": "#9C6500", "danger": "#C9364E", "success_bg": "#E4F5EC",
        "warning_bg": "#FFF4D8", "danger_bg": "#FDE9ED", "indicator": "#F8FBFF",
        "indicator_border": "#BDD2F2", "indicator_glow": "#DCEAFF", "indicator_accent": "#397BE0",
        "indicator_text": "#172F54",
    },
}
COLORS = THEMES["dark"].copy()

STARTUP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "WhisperLive"
APP_ID = "AbdullatifZanabili.WhisperLive"


def resource_path(relative_path):
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


class WhisperLiveApp:
    def __init__(self, root):
        self.root = root
        self.root.title("WhisperLive")
        self.root.geometry("700x720")
        self.root.minsize(620, 620)

        self.config = load_config()
        COLORS.clear()
        COLORS.update(THEMES.get(self.config.get("theme"), THEMES["dark"]))
        self.root.configure(bg=COLORS["background"])
        self.root.iconbitmap(resource_path("assets/whisperlive.ico"))
        self.window_icon = ImageTk.PhotoImage(Image.open(resource_path("assets/whisperlive.png")))
        self.root.iconphoto(True, self.window_icon)
        self.recorder = AudioRecorder()
        self.is_processing = False
        self.is_exiting = False
        self.hotkey_handles = []
        self.indicator_after_id = None
        self.indicator_phase = 0
        self.indicator_state = None
        self.status_text = "Ready  |  Listening for your shortcut"
        self.status_state = "ready"
        self.is_capturing_shortcut = False
        self.capture_modifiers = set()
        self.reduce_motion = self.prefers_reduced_motion()

        self.setup_styles()
        self.setup_ui()
        self.fit_window_to_content()
        self.create_recording_indicator()
        self.create_tray_icon()
        self.setup_hotkeys()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Unmap>", self.on_window_unmap)

        if self.config.get("start_minimized"):
            self.root.after(150, self.hide_window)

    def setup_styles(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Dark.TCombobox",
            fieldbackground=COLORS["surface_hover"],
            background=COLORS["surface_hover"],
            foreground=COLORS["text"],
            arrowcolor=COLORS["muted"],
            bordercolor=COLORS["border"],
            lightcolor=COLORS["border"],
            darkcolor=COLORS["border"],
            padding=8,
        )
        style.map("Dark.TCombobox", fieldbackground=[("readonly", COLORS["surface_hover"])])

    def setup_ui(self):
        self.footer = tk.Frame(self.root, bg=COLORS["background"], padx=28, pady=14)
        self.footer.pack(side=tk.BOTTOM, fill=tk.X)
        container = tk.Frame(self.root, bg=COLORS["background"], padx=28, pady=16)
        container.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.content = container

        header = tk.Frame(container, bg=COLORS["background"])
        header.pack(fill=tk.X, pady=(0, 12))
        title_group = tk.Frame(header, bg=COLORS["background"])
        title_group.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Label(
            title_group, text="WhisperLive", bg=COLORS["background"], fg=COLORS["text"],
            font=("Segoe UI Semibold", 22),
        ).pack(anchor=tk.W)
        tk.Label(
            title_group, text="Voice capture that stays out of your way.",
            bg=COLORS["background"], fg=COLORS["muted"], font=("Segoe UI", 10),
        ).pack(anchor=tk.W, pady=(2, 0))
        self.create_button(header, "About", self.show_about, secondary=True).pack(side=tk.RIGHT, padx=(8, 0))
        theme_label = "Light mode" if self.config.get("theme") == "dark" else "Dark mode"
        self.create_button(header, theme_label, self.toggle_theme, secondary=True).pack(side=tk.RIGHT)

        self.status_label = tk.Label(
            container, text=self.status_text, anchor=tk.W,
            bg=COLORS["success_bg"], fg=COLORS["success"], padx=12, pady=9,
            font=("Segoe UI Semibold", 10), justify=tk.LEFT, wraplength=620,
        )
        self.status_label.pack(fill=tk.X, pady=(0, 12))

        settings = tk.Frame(container, bg=COLORS["surface"], highlightbackground=COLORS["border"],
                            highlightthickness=1, padx=18, pady=12)
        settings.pack(fill=tk.X)

        self.api_key_var = tk.StringVar(value=self.config.get("api_key", ""))
        self.add_label(settings, "Gemini API key")
        api_frame = tk.Frame(settings, bg=COLORS["surface"])
        api_frame.pack(fill=tk.X, pady=(0, 5))
        self.api_entry = self.create_entry(api_frame, self.api_key_var, show="*")
        self.api_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.show_btn = self.create_button(api_frame, "Show", self.toggle_api_visibility, secondary=True)
        self.show_btn.pack(side=tk.LEFT, padx=(8, 0))
        tk.Label(
            settings, text="Get a key from aistudio.google.com/app/apikey",
            bg=COLORS["surface"], fg=COLORS["muted"], font=("Segoe UI", 9),
        ).pack(anchor=tk.W, pady=(0, 10))

        self.lang_var = tk.StringVar(value=self.config.get("target_language", "English"))
        self.add_label(settings, "Output language")
        lang_combo = ttk.Combobox(
            settings, textvariable=self.lang_var,
            values=["English", "العربية", "Français", "Español", "Deutsch", "中文", "日本語"],
            state="readonly", style="Dark.TCombobox",
        )
        lang_combo.pack(fill=tk.X, pady=(0, 10))

        self.shortcut_var = tk.StringVar(value=self.config.get("shortcut", "ctrl+space"))
        self.add_label(settings, "Global shortcut")
        shortcut_frame = tk.Frame(settings, bg=COLORS["surface"])
        shortcut_frame.pack(fill=tk.X, pady=(0, 10))
        self.shortcut_entry = self.create_entry(shortcut_frame, self.shortcut_var)
        self.shortcut_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.shortcut_entry.bind("<FocusIn>", self.begin_shortcut_capture)
        self.shortcut_entry.bind("<FocusOut>", self.cancel_shortcut_capture)
        self.shortcut_entry.bind("<KeyPress>", self.capture_shortcut_key)
        self.create_button(shortcut_frame, "Record", self.focus_shortcut_capture, secondary=True).pack(side=tk.LEFT, padx=(8, 0))

        self.mode_var = tk.StringVar(value=self.config.get("mode", "toggle"))
        self.add_label(settings, "Recording behavior")
        mode_row = tk.Frame(settings, bg=COLORS["surface"])
        mode_row.pack(fill=tk.X)
        self.create_radio(mode_row, "Toggle", "toggle").pack(side=tk.LEFT)
        self.create_radio(mode_row, "Hold to record", "hold").pack(side=tk.LEFT, padx=(18, 0))

        preferences = tk.Frame(container, bg=COLORS["surface"], highlightbackground=COLORS["border"],
                               highlightthickness=1, padx=18, pady=12)
        preferences.pack(fill=tk.X, pady=(12, 0))
        tk.Label(
            preferences, text="Background behavior", bg=COLORS["surface"], fg=COLORS["text"],
            font=("Segoe UI Semibold", 11),
        ).pack(anchor=tk.W, pady=(0, 7))
        self.minimize_to_tray_var = tk.BooleanVar(value=self.config.get("minimize_to_tray", True))
        self.start_minimized_var = tk.BooleanVar(value=self.config.get("start_minimized", False))
        self.run_at_startup_var = tk.BooleanVar(value=self.config.get("run_at_startup", False))
        preference_controls = tk.Frame(preferences, bg=COLORS["surface"])
        preference_controls.pack(fill=tk.X)
        self.create_checkbutton(preference_controls, "Minimize to tray", self.minimize_to_tray_var).pack(side=tk.LEFT)
        self.create_checkbutton(preference_controls, "Start minimized", self.start_minimized_var).pack(side=tk.LEFT, padx=(16, 0))
        self.create_checkbutton(preference_controls, "Run at sign-in", self.run_at_startup_var).pack(side=tk.LEFT, padx=(16, 0))

        self.update_button = self.create_button(self.footer, "Check for updates", self.start_update, secondary=True)
        self.update_button.pack(side=tk.LEFT)
        self.create_button(self.footer, "Open GitHub releases", self.open_releases, secondary=True).pack(side=tk.LEFT, padx=(8, 0))
        self.create_button(self.footer, "Save changes", self.save_and_apply).pack(side=tk.RIGHT)
        self.set_status(self.status_text, self.status_state)

    def fit_window_to_content(self):
        self.root.update_idletasks()
        minimum_height = self.root.winfo_reqheight()
        self.root.minsize(620, minimum_height)
        if self.root.winfo_height() < minimum_height:
            self.root.geometry(f"700x{minimum_height}")

    def add_label(self, parent, text):
        tk.Label(
            parent, text=text, bg=COLORS["surface"], fg=COLORS["text"],
            font=("Segoe UI Semibold", 10),
        ).pack(anchor=tk.W, pady=(0, 6))

    def create_entry(self, parent, variable, show=None):
        return tk.Entry(
            parent, textvariable=variable, show=show, bg=COLORS["surface_hover"], fg=COLORS["text"],
            insertbackground=COLORS["text"], relief=tk.FLAT, highlightthickness=1,
            highlightbackground=COLORS["border"], highlightcolor=COLORS["accent"],
            font=("Segoe UI", 10),
        )

    def create_button(self, parent, text, command, secondary=False):
        background = COLORS["surface_hover"] if secondary else COLORS["accent"]
        foreground = COLORS["text"] if secondary else COLORS["background"]
        hover = COLORS["border"] if secondary else COLORS["accent_hover"]
        button = tk.Button(
            parent, text=text, command=command, bg=background, fg=foreground,
            activebackground=hover, activeforeground=foreground, relief=tk.FLAT,
            padx=15, pady=8, cursor="hand2", font=("Segoe UI Semibold", 10),
        )
        button.bind("<Enter>", lambda _event: button.config(bg=hover))
        button.bind("<Leave>", lambda _event: button.config(bg=background))
        return button

    def create_radio(self, parent, text, value):
        return tk.Radiobutton(
            parent, text=text, value=value, variable=self.mode_var, bg=COLORS["surface"],
            fg=COLORS["muted"], selectcolor=COLORS["surface_hover"], activebackground=COLORS["surface"],
            activeforeground=COLORS["text"], font=("Segoe UI", 10),
        )

    def create_checkbutton(self, parent, text, variable):
        return tk.Checkbutton(
            parent, text=text, variable=variable, bg=COLORS["surface"], fg=COLORS["muted"],
            selectcolor=COLORS["surface_hover"], activebackground=COLORS["surface"],
            activeforeground=COLORS["text"], font=("Segoe UI", 10),
        )

    def toggle_api_visibility(self):
        if self.api_entry.cget("show") == "*":
            self.api_entry.config(show="")
            self.show_btn.config(text="Hide")
        else:
            self.api_entry.config(show="*")
            self.show_btn.config(text="Show")

    def collect_form_values(self):
        self.config.update({
            "api_key": self.api_key_var.get().strip(),
            "target_language": self.lang_var.get(),
            "shortcut": self.shortcut_var.get().strip(),
            "mode": self.mode_var.get(),
            "minimize_to_tray": self.minimize_to_tray_var.get(),
            "start_minimized": self.start_minimized_var.get(),
            "run_at_startup": self.run_at_startup_var.get(),
        })

    def toggle_theme(self):
        self.collect_form_values()
        theme = "light" if self.config.get("theme") == "dark" else "dark"
        self.config["theme"] = theme
        COLORS.clear()
        COLORS.update(THEMES[theme])
        save_config(self.config)
        self.root.configure(bg=COLORS["background"])
        self.content.destroy()
        self.footer.destroy()
        self.setup_styles()
        self.setup_ui()
        self.fit_window_to_content()
        self.refresh_indicator_theme()

    def show_about(self):
        about = tk.Toplevel(self.root)
        about.title("About WhisperLive")
        about.transient(self.root)
        about.resizable(False, False)
        about.configure(bg=COLORS["surface"])
        panel = tk.Frame(about, bg=COLORS["surface"], padx=28, pady=24)
        panel.pack(fill=tk.BOTH, expand=True)
        tk.Label(panel, text="WhisperLive", bg=COLORS["surface"], fg=COLORS["text"], font=("Segoe UI Semibold", 16)).pack(anchor=tk.W)
        tk.Label(
            panel, text=f"Current version: {APP_VERSION}", bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Segoe UI", 10),
        ).pack(anchor=tk.W, pady=(4, 0))
        tk.Label(
            panel, text="Informatics Engineer Abdullatif Zanabili", bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Segoe UI", 10), wraplength=300, justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(8, 16))
        self.create_link(panel, "Personal website", "https://zanabili.dev/").pack(anchor=tk.W, pady=2)
        self.create_link(panel, "Support WhisperLive", "https://paypal.me/Zanabili").pack(anchor=tk.W, pady=2)
        self.create_link(panel, "Project on GitHub", "https://github.com/aazanabili/wisperlive").pack(anchor=tk.W, pady=2)
        self.create_link(panel, "info@zanabili.dev", "mailto:info@zanabili.dev").pack(anchor=tk.W, pady=2)
        self.create_button(panel, "Close", about.destroy, secondary=True).pack(anchor=tk.E, pady=(18, 0))

    def create_link(self, parent, text, url):
        link = tk.Label(
            parent, text=text, bg=COLORS["surface"], fg=COLORS["accent"], cursor="hand2",
            font=("Segoe UI Semibold", 10),
        )
        link.bind("<Button-1>", lambda _event: webbrowser.open(url))
        link.bind("<Enter>", lambda _event: link.config(fg=COLORS["accent_hover"]))
        link.bind("<Leave>", lambda _event: link.config(fg=COLORS["accent"]))
        return link

    def focus_shortcut_capture(self):
        self.shortcut_entry.focus_set()

    def begin_shortcut_capture(self, _event=None):
        if self.is_capturing_shortcut:
            return
        self.is_capturing_shortcut = True
        self.capture_modifiers.clear()
        self.clear_hotkeys()
        self.shortcut_var.set("Press the shortcut now")
        self.shortcut_entry.selection_range(0, tk.END)

    def cancel_shortcut_capture(self, _event=None):
        if not self.is_capturing_shortcut:
            return
        self.is_capturing_shortcut = False
        self.capture_modifiers.clear()
        if self.shortcut_var.get() == "Press the shortcut now":
            self.shortcut_var.set(self.config.get("shortcut", "ctrl+space"))
        self.setup_hotkeys()

    def capture_shortcut_key(self, event):
        if not self.is_capturing_shortcut:
            return None
        modifier_names = {
            "Control_L": "ctrl", "Control_R": "ctrl", "Shift_L": "shift", "Shift_R": "shift",
            "Alt_L": "alt", "Alt_R": "alt", "Win_L": "windows", "Win_R": "windows",
        }
        modifier = modifier_names.get(event.keysym)
        if modifier:
            self.capture_modifiers.add(modifier)
            return "break"
        if event.keysym == "Escape":
            self.shortcut_var.set(self.config.get("shortcut", "ctrl+space"))
            self.cancel_shortcut_capture()
            return "break"
        key = event.keysym.lower()
        key_names = {"prior": "page up", "next": "page down", "return": "enter", "escape": "esc"}
        key = key_names.get(key, key)
        modifiers = [name for name in ("ctrl", "alt", "shift", "windows") if name in self.capture_modifiers]
        shortcut = "+".join([*modifiers, key])
        self.shortcut_var.set(shortcut)
        self.is_capturing_shortcut = False
        self.capture_modifiers.clear()
        self.shortcut_entry.selection_clear()
        self.shortcut_entry.focus_set()
        self.setup_hotkeys()
        self.set_status("Shortcut recorded. Save changes to apply it.", "ready")
        return "break"

    def save_and_apply(self):
        self.clear_hotkeys()
        self.collect_form_values()
        if not self.set_startup_registration(self.config["run_at_startup"]):
            self.config["run_at_startup"] = False
            self.run_at_startup_var.set(False)
            startup_message = " Changes saved, but Windows startup could not be updated."
        else:
            startup_message = ""
        save_config(self.config)
        self.setup_hotkeys()
        self.set_status(f"Changes saved. Your shortcut is ready.{startup_message}", "ready")

    def set_startup_registration(self, enabled):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY, 0, winreg.KEY_SET_VALUE) as key:
                if enabled:
                    if getattr(sys, "frozen", False):
                        command = f'"{sys.executable}"'
                    else:
                        command = f'"{sys.executable}" "{os.path.abspath(__file__)}"'
                    winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
                else:
                    try:
                        winreg.DeleteValue(key, APP_NAME)
                    except FileNotFoundError:
                        pass
            return True
        except OSError:
            return False

    def setup_hotkeys(self):
        shortcut = self.config.get("shortcut", "ctrl+space")
        try:
            if self.config.get("mode") == "toggle":
                handle = keyboard.add_hotkey(shortcut, lambda: self.root.after(0, self.toggle_recording), suppress=True)
                self.hotkey_handles.append(("hotkey", handle))
            else:
                main_key = shortcut.split("+")[-1].strip()
                press_handle = keyboard.on_press_key(main_key, self.on_key_press, suppress=False)
                release_handle = keyboard.on_release_key(main_key, self.on_key_release, suppress=False)
                self.hotkey_handles.extend([("hook", press_handle), ("hook", release_handle)])
        except Exception as error:
            self.set_status(f"Shortcut error: {error}", "error")

    def clear_hotkeys(self):
        for kind, handle in self.hotkey_handles:
            if kind == "hotkey":
                keyboard.remove_hotkey(handle)
            else:
                keyboard.unhook(handle)
        self.hotkey_handles.clear()

    def on_key_press(self, _event):
        parts = [part.strip().lower() for part in self.config.get("shortcut", "").split("+")]
        modifiers = [part for part in parts if part in ["ctrl", "shift", "alt", "windows"]]
        if all(keyboard.is_pressed(modifier) for modifier in modifiers):
            self.root.after(0, self.start_recording)

    def on_key_release(self, _event):
        self.root.after(0, self.stop_and_process)

    def toggle_recording(self):
        if self.is_processing:
            return
        if self.recorder.is_recording:
            self.stop_and_process()
        else:
            self.start_recording()

    def start_recording(self):
        if self.recorder.is_recording or self.is_processing:
            return
        if not self.config.get("api_key", "").strip():
            self.set_status("An API key is required before recording.", "error")
            winsound.Beep(400, 300)
            return
        winsound.Beep(1000, 100)
        self.recorder.start_recording()
        self.set_status("Recording in progress. Press the shortcut to stop.", "recording")
        self.show_indicator("Recording", "Listening for your voice", "recording")

    def stop_and_process(self):
        if not self.recorder.is_recording:
            return
        winsound.Beep(800, 100)
        audio_file = self.recorder.stop_recording()
        self.is_processing = True
        self.set_status("Processing your recording...", "processing")
        self.show_indicator("Processing", "Turning speech into text", "processing")
        threading.Thread(target=self.process_audio_thread, args=(audio_file,), daemon=True).start()

    def process_audio_thread(self, audio_file):
        try:
            text = process_audio(audio_file, self.config.get("target_language", "English"), self.config.get("api_key", "").strip())
            if text:
                paste_text(text)
                winsound.Beep(1200, 200)
            self.root.after(0, lambda: self.set_status("Done. Ready for the next recording.", "ready"))
        except Exception as error:
            message = f"Could not process recording: {error}"
            self.root.after(0, lambda: self.set_status(message, "error"))
        finally:
            self.is_processing = False
            self.root.after(0, self.hide_indicator)

    def set_status(self, text, state):
        palettes = {
            "ready": (COLORS["success_bg"], COLORS["success"]),
            "recording": (COLORS["danger_bg"], COLORS["danger"]),
            "processing": (COLORS["warning_bg"], COLORS["warning"]),
            "error": (COLORS["danger_bg"], COLORS["danger"]),
        }
        self.status_text = text
        self.status_state = state
        background, foreground = palettes[state]
        self.status_label.config(text=text, bg=background, fg=foreground)
        self.root.after_idle(self.fit_window_to_content)

    def start_update(self):
        self.update_button.config(state=tk.DISABLED, text="Checking...")
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.set_status("Checking GitHub for an update...", "processing")
        threading.Thread(target=self.check_update_thread, daemon=True).start()

    def check_update_thread(self):
        try:
            release = get_latest_release()
            tag = release.get("tag_name", "")
            if is_newer_version(tag, APP_VERSION):
                self.root.after(0, lambda: self.update_available(tag))
            else:
                self.root.after(0, lambda: self.update_complete("You already have the latest release."))
        except UpdateError as error:
            message = str(error)
            self.root.after(0, lambda: self.update_manual(message))

    def update_available(self, tag):
        self.update_button.config(state=tk.NORMAL, text="Check for updates")
        self.set_status(
            f"Your current version is {APP_VERSION}. Version {tag} is available in the repository. Open GitHub Releases to download it.",
            "processing",
        )

    def update_manual(self, reason):
        self.update_button.config(state=tk.NORMAL, text="Check for updates")
        self.set_status(
            f"Current version: {APP_VERSION}. {reason} Open GitHub Releases to view and download the latest version.",
            "error",
        )

    @staticmethod
    def open_releases():
        webbrowser.open(RELEASES_PAGE)

    def update_complete(self, message):
        self.update_button.config(state=tk.NORMAL, text="Check for updates")
        self.set_status(message, "ready")

    @staticmethod
    def prefers_reduced_motion():
        animations_enabled = ctypes.c_int()
        try:
            # SPI_GETCLIENTAREAANIMATION reflects the Windows animation accessibility setting.
            result = ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(animations_enabled), 0)
            return bool(result) and not bool(animations_enabled.value)
        except OSError:
            return False

    def create_recording_indicator(self):
        self.indicator = tk.Toplevel(self.root)
        self.indicator.withdraw()
        self.indicator.overrideredirect(True)
        self.indicator.attributes("-topmost", True)
        self.indicator.configure(bg=COLORS["indicator_border"])
        panel = tk.Frame(self.indicator, bg=COLORS["indicator"], padx=18, pady=10)
        panel.pack(padx=1, pady=1)
        self.indicator_panel = panel
        self.indicator_canvas = tk.Canvas(panel, width=46, height=46, bg=COLORS["indicator"], highlightthickness=0)
        self.indicator_canvas.pack(side=tk.LEFT, padx=(0, 11))
        labels = tk.Frame(panel, bg=COLORS["indicator"])
        labels.pack(side=tk.LEFT)
        self.indicator_labels = labels
        self.indicator_title = tk.Label(labels, text="Recording", bg=COLORS["indicator"], fg=COLORS["indicator_text"], font=("Segoe UI Semibold", 10))
        self.indicator_title.pack(anchor=tk.W)
        self.indicator_detail = tk.Label(labels, text="Listening for your voice", bg=COLORS["indicator"], fg=COLORS["muted"], font=("Segoe UI", 9))
        self.indicator_detail.pack(anchor=tk.W)

    def refresh_indicator_theme(self):
        if self.indicator_after_id:
            self.root.after_cancel(self.indicator_after_id)
            self.indicator_after_id = None
        self.indicator.configure(bg=COLORS["indicator_border"])
        self.indicator_panel.configure(bg=COLORS["indicator"])
        self.indicator_labels.configure(bg=COLORS["indicator"])
        self.indicator_canvas.configure(bg=COLORS["indicator"])
        title_color = COLORS["warning"] if self.indicator_state == "processing" else COLORS["indicator_text"]
        self.indicator_title.configure(bg=COLORS["indicator"], fg=title_color)
        self.indicator_detail.configure(bg=COLORS["indicator"], fg=COLORS["muted"])
        if self.indicator_state:
            self.draw_indicator()

    def show_indicator(self, title, detail, state):
        if self.indicator_after_id:
            self.root.after_cancel(self.indicator_after_id)
            self.indicator_after_id = None
        title_color = COLORS["warning"] if state == "processing" else COLORS["indicator_text"]
        self.indicator_title.config(text=title, fg=title_color)
        self.indicator_detail.config(text=detail)
        self.position_indicator()
        self.indicator.deiconify()
        self.indicator.lift()
        self.indicator_state = state
        self.draw_indicator()

    def position_indicator(self):
        self.indicator.update_idletasks()
        width = self.indicator.winfo_width()
        x = (self.root.winfo_screenwidth() - width) // 2
        y = self.root.winfo_screenheight() - self.indicator.winfo_height() - 112
        self.indicator.geometry(f"+{x}+{y}")

    def draw_indicator(self):
        self.indicator_after_id = None
        self.indicator_canvas.delete("all")
        pulse = (math.sin(self.indicator_phase) + 1) / 2
        is_processing = self.indicator_state == "processing"
        accent = COLORS["warning"] if is_processing else COLORS["indicator_accent"]
        glow = COLORS["warning"] if is_processing else COLORS["indicator_glow"]
        outer_radius = 18 + int(pulse * 3)
        center = 23
        self.indicator_canvas.create_oval(
            center - outer_radius, center - outer_radius, center + outer_radius, center + outer_radius,
            outline=glow, width=2,
        )
        self.indicator_canvas.create_arc(
            7, 7, 39, 39, start=int(self.indicator_phase * 38), extent=120,
            outline=accent, width=2,
        )
        core_radius = 8 + int(pulse * 2)
        self.indicator_canvas.create_oval(
            center - core_radius, center - core_radius, center + core_radius, center + core_radius,
            fill=accent, outline="",
        )
        self.indicator_canvas.create_oval(21, 21, 25, 25, fill=COLORS["indicator"], outline="")
        if not self.reduce_motion:
            self.indicator_phase += 0.14
            self.indicator_after_id = self.root.after(40, self.draw_indicator)

    def hide_indicator(self):
        if self.indicator_after_id:
            self.root.after_cancel(self.indicator_after_id)
            self.indicator_after_id = None
        self.indicator.withdraw()

    def create_tray_icon(self):
        image = Image.open(resource_path("assets/whisperlive.png")).convert("RGBA")
        self.tray_icon = pystray.Icon(
            APP_NAME, image, APP_NAME,
            menu=pystray.Menu(
                pystray.MenuItem("Show WhisperLive", self.on_tray_show, default=True),
                pystray.MenuItem("Quit", self.on_tray_quit),
            ),
        )
        self.tray_icon.run_detached()

    def on_tray_show(self, _icon, _item):
        self.root.after(0, self.restore_window)

    def on_tray_quit(self, _icon, _item):
        self.root.after(0, self.quit_app)

    def on_window_unmap(self, _event):
        if self.config.get("minimize_to_tray"):
            self.root.after_idle(self.hide_if_minimized)

    def hide_if_minimized(self):
        if self.root.state() == "iconic":
            self.hide_window()

    def hide_window(self):
        self.root.withdraw()

    def restore_window(self):
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()

    def on_close(self):
        if self.config.get("minimize_to_tray"):
            self.hide_window()
        else:
            self.quit_app()

    def quit_app(self):
        self.is_exiting = True
        self.clear_hotkeys()
        self.hide_indicator()
        self.tray_icon.stop()
        self.root.destroy()


if __name__ == "__main__":
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    # Notify Explorer that the executable's multi-size icon has changed, including List view's 16 px cache.
    ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
    root = tk.Tk()
    app = WhisperLiveApp(root)
    root.mainloop()
