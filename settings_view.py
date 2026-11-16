"""3-tab settings dialog shell: Dictation / Engines & keys / Models (A5A7).

Second mixin module: views + this dialog would exceed the <=350-line gate.
`self.` refers to the host instance (WhisperLiveApp); never imports `main`.
The tab CONTENT lives in the annex-s4 modules (R-6 Option A): provider rows in
`settings_provider_view` (its copy constants + `key_status_hint` re-exported
below for the pinned test namespace), dictation in `settings_dictation_view`,
keys/consent/quota in `settings_keys_view`, models in `settings_models_view`.
This shell owns the notebook, sizing, single-dialog guard, payload merge
(provider LAST - ruling R3 IFF) and the ONE save validator.
"""

import tkinter as tk
from tkinter import ttk

import providers
from config_manager import save_config, set_vendor_key
from settings_dictation_view import SettingsDictationMixin, validate_save
from settings_keys_view import SettingsKeysMixin
from settings_models_view import SettingsModelsMixin
from settings_provider_view import (BASE_URL_HINT, MIMO_MODEL_HINT,  # noqa: F401  re-exported
                                    SettingsProviderMixin, key_status_hint)
from themes import COLORS


class SettingsDialogMixin(SettingsDictationMixin, SettingsKeysMixin,
                          SettingsModelsMixin, SettingsProviderMixin):
    """Settings shell: notebook + save state machine (A5A7 s1.1/s3)."""

    def show_settings(self, tab=None):
        widgets = getattr(self, "settings_widgets", {})
        if widgets.get("dialog") is not None:      # single-dialog owner: select + lift
            widgets["notebook"].select(widgets["tabs"].get(tab or "dictation",
                                                           widgets["tabs"]["dictation"]))
            widgets["dialog"].lift()
            return
        self.settings_widgets = {}                 # builders register into THIS dict
        dialog = tk.Toplevel(self.root)
        dialog.title("Settings")
        dialog.transient(self.root)
        dialog.configure(bg=COLORS["surface"])
        notebook = ttk.Notebook(dialog)
        style = ttk.Style(dialog)                  # local + idempotent (grab blocks theme swap)
        style.configure("TNotebook", background=COLORS["background"], borderwidth=0)
        style.configure("TNotebook.Tab", padding=(14, 8), background=COLORS["surface_hover"],
                        foreground=COLORS["muted"])
        style.map("TNotebook.Tab", background=[("selected", COLORS["surface"])],
                  foreground=[("selected", COLORS["text"])])
        panel_d = tk.Frame(notebook, bg=COLORS["surface"], padx=26, pady=22)
        panel = tk.Frame(notebook, bg=COLORS["surface"], padx=26, pady=22)  # Engines & keys
        panel_m = tk.Frame(notebook, bg=COLORS["surface"], padx=26, pady=22)
        for frame, label in ((panel_d, "Dictation"), (panel, "Engines & keys"),
                             (panel_m, "Models")):
            notebook.add(frame, text=label)
        notebook.pack(fill=tk.BOTH, expand=True)
        tabs = {"dictation": panel_d, "engines": panel, "models": panel_m}
        dictation_payload = self.build_dictation_tab(panel_d, self.config, notebook,
                                                      self.settings_widgets)
        models_payload = self.build_models_tab(panel_m, self.config, self.settings_widgets)
        extras, keys_payload = self.build_engines_extras(panel, self.config,
                                                         self.settings_widgets)
        # status/buttons are dialog-level so Save is visible on EVERY tab.
        status = tk.Label(dialog, text="", bg=COLORS["surface"], fg=COLORS["muted"],
                          font=("Segoe UI", 9), justify=tk.LEFT, wraplength=380)
        buttons = tk.Frame(dialog, bg=COLORS["surface"])
        self.build_provider_panel(panel, status, buttons, extras)

        keys_present = "api_keys_protected" in self.config
        keys_before = self.config.get("api_keys_protected")   # K7 open-state reference (R-U7)

        def destroy_dialog():
            dialog.destroy()
            self.settings_widgets = {}

        def close_dialog(_event=None):
            # Cancel/Escape discards EVERYTHING: the K7 switch-time vendor filing
            # must never leak past a discarded dialog (config == open state).
            if keys_present:
                self.config["api_keys_protected"] = keys_before
            else:
                self.config.pop("api_keys_protected", None)
            destroy_dialog()

        def save_settings():
            # Tab payloads merge FIRST (dictation/keys/models), the ONE save
            # validator can block, then the provider payload LAST (R3 IFF rule
            # enforced in providers.save_payload).  The provider panel snapshot
            # feeds BOTH the F-2 endpoint validation and the payload write.
            panel_values = {
                "provider": self.settings_widgets["provider_var"].get(),
                "provider_variant": self.settings_widgets["variant_var"].get(),
                "provider_base_url": self.settings_widgets["base_var"].get(),
                "api_key": self.settings_widgets["key_var"].get(),
                "model": self.settings_widgets["model_var"].get(),
                "transcription_model": self.settings_widgets["transcription_var"].get(),
            }
            issue = self.apply_settings_payloads(
                (dictation_payload(), keys_payload(), models_payload()), notebook, panel,
                panel_values)
            if issue:
                return
            self.config.update(providers.save_payload(panel_values))
            # K7: file the final field under the ACTIVE vendor so a reopen re-hydrates
            # this exact key (switches already filed each origin vendor — plan R-U7).
            set_vendor_key(self.config, providers.resolve_provider_id(
                self.config.get("provider")), panel_values["api_key"])
            saved = save_config(self.config)
            destroy_dialog()
            if saved:
                self.set_status("Provider settings saved. They apply to the next recording.", "ready")
            else:
                self.set_status("Provider settings could not be saved to disk.", "error")

        cancel_btn = self.create_button(buttons, "Cancel", close_dialog, secondary=True)
        cancel_btn.pack(side=tk.RIGHT)
        save_btn = self.create_button(buttons, "Save", lambda: save_settings())
        save_btn.pack(side=tk.RIGHT, padx=(0, 8))
        self.settings_widgets.update({
            "dialog": dialog, "panel": panel, "status": status, "buttons": buttons,
            "save_btn": save_btn, "cancel_btn": cancel_btn,
            "notebook": notebook, "tabs": tabs,
        })
        # A5A7 1.1: size after ALL tabs build (select + realize each, then
        # restore the requested tab); focus the first Dictation control AFTER
        # the selection so the focus target is the visible tab's widget.
        def _fit():
            heights = []
            for frame in (panel_d, panel, panel_m):
                notebook.select(frame)
                dialog.update_idletasks()
                heights.append(frame.winfo_reqheight())
            start = tabs.get(tab or "dictation", panel_d)
            notebook.select(start)
            width = max(680, notebook.winfo_reqwidth())
            height = max(heights) + 96          # + notebook chrome + status + buttons
            dialog.geometry(f"{width}x{height}")
            dialog.minsize(width, height)
            dialog.resizable(False, True)

        dialog.after_idle(_fit)
        dialog.after_idle(lambda: self.settings_widgets["mode_local"].focus_set())
        dialog.bind("<Escape>", lambda _event: close_dialog())
        dialog.grab_set()
