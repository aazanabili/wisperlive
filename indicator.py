"""Floating recording/processing indicator window (self-contained component).

Owns its window, widgets, animation state, and reduced-motion flag. Host
calls: show()/hide()/refresh_theme(). Drawing math, -112 px offset, 40 ms
pulse, and tokens are byte-identical to upstream (pixel-identical mandate).
"""

import ctypes
import math
import os
import tkinter as tk

from themes import COLORS


def prefers_reduced_motion() -> bool:
    if os.name != "nt":
        # Upstream platform guard: ctypes.windll does not exist outside Windows.
        return False
    animations_enabled = ctypes.c_int()
    try:
        # SPI_GETCLIENTAREAANIMATION reflects the Windows animation accessibility setting.
        result = ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(animations_enabled), 0)
        return bool(result) and not bool(animations_enabled.value)
    except OSError:
        return False


class RecordingIndicator:
    """The on-screen recording/processing pill (upstream indicator code,
    renamed per the ui b.3 mapping: indicator->window, indicator_panel->panel,
    indicator_canvas->canvas, indicator_labels->labels, indicator_title->
    title_label, indicator_detail->detail_label, indicator_after_id->after_id,
    indicator_phase->phase, indicator_state->state)."""

    def __init__(self, root) -> None:
        self.root = root
        self.after_id = None
        self.phase = 0
        self.state = None
        self.reduce_motion = prefers_reduced_motion()
        self.window = tk.Toplevel(self.root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.configure(bg=COLORS["indicator_border"])
        panel = tk.Frame(self.window, bg=COLORS["indicator"], padx=18, pady=10)
        panel.pack(padx=1, pady=1)
        self.panel = panel
        self.canvas = tk.Canvas(panel, width=46, height=46, bg=COLORS["indicator"], highlightthickness=0)
        self.canvas.pack(side=tk.LEFT, padx=(0, 11))
        labels = tk.Frame(panel, bg=COLORS["indicator"])
        labels.pack(side=tk.LEFT)
        self.labels = labels
        self.title_label = tk.Label(labels, text="Recording", bg=COLORS["indicator"], fg=COLORS["indicator_text"], font=("Segoe UI Semibold", 10))
        self.title_label.pack(anchor=tk.W)
        self.detail_label = tk.Label(labels, text="Listening for your voice", bg=COLORS["indicator"], fg=COLORS["muted"], font=("Segoe UI", 9))
        self.detail_label.pack(anchor=tk.W)

    def refresh_theme(self) -> None:
        if self.after_id:
            self.root.after_cancel(self.after_id)
            self.after_id = None
        self.window.configure(bg=COLORS["indicator_border"])
        self.panel.configure(bg=COLORS["indicator"])
        self.labels.configure(bg=COLORS["indicator"])
        self.canvas.configure(bg=COLORS["indicator"])
        title_color = COLORS["warning"] if self.state == "processing" else COLORS["indicator_text"]
        self.title_label.configure(bg=COLORS["indicator"], fg=title_color)
        self.detail_label.configure(bg=COLORS["indicator"], fg=COLORS["muted"])
        if self.state:
            self._draw()

    def show(self, title: str, detail: str, state: str) -> None:
        if self.after_id:
            self.root.after_cancel(self.after_id)
            self.after_id = None
        title_color = COLORS["warning"] if state == "processing" else COLORS["indicator_text"]
        self.title_label.config(text=title, fg=title_color)
        self.detail_label.config(text=detail)
        self._position()
        self.window.deiconify()
        self.window.lift()
        self.state = state
        self._draw()

    def hide(self) -> None:
        if self.after_id:
            self.root.after_cancel(self.after_id)
            self.after_id = None
        self.window.withdraw()

    def _position(self) -> None:
        self.window.update_idletasks()
        width = self.window.winfo_width()
        x = (self.root.winfo_screenwidth() - width) // 2
        y = self.root.winfo_screenheight() - self.window.winfo_height() - 112
        self.window.geometry(f"+{x}+{y}")

    def _draw(self) -> None:
        self.after_id = None
        self.canvas.delete("all")
        pulse = (math.sin(self.phase) + 1) / 2
        is_processing = self.state == "processing"
        accent = COLORS["warning"] if is_processing else COLORS["indicator_accent"]
        glow = COLORS["warning"] if is_processing else COLORS["indicator_glow"]
        outer_radius = 18 + int(pulse * 3)
        center = 23
        self.canvas.create_oval(
            center - outer_radius, center - outer_radius, center + outer_radius, center + outer_radius,
            outline=glow, width=2,
        )
        self.canvas.create_arc(
            7, 7, 39, 39, start=int(self.phase * 38), extent=120,
            outline=accent, width=2,
        )
        core_radius = 8 + int(pulse * 2)
        self.canvas.create_oval(
            center - core_radius, center - core_radius, center + core_radius, center + core_radius,
            fill=accent, outline="",
        )
        self.canvas.create_oval(21, 21, 25, 25, fill=COLORS["indicator"], outline="")
        if not self.reduce_motion:
            self.phase += 0.14
            self.after_id = self.root.after(40, self._draw)
