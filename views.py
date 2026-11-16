"""Main-window construction, widget factories, and window/shortcut handling.

Single-class mixin; host: WhisperLiveApp. Bodies are verbatim relocations of
upstream `main.py` (upstream strings/tokens win - pixel-identical mandate).
`self.` refers to the host instance; this module never imports `main`.
"""

import os
import tkinter as tk
from tkinter import ttk
import webbrowser

from config_manager import save_config
from llm_api import NO_TRANSLATION
from themes import COLORS, THEMES
from updater import RELEASES_PAGE
from version import APP_VERSION
from windows_hotkey import WindowsGlobalHotkey


class MainUiMixin:
    """Main-window construction and widget event handlers (presentation layer)."""

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
        self.create_button(header, "Settings ⚙", self.show_settings, secondary=True).pack(side=tk.RIGHT, padx=(8, 0))
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

        self.lang_var = tk.StringVar(value=self.config.get("target_language", "English"))
        self.add_label(settings, "Output language")
        lang_combo = ttk.Combobox(
            settings, textvariable=self.lang_var,
            values=["English", "العربية", "Français", "Español", "Deutsch", "中文", "日本語", NO_TRANSLATION],
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

        self.create_button(self.footer, "Open GitHub releases", self.open_releases, secondary=True).pack(side=tk.LEFT)
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

    def collect_form_values(self):
        self.config.update({
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
        self.indicator.refresh_theme()

    def show_about(self):
        about = tk.Toplevel(self.root)
        about.title("About WhisperLive")
        about.transient(self.root)
        about.resizable(False, False)
        about.configure(bg=COLORS["surface"])
        about.geometry("430x370")
        panel = tk.Frame(about, bg=COLORS["surface"], padx=30, pady=26)
        panel.pack(fill=tk.BOTH, expand=True)

        tk.Label(panel, text="WhisperLive", bg=COLORS["surface"], fg=COLORS["text"], font=("Segoe UI Semibold", 20)).pack(anchor=tk.W)
        tk.Label(
            panel, text=f"Current version: {APP_VERSION}", bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Segoe UI", 10),
        ).pack(anchor=tk.W, pady=(3, 16))
        tk.Frame(panel, bg=COLORS["border"], height=1).pack(fill=tk.X, pady=(0, 16))
        tk.Label(
            panel, text="Informatics Engineer Abdullatif Zanabili", bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Segoe UI Semibold", 11), wraplength=340, justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 16))
        tk.Label(panel, text="Connect", bg=COLORS["surface"], fg=COLORS["text"], font=("Segoe UI Semibold", 10)).pack(anchor=tk.W, pady=(0, 5))
        self.create_link(panel, "Personal website", "https://zanabili.dev/").pack(anchor=tk.W, pady=2)
        self.create_link(panel, "Support WhisperLive", "https://paypal.me/Zanabili").pack(anchor=tk.W, pady=2)
        self.create_link(panel, "Project on GitHub", "https://github.com/aazanabili/wisperlive").pack(anchor=tk.W, pady=2)
        self.create_link(panel, "Questions: info@zanabili.dev", "mailto:info@zanabili.dev").pack(anchor=tk.W, pady=2)
        self.create_button(panel, "Close", about.destroy, secondary=True).pack(anchor=tk.E, pady=(20, 0))

    def create_link(self, parent, text, url):
        link = tk.Label(
            parent, text=text, bg=COLORS["surface"], fg=COLORS["accent"], cursor="hand2",
            font=("Segoe UI Semibold", 10),
        )
        link.bind("<Button-1>", lambda _event: webbrowser.open(url))
        link.bind("<Enter>", lambda _event: link.config(fg=COLORS["accent_hover"]))
        link.bind("<Leave>", lambda _event: link.config(fg=COLORS["accent"]))
        return link

    def open_releases(self):
        webbrowser.open(RELEASES_PAGE)

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

    def setup_hotkeys(self):
        self.clear_hotkeys()
        shortcut = self.config.get("shortcut", "ctrl+space")
        try:
            if self.config.get("mode") == "toggle":
                callback = lambda: self.call_in_ui(self.toggle_recording)
            else:
                callback = lambda: self.call_in_ui(self.start_hold_recording)
            if os.name == "nt":
                hotkey_class = WindowsGlobalHotkey
            else:
                from linux_hotkey import LinuxGlobalHotkey
                hotkey_class = LinuxGlobalHotkey
            self.hotkey = hotkey_class(shortcut, callback)
            self.hotkey.start()
        except Exception as error:
            self.hotkey = None
            self.set_status(f"Shortcut error: {error}", "error")

    def clear_hotkeys(self):
        if self.hold_poll_after_id:
            self.root.after_cancel(self.hold_poll_after_id)
            self.hold_poll_after_id = None
        if self.hotkey:
            self.hotkey.stop()
            self.hotkey = None

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

    def on_window_unmap(self, _event):
        if self.config.get("minimize_to_tray") and self.tray_icon:
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
        if self.config.get("minimize_to_tray") and self.tray_icon:
            self.hide_window()
        else:
            self.quit_app()
