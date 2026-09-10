import tkinter as tk
from tkinter import ttk
import threading
import keyboard
from config_manager import load_config, save_config
from audio_recorder import AudioRecorder
from gemini_api import process_audio
from auto_typer import paste_text
import winsound


class WhisperLiveApp:
    def __init__(self, root):
        self.root = root
        self.root.title("WhisperLive - Settings")
        self.root.geometry("440x400")
        self.root.resizable(False, False)

        self.config = load_config()
        self.recorder = AudioRecorder()
        self.is_processing = False

        self.setup_ui()
        self.setup_hotkeys()

    def setup_ui(self):
        frame = ttk.Frame(self.root, padding="20")
        frame.pack(fill=tk.BOTH, expand=True)

        # ── API Key ──────────────────────────────────────────────
        ttk.Label(frame, text="Gemini API Key:").pack(anchor=tk.W, pady=(0, 4))
        self.api_key_var = tk.StringVar(value=self.config.get("api_key", ""))

        api_frame = ttk.Frame(frame)
        api_frame.pack(fill=tk.X, pady=(0, 4))

        self.api_entry = ttk.Entry(api_frame, textvariable=self.api_key_var, show="*")
        self.api_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.show_btn = ttk.Button(api_frame, text="👁", width=3, command=self.toggle_api_visibility)
        self.show_btn.pack(side=tk.LEFT, padx=(4, 0))

        ttk.Label(frame, text="Get a free key → aistudio.google.com/app/apikey",
                  foreground="gray", font=("Segoe UI", 8)).pack(anchor=tk.W, pady=(0, 14))

        # ── Target Language ──────────────────────────────────────
        ttk.Label(frame, text="Target Language (لغة الترجمة):").pack(anchor=tk.W, pady=(0, 4))
        self.lang_var = tk.StringVar(value=self.config.get("target_language", "English"))
        lang_combo = ttk.Combobox(
            frame, textvariable=self.lang_var,
            values=["English", "العربية", "Français", "Español", "Deutsch", "中文", "日本語"],
            state="readonly"
        )
        lang_combo.pack(fill=tk.X, pady=(0, 14))

        # ── Shortcut ─────────────────────────────────────────────
        ttk.Label(frame, text="Shortcut (الاختصار):").pack(anchor=tk.W, pady=(0, 4))
        self.shortcut_var = tk.StringVar(value=self.config.get("shortcut", "ctrl+space"))
        ttk.Entry(frame, textvariable=self.shortcut_var).pack(fill=tk.X, pady=(0, 14))

        # ── Mode ─────────────────────────────────────────────────
        ttk.Label(frame, text="Recording Mode (وضع التشغيل):").pack(anchor=tk.W, pady=(0, 4))
        self.mode_var = tk.StringVar(value=self.config.get("mode", "toggle"))
        mode_combo = ttk.Combobox(
            frame, textvariable=self.mode_var,
            values=["toggle (press once to start, once to stop)",
                    "hold (hold key while speaking)"],
            state="readonly"
        )
        mode_combo.pack(fill=tk.X, pady=(0, 14))

        # ── Save Button ───────────────────────────────────────────
        ttk.Button(frame, text="💾  Save & Apply", command=self.save_and_apply).pack(pady=(0, 8))

        # ── Status Label ─────────────────────────────────────────
        self.status_label = ttk.Label(frame, text="Ready. Listening for shortcut in background.",
                                      foreground="green")
        self.status_label.pack()

    def toggle_api_visibility(self):
        if self.api_entry.cget("show") == "*":
            self.api_entry.config(show="")
            self.show_btn.config(text="🙈")
        else:
            self.api_entry.config(show="*")
            self.show_btn.config(text="👁")

    def save_and_apply(self):
        keyboard.unhook_all()

        # Read mode (strip the description part if combo shows it)
        raw_mode = self.mode_var.get()
        mode = "hold" if raw_mode.startswith("hold") else "toggle"

        self.config["api_key"] = self.api_key_var.get().strip()
        self.config["target_language"] = self.lang_var.get()
        self.config["shortcut"] = self.shortcut_var.get().strip()
        self.config["mode"] = mode

        save_config(self.config)
        self.setup_hotkeys()
        self.status_label.config(text="Saved! Shortcuts updated.", foreground="green")

    def setup_hotkeys(self):
        shortcut = self.config.get("shortcut", "ctrl+space")
        mode = self.config.get("mode", "toggle")
        try:
            if mode == "toggle":
                keyboard.add_hotkey(shortcut, self.toggle_recording, suppress=True)
            else:
                main_key = shortcut.split('+')[-1].strip()
                keyboard.on_press_key(main_key, self.on_key_press, suppress=False)
                keyboard.on_release_key(main_key, self.on_key_release, suppress=False)
        except Exception as e:
            self.status_label.config(text=f"Shortcut error: {e}", foreground="red")

    def on_key_press(self, event):
        if self.config.get("mode") != "hold":
            return
        parts = [p.strip().lower() for p in self.config.get("shortcut", "").split('+')]
        mods = [p for p in parts if p in ['ctrl', 'shift', 'alt', 'windows']]
        if all(keyboard.is_pressed(m) for m in mods):
            if not self.recorder.is_recording and not self.is_processing:
                self.start_recording()

    def on_key_release(self, event):
        if self.config.get("mode") != "hold":
            return
        if self.recorder.is_recording:
            self.stop_and_process()

    def toggle_recording(self):
        if self.is_processing:
            return
        if self.recorder.is_recording:
            self.stop_and_process()
        else:
            self.start_recording()

    def start_recording(self):
        api_key = self.config.get("api_key", "").strip()
        if not api_key:
            self.status_label.config(
                text="⚠ No API key! Enter it in settings and save.", foreground="red")
            winsound.Beep(400, 300)
            return
        winsound.Beep(1000, 100)
        self.status_label.config(text="🔴 Recording...", foreground="red")
        self.recorder.start_recording()

    def stop_and_process(self):
        winsound.Beep(800, 100)
        self.status_label.config(text="⏳ Processing...", foreground="blue")
        audio_file = self.recorder.stop_recording()
        self.is_processing = True
        threading.Thread(target=self.process_audio_thread, args=(audio_file,), daemon=True).start()

    def process_audio_thread(self, audio_file):
        try:
            api_key = self.config.get("api_key", "").strip()
            target_lang = self.config.get("target_language", "English")
            text = process_audio(audio_file, target_lang, api_key)
            if text:
                paste_text(text)
                winsound.Beep(1200, 200)
            self.status_label.config(text="✅ Done! Ready.", foreground="green")
        except Exception as e:
            self.status_label.config(text=f"❌ Error: {e}", foreground="red")
        finally:
            self.is_processing = False


if __name__ == "__main__":
    root = tk.Tk()
    app = WhisperLiveApp(root)
    root.mainloop()
