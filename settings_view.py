"""LLM provider settings dialog (issue-4 feature; upstream show_settings verbatim).

Second mixin module: views + show_settings would exceed the <=350-line gate.
`self.` refers to the host instance (WhisperLiveApp); never imports `main`.
"""

import logging
import threading
import tkinter as tk
from tkinter import ttk

from config_manager import save_config
from llm_api import LLMError, fetch_models
from themes import COLORS

logger = logging.getLogger(__name__)


class SettingsDialogMixin:
    """Provider settings dialog (Base URL / API key / Model + Fetch Models)."""

    def show_settings(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Settings — LLM provider")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.configure(bg=COLORS["surface"])
        panel = tk.Frame(dialog, bg=COLORS["surface"], padx=26, pady=22)
        panel.pack(fill=tk.BOTH, expand=True)

        self.add_label(panel, "Provider Base URL")
        base_var = tk.StringVar(value=self.config.get("provider_base_url", ""))
        self.create_entry(panel, base_var).pack(fill=tk.X, pady=(0, 10))

        self.add_label(panel, "API key")
        key_row = tk.Frame(panel, bg=COLORS["surface"])
        key_row.pack(fill=tk.X, pady=(0, 10))
        key_var = tk.StringVar(value=self.config.get("api_key", ""))
        key_entry = self.create_entry(key_row, key_var, show="*")
        key_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        def toggle_key_visibility():
            if key_entry.cget("show") == "*":
                key_entry.config(show="")
                show_btn.config(text="Hide")
            else:
                key_entry.config(show="*")
                show_btn.config(text="Show")

        show_btn = self.create_button(key_row, "Show", toggle_key_visibility, secondary=True)
        show_btn.pack(side=tk.LEFT, padx=(8, 0))

        self.add_label(panel, "Model")
        model_row = tk.Frame(panel, bg=COLORS["surface"])
        model_row.pack(fill=tk.X, pady=(0, 6))
        model_var = tk.StringVar(value=self.config.get("model", ""))
        model_combo = ttk.Combobox(model_row, textvariable=model_var, style="Dark.TCombobox")
        model_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        fetch_btn = self.create_button(model_row, "Fetch Models", lambda: start_fetch(), secondary=True)
        fetch_btn.pack(side=tk.LEFT, padx=(8, 0))

        status = tk.Label(panel, text="", bg=COLORS["surface"], fg=COLORS["muted"],
                          font=("Segoe UI", 9), justify=tk.LEFT, wraplength=380)
        status.pack(anchor=tk.W, pady=(0, 10))

        def show_result(message, color):
            try:
                status.config(text=message, fg=color)
                fetch_btn.config(state=tk.NORMAL)
            except tk.TclError:
                pass  # the dialog was closed while the fetch was running

        def start_fetch():
            base_url = base_var.get().strip()
            api_key = key_var.get().strip()
            fetch_btn.config(state=tk.DISABLED)
            status.config(text="Fetching models...", fg=COLORS["muted"])

            def worker():
                try:
                    models = fetch_models(base_url, api_key)
                except LLMError as error:
                    message = str(error)
                    self.call_in_ui(lambda: show_result(message, COLORS["danger"]))
                    return
                except Exception:
                    logger.exception("Unexpected error while fetching models")
                    self.call_in_ui(lambda: show_result("Unexpected error while fetching models.", COLORS["danger"]))
                    return

                def apply_models():
                    try:
                        model_combo.configure(values=models)
                        if models and not model_var.get().strip():
                            model_var.set(models[0])
                        show_result(f"{len(models)} model(s) available.", COLORS["success"])
                    except tk.TclError:
                        pass  # the dialog was closed while the fetch was running

                self.call_in_ui(apply_models)

            threading.Thread(target=worker, daemon=True).start()

        def save_settings():
            self.config["provider_base_url"] = base_var.get().strip()
            self.config["api_key"] = key_var.get().strip()
            self.config["model"] = model_var.get().strip()
            saved = save_config(self.config)
            dialog.destroy()
            if saved:
                self.set_status("Provider settings saved. They apply to the next recording.", "ready")
            else:
                self.set_status("Provider settings could not be saved to disk.", "error")

        buttons = tk.Frame(panel, bg=COLORS["surface"])
        buttons.pack(fill=tk.X)
        self.create_button(buttons, "Cancel", dialog.destroy, secondary=True).pack(side=tk.RIGHT)
        self.create_button(buttons, "Save", lambda: save_settings()).pack(side=tk.RIGHT, padx=(0, 8))
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.grab_set()
