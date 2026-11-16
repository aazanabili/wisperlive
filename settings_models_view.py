"""Models tab: authoritative model-size selector + honest store state (A5A7 s4/s1.5).

WHY its own module (R-6 Option A / annex s4 budget): the size selector is the
SINGLE WRITER of ``model_size`` (ruling Q1 Option A) and renders the A5A7
``download_state_copy`` vocabulary against the Phase-5 ``app.model_store`` port
— with no store present every action is disabled WITH its reason (R6: never
selectable-and-broken; the copy never claims completion before SHA-256).
"""

import tkinter as tk
from tkinter import ttk

from local_engine import resolve_model_id
from settings_dictation_view import (STORE_ABSENT_REASON, download_state_copy,
                                     option_radio)
from themes import COLORS


class SettingsModelsMixin:
    """Builders/state for the Models tab (self = the host app)."""

    def build_models_tab(self, panel, config, widgets):
        """Authoritative model-size selector (Q1 Option A) + honest store state.
        Phase 5 ships ``app.model_store``; until it exists every action renders
        disabled WITH its reason (never selectable-and-broken, R6)."""
        c = COLORS
        size_var = tk.StringVar(value=config.get("model_size", "auto"))
        if size_var.get() not in ("auto", "fast", "standard"):
            size_var.set("auto")
        self.add_label(panel, "Local model")
        echo = tk.Label(panel, bg=c["surface"], fg=c["muted"], font=("Segoe UI", 9),
                        wraplength=400, justify=tk.LEFT)
        echo.pack(anchor=tk.W, pady=(0, 6))
        for value, text in (("auto", "Auto (pick for this computer)"),
                            ("fast", "Fast (small and quick)"),
                            ("standard", "Standard (more accurate, slower)")):
            option_radio(panel, text, size_var, value,
                         command=self._refresh_models_view).pack(anchor=tk.W)
        progress = ttk.Progressbar(panel, mode="determinate", maximum=100)
        progress.pack(fill=tk.X, pady=(8, 4))
        detail = tk.Label(panel, text="", bg=c["surface"], fg=c["muted"],
                          font=("Segoe UI", 9), wraplength=400, justify=tk.LEFT)
        detail.pack(anchor=tk.W, pady=(0, 4))
        state_label = tk.Label(panel, text="", bg=c["surface"], fg=c["muted"],
                               font=("Segoe UI", 9), wraplength=400, justify=tk.LEFT)
        state_label.pack(anchor=tk.W, pady=(0, 6))
        store_reason = tk.Label(panel, text="", bg=c["surface"], fg=c["warning"],
                                font=("Segoe UI", 9), wraplength=400, justify=tk.LEFT)
        store_reason.pack(anchor=tk.W, pady=(0, 6))
        actions = tk.Frame(panel, bg=c["surface"])
        actions.pack(fill=tk.X, pady=(0, 6))
        buttons = {}
        for name in ("Download", "Pause", "Resume", "Cancel download",
                     "Re-check file", "Delete"):
            buttons[name] = self.create_button(
                actions, name, lambda _n=name: self._model_action(_n), secondary=True)
            buttons[name].pack(side=tk.LEFT, padx=(0, 6))
        tk.Label(panel, text="Downloads are verified against SHA-256 before use.",
                 bg=c["surface"], fg=c["muted"], font=("Segoe UI", 9)).pack(anchor=tk.W)
        widgets.update({"model_size_var": size_var, "models_echo": echo,
                        "download_state": state_label, "download_detail": detail,
                        "download_progress": progress, "download_actions": buttons,
                        "download_store_reason": store_reason})
        self._models_size_var = size_var
        self._refresh_models_view()
        return lambda: {"model_size": size_var.get()}

    def _model_action(self, name):
        """Port seam: Phase-5 ``app.model_store`` owns every action (duck-typed);
        with no store the buttons are disabled, so this is a no-op safety net."""
        store = getattr(self, "model_store", None)
        state_label = getattr(self, "settings_widgets", {}).get("download_state")
        if store is None or state_label is None:
            return
        try:
            getattr(store, name.lower().replace(" ", "_"))()
        except Exception:
            state_label.config(text=download_state_copy("failed"), fg=COLORS["danger"])

    def _refresh_models_view(self):
        widgets = getattr(self, "settings_widgets", {})
        config = getattr(self, "config", {})
        size_var = getattr(self, "_models_size_var", None)
        size = size_var.get() if size_var is not None else "auto"
        echo = widgets.get("models_echo")
        if echo is not None:
            echo.config(text=f"Model size: {size} \u2014 set under Dictation.")
        state_label = widgets.get("download_state")
        if state_label is None:
            return
        model_id = resolve_model_id(size)                   # registry-mapped ids (D12)
        local_models = config.get("local_models") if isinstance(config, dict) else {}
        ready = isinstance(local_models, dict) and local_models.get(model_id) == "ready"
        if ready:
            state_label.config(text=download_state_copy("ready", size=size),
                               fg=COLORS["success"])
        else:
            state_label.config(text=download_state_copy("not_downloaded", size=size),
                               fg=COLORS["muted"])
        reason = widgets.get("download_store_reason")
        if reason is not None:
            reason.config(text="" if getattr(self, "model_store", None) is not None
                          else STORE_ABSENT_REASON)
        for button in widgets.get("download_actions", {}).values():
            button.configure(state=tk.NORMAL if getattr(self, "model_store", None)
                             is not None else tk.DISABLED)
        # Model-size is single-writer (Q1): the Dictation tab's echoes/notes
        # re-render against the Models tab's LIVE variable.
        self._dictation_visibility()
