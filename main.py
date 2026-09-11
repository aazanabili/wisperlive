import ctypes
import os
import sys
import threading
import tkinter as tk
from tkinter import ttk
import winreg

import keyboard
import pystray
import winsound
from PIL import Image, ImageDraw

from audio_recorder import AudioRecorder
from auto_typer import paste_text
from config_manager import load_config, save_config
from gemini_api import process_audio


COLORS = {
    "background": "#10131A",
    "surface": "#181D27",
    "surface_hover": "#202735",
    "border": "#2C3444",
    "text": "#F4F7FB",
    "muted": "#A5B0C2",
    "accent": "#7C8CFF",
    "accent_hover": "#95A2FF",
    "success": "#49D39A",
    "warning": "#F5C761",
    "danger": "#FF667D",
}

STARTUP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "WhisperLive"


class WhisperLiveApp:
    def __init__(self, root):
        self.root = root
        self.root.title("WhisperLive")
        self.root.geometry("560x610")
        self.root.minsize(520, 570)
        self.root.configure(bg=COLORS["background"])

        self.config = load_config()
        self.recorder = AudioRecorder()
        self.is_processing = False
        self.is_exiting = False
        self.hotkey_handles = []
        self.indicator_after_id = None
        self.indicator_phase = 0
        self.reduce_motion = self.prefers_reduced_motion()

        self.setup_styles()
        self.setup_ui()
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
        container = tk.Frame(self.root, bg=COLORS["background"], padx=28, pady=24)
        container.pack(fill=tk.BOTH, expand=True)

        header = tk.Frame(container, bg=COLORS["background"])
        header.pack(fill=tk.X, pady=(0, 22))
        tk.Label(
            header, text="WhisperLive", bg=COLORS["background"], fg=COLORS["text"],
            font=("Segoe UI Semibold", 22),
        ).pack(anchor=tk.W)
        tk.Label(
            header, text="Voice capture that stays out of your way.",
            bg=COLORS["background"], fg=COLORS["muted"], font=("Segoe UI", 10),
        ).pack(anchor=tk.W, pady=(2, 0))

        self.status_label = tk.Label(
            container, text="Ready  |  Listening for your shortcut", anchor=tk.W,
            bg="#15251F", fg=COLORS["success"], padx=12, pady=9,
            font=("Segoe UI Semibold", 10),
        )
        self.status_label.pack(fill=tk.X, pady=(0, 18))

        settings = tk.Frame(container, bg=COLORS["surface"], highlightbackground=COLORS["border"],
                            highlightthickness=1, padx=18, pady=16)
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
        ).pack(anchor=tk.W, pady=(0, 15))

        self.lang_var = tk.StringVar(value=self.config.get("target_language", "English"))
        self.add_label(settings, "Output language")
        lang_combo = ttk.Combobox(
            settings, textvariable=self.lang_var,
            values=["English", "العربية", "Français", "Español", "Deutsch", "中文", "日本語"],
            state="readonly", style="Dark.TCombobox",
        )
        lang_combo.pack(fill=tk.X, pady=(0, 15))

        self.shortcut_var = tk.StringVar(value=self.config.get("shortcut", "ctrl+space"))
        self.add_label(settings, "Global shortcut")
        self.create_entry(settings, self.shortcut_var).pack(fill=tk.X, pady=(0, 15))

        self.mode_var = tk.StringVar(value=self.config.get("mode", "toggle"))
        self.add_label(settings, "Recording behavior")
        mode_row = tk.Frame(settings, bg=COLORS["surface"])
        mode_row.pack(fill=tk.X)
        self.create_radio(mode_row, "Toggle", "toggle").pack(side=tk.LEFT)
        self.create_radio(mode_row, "Hold to record", "hold").pack(side=tk.LEFT, padx=(18, 0))

        preferences = tk.Frame(container, bg=COLORS["surface"], highlightbackground=COLORS["border"],
                               highlightthickness=1, padx=18, pady=16)
        preferences.pack(fill=tk.X, pady=(14, 0))
        tk.Label(
            preferences, text="Background behavior", bg=COLORS["surface"], fg=COLORS["text"],
            font=("Segoe UI Semibold", 11),
        ).pack(anchor=tk.W, pady=(0, 7))
        self.minimize_to_tray_var = tk.BooleanVar(value=self.config.get("minimize_to_tray", True))
        self.start_minimized_var = tk.BooleanVar(value=self.config.get("start_minimized", False))
        self.run_at_startup_var = tk.BooleanVar(value=self.config.get("run_at_startup", False))
        self.create_checkbutton(preferences, "Minimize to the system tray", self.minimize_to_tray_var).pack(anchor=tk.W, pady=3)
        self.create_checkbutton(preferences, "Start minimized", self.start_minimized_var).pack(anchor=tk.W, pady=3)
        self.create_checkbutton(preferences, "Run automatically when I sign in", self.run_at_startup_var).pack(anchor=tk.W, pady=3)

        actions = tk.Frame(container, bg=COLORS["background"])
        actions.pack(fill=tk.X, pady=(20, 0))
        self.create_button(actions, "Save changes", self.save_and_apply).pack(side=tk.RIGHT)

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

    def save_and_apply(self):
        self.clear_hotkeys()
        self.config.update({
            "api_key": self.api_key_var.get().strip(),
            "target_language": self.lang_var.get(),
            "shortcut": self.shortcut_var.get().strip(),
            "mode": self.mode_var.get(),
            "minimize_to_tray": self.minimize_to_tray_var.get(),
            "start_minimized": self.start_minimized_var.get(),
            "run_at_startup": self.run_at_startup_var.get(),
        })
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
            "ready": ("#15251F", COLORS["success"]),
            "recording": ("#311B25", COLORS["danger"]),
            "processing": ("#322A17", COLORS["warning"]),
            "error": ("#311B25", COLORS["danger"]),
        }
        background, foreground = palettes[state]
        self.status_label.config(text=text, bg=background, fg=foreground)

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
        self.indicator.configure(bg=COLORS["border"])
        panel = tk.Frame(self.indicator, bg="#171B25", padx=16, pady=12)
        panel.pack(padx=1, pady=1)
        self.indicator_canvas = tk.Canvas(panel, width=34, height=34, bg="#171B25", highlightthickness=0)
        self.indicator_canvas.pack(side=tk.LEFT, padx=(0, 11))
        labels = tk.Frame(panel, bg="#171B25")
        labels.pack(side=tk.LEFT)
        self.indicator_title = tk.Label(labels, text="Recording", bg="#171B25", fg=COLORS["text"], font=("Segoe UI Semibold", 10))
        self.indicator_title.pack(anchor=tk.W)
        self.indicator_detail = tk.Label(labels, text="Listening for your voice", bg="#171B25", fg=COLORS["muted"], font=("Segoe UI", 9))
        self.indicator_detail.pack(anchor=tk.W)

    def show_indicator(self, title, detail, state):
        if self.indicator_after_id:
            self.root.after_cancel(self.indicator_after_id)
            self.indicator_after_id = None
        color = COLORS["danger"] if state == "recording" else COLORS["warning"]
        self.indicator_title.config(text=title, fg=color)
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
        y = self.root.winfo_screenheight() - self.indicator.winfo_height() - 90
        self.indicator.geometry(f"+{x}+{y}")

    def draw_indicator(self):
        self.indicator_after_id = None
        self.indicator_canvas.delete("all")
        color = COLORS["danger"] if self.indicator_state == "recording" else COLORS["warning"]
        if self.indicator_state == "recording":
            heights = [10, 18, 26, 18, 10]
            shift = self.indicator_phase % 4
            for index, height in enumerate(heights):
                animated_height = max(7, height - abs((index + shift) % 5 - 2) * 3)
                x = 3 + index * 7
                self.indicator_canvas.create_line(x, 17 - animated_height // 2, x, 17 + animated_height // 2, fill=color, width=4)
            if not self.reduce_motion:
                self.indicator_phase += 1
                self.indicator_after_id = self.root.after(180, self.draw_indicator)
        else:
            self.indicator_canvas.create_oval(7, 7, 27, 27, fill=color, outline="")
            self.indicator_canvas.create_arc(10, 10, 24, 24, start=40, extent=275, outline="#171B25", width=2)

    def hide_indicator(self):
        if self.indicator_after_id:
            self.root.after_cancel(self.indicator_after_id)
            self.indicator_after_id = None
        self.indicator.withdraw()

    def create_tray_icon(self):
        image = Image.new("RGBA", (64, 64), COLORS["background"])
        draw = ImageDraw.Draw(image)
        draw.ellipse((8, 8, 56, 56), fill=COLORS["accent"])
        draw.rounded_rectangle((26, 18, 38, 39), radius=6, fill=COLORS["background"])
        draw.arc((20, 27, 44, 48), start=0, end=180, fill=COLORS["background"], width=4)
        draw.line((32, 48, 32, 54), fill=COLORS["background"], width=4)
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
    root = tk.Tk()
    app = WhisperLiveApp(root)
    root.mainloop()
