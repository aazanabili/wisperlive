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
        self.root.title("WhisperLive Settings")
        self.root.geometry("400x300")
        
        self.config = load_config()
        self.recorder = AudioRecorder()
        self.is_processing = False
        
        self.setup_ui()
        self.setup_hotkeys()
        
        # Override close button to just minimize or hide? 
        # For simplicity, closing the window closes the app right now.
        
    def setup_ui(self):
        frame = ttk.Frame(self.root, padding="20")
        frame.pack(fill=tk.BOTH, expand=True)
        
        # Target Language
        ttk.Label(frame, text="لغة الترجمة (Target Language):").pack(anchor=tk.W, pady=(0, 5))
        self.lang_var = tk.StringVar(value=self.config.get("target_language", "English"))
        lang_combo = ttk.Combobox(frame, textvariable=self.lang_var, values=["English", "العربية", "Français", "Español", "Deutsch"])
        lang_combo.pack(fill=tk.X, pady=(0, 15))
        
        # Shortcut
        ttk.Label(frame, text="الاختصار (Shortcut):").pack(anchor=tk.W, pady=(0, 5))
        self.shortcut_var = tk.StringVar(value=self.config.get("shortcut", "ctrl+space"))
        shortcut_entry = ttk.Entry(frame, textvariable=self.shortcut_var)
        shortcut_entry.pack(fill=tk.X, pady=(0, 15))
        
        # Mode
        ttk.Label(frame, text="وضع التشغيل (Mode):").pack(anchor=tk.W, pady=(0, 5))
        self.mode_var = tk.StringVar(value=self.config.get("mode", "toggle"))
        mode_combo = ttk.Combobox(frame, textvariable=self.mode_var, values=["toggle", "hold"])
        mode_combo.pack(fill=tk.X, pady=(0, 15))
        
        # Save Button
        save_btn = ttk.Button(frame, text="حفظ وتطبيق (Save & Apply)", command=self.save_and_apply)
        save_btn.pack(pady=10)
        
        # Status Label
        self.status_label = ttk.Label(frame, text="جاهز. التطبيق يعمل في الخلفية.", foreground="green")
        self.status_label.pack(pady=5)

    def save_and_apply(self):
        # Remove old hotkeys
        keyboard.unhook_all()
        
        self.config["target_language"] = self.lang_var.get()
        self.config["shortcut"] = self.shortcut_var.get()
        self.config["mode"] = self.mode_var.get()
        save_config(self.config)
        
        self.setup_hotkeys()
        self.status_label.config(text="تم الحفظ وتحديث الاختصارات.")
        
    def setup_hotkeys(self):
        shortcut = self.config.get("shortcut", "ctrl+space")
        mode = self.config.get("mode", "toggle")
        
        try:
            if mode == "toggle":
                keyboard.add_hotkey(shortcut, self.toggle_recording, suppress=True)
            elif mode == "hold":
                # For hold mode, it's highly recommended to use a single key like F9 or F10
                # because holding modifiers + key combinations causes repeated OS events.
                # Here we bind to the main key.
                main_key = shortcut.split('+')[-1].strip()
                keyboard.on_press_key(main_key, self.on_key_press, suppress=False)
                keyboard.on_release_key(main_key, self.on_key_release, suppress=False)
        except Exception as e:
            print(f"Error setting up hotkeys: {e}")
            self.status_label.config(text=f"خطأ في الاختصار: {e}", foreground="red")
            
    def on_key_press(self, event):
        mode = self.config.get("mode", "toggle")
        shortcut = self.config.get("shortcut", "ctrl+space")
        if mode != "hold": return
        
        # Check if modifiers are pressed if required by the shortcut
        parts = [p.strip().lower() for p in shortcut.split('+')]
        modifiers = [p for p in parts if p in ['ctrl', 'shift', 'alt', 'windows']]
        
        for mod in modifiers:
            if not keyboard.is_pressed(mod):
                return
                
        if not self.recorder.is_recording and not self.is_processing:
            self.start_recording()

    def on_key_release(self, event):
        mode = self.config.get("mode", "toggle")
        if mode != "hold": return
        
        if self.recorder.is_recording:
            self.stop_and_process()

    def toggle_recording(self):
        if self.is_processing:
            print("Currently processing, please wait...")
            return
            
        if self.recorder.is_recording:
            self.stop_and_process()
        else:
            self.start_recording()
            
    def start_recording(self):
        # Play a simple beep to notify user recording started
        winsound.Beep(1000, 100)
        self.status_label.config(text="يتم التسجيل الآن...", foreground="red")
        self.recorder.start_recording()
        
    def stop_and_process(self):
        self.status_label.config(text="جاري معالجة الصوت...", foreground="blue")
        # Play a different beep for stop
        winsound.Beep(800, 100)
        audio_file = self.recorder.stop_recording()
        
        self.is_processing = True
        
        # Run processing in background so UI doesn't freeze
        threading.Thread(target=self.process_audio_thread, args=(audio_file,)).start()
        
    def process_audio_thread(self, audio_file):
        try:
            target_lang = self.config.get("target_language", "English")
            text = process_audio(audio_file, target_lang)
            if text:
                paste_text(text)
                # Success beep
                winsound.Beep(1200, 200)
            self.status_label.config(text="تم بنجاح. جاهز.", foreground="green")
        except Exception as e:
            self.status_label.config(text="حدث خطأ في المعالجة.", foreground="red")
            print(f"Error: {e}")
        finally:
            self.is_processing = False

if __name__ == "__main__":
    root = tk.Tk()
    app = WhisperLiveApp(root)
    root.mainloop()
