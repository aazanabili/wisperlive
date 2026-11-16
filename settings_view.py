"""LLM provider settings dialog (provider presets + Custom mode).

Second mixin module: views + this dialog would exceed the <=350-line gate.
`self.` refers to the host instance (WhisperLiveApp); never imports `main`.
Registry knowledge (preset ids, labels, endpoint URLs, variant ids, curated
model lists, key hints) lives ONLY in `providers.py`; this module renders it
and maps whatever the widgets display back through `providers.save_payload`
(ruling R3: `transcription_model` non-empty iff the provider is split-mode).
"""

import logging
import threading
import tkinter as tk
from tkinter import ttk

import providers
from config_manager import save_config
from llm_api import LLMError, fetch_models
from themes import COLORS

logger = logging.getLogger(__name__)

# Exact approved UI copy (A5A7 section 5) - ids/labels/URLs never appear here.
BASE_URL_HINT = "Preset URL — choose Custom (OpenAI-compatible URL) to edit it."
MIMO_MODEL_HINT = (
    "TTS models are not listed: WhisperLive never synthesizes speech. "
    "mimo-v2.5-asr officially supports Chinese and English only."
)
KEYLESS_FETCH_MESSAGE = "Add the API key first — this provider does not accept keyless requests."
STALE_FETCH_MESSAGE = "Ignored a model list fetched for the previous provider."
FALLBACK_KEY_HINT = "An API key is required by this provider; add one before recording."


def key_status_hint(provider_id, variant_id, api_key):
    """Muted idle-status hint: the token-plan format copy for a token-plan
    variant, the generic copy for any other key-required preset with an empty
    key, '' for a filled key or a keyless preset (Ollama/Custom, D2). Pure -
    importable without a Tk root; never raises on malformed input."""
    if not isinstance(api_key, str) or api_key.strip() or not providers.needs_api_key(provider_id):
        return ""
    variant = next((v for v in providers.variant_options(provider_id) if v.id == variant_id), None)
    preset = providers.get_provider(provider_id)
    if variant is not None and variant.key_hint and preset is not None and preset.api_key_hint:
        return preset.api_key_hint
    return FALLBACK_KEY_HINT


class SettingsDialogMixin:
    """Provider settings dialog (presets, endpoint/plan, URL, key, models)."""

    def show_settings(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Settings — LLM provider")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.configure(bg=COLORS["surface"])
        panel = tk.Frame(dialog, bg=COLORS["surface"], padx=26, pady=22)
        panel.pack(fill=tk.BOTH, expand=True)

        # Hydration (providers.initial_state): no legacy URL inference; a
        # pre-preset config shows Custom with the stored URL byte-identical.
        state = providers.initial_state(self.config)
        labels_by_id = dict(providers.provider_choices())
        provider_var = tk.StringVar(value=labels_by_id[state["provider"]])
        variant_labels = {v.id: v.label for v in providers.variant_options(state["provider"])}
        variant_var = tk.StringVar(value=variant_labels.get(state["provider_variant"], ""))
        base_var = tk.StringVar(value=state["provider_base_url"])
        key_var = tk.StringVar(value=state["api_key"])
        transcription_var = tk.StringVar(value=state["transcription_model"])
        model_var = tk.StringVar(value=state["model"])
        # Dialog-scoped scratch (never config): fetched dropdown suggestions per
        # provider, the in-flight fetch token (stale guard) and the fetch-owned
        # status text (None -> the key hint owns the status label).
        fetched_models = {}
        fetch_state = {"token": None}
        fetch_status = {"text": None, "color": None}

        # Row 1: Provider (always visible; labels from the registry).
        row_provider = tk.Frame(panel, bg=COLORS["surface"])
        self.add_label(row_provider, "Provider")
        provider_combo = ttk.Combobox(
            row_provider, textvariable=provider_var, values=list(providers.display_names()),
            state="readonly", style="Dark.TCombobox", width=32,
        )
        provider_combo.pack(fill=tk.X, pady=(0, 10))

        # Row 2: Endpoint / plan (packed only for presets with variants).
        row_variant = tk.Frame(panel, bg=COLORS["surface"])
        self.add_label(row_variant, "Endpoint / plan")
        variant_combo = ttk.Combobox(
            row_variant, textvariable=variant_var, values=[], state="readonly",
            style="Dark.TCombobox", width=30,
        )
        variant_combo.pack(fill=tk.X, pady=(0, 10))

        # Row 3: Provider Base URL - readonly (focusable/copyable) for presets,
        # editable for Custom (today's entry factory, width fits the longest URL).
        row_base = tk.Frame(panel, bg=COLORS["surface"])
        self.add_label(row_base, "Provider Base URL")
        base_entry = self.create_entry(row_base, base_var)
        base_entry.configure(width=54, readonlybackground=COLORS["surface_hover"])
        base_entry.pack(fill=tk.X, pady=(0, 10))
        base_hint = tk.Label(
            panel, text=BASE_URL_HINT, bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Segoe UI", 9), justify=tk.LEFT, wraplength=380,
        )

        # Row 4: API key - masked entry + Show/Hide, unchanged upstream behaviour.
        row_key = tk.Frame(panel, bg=COLORS["surface"])
        self.add_label(row_key, "API key")
        key_row = tk.Frame(row_key, bg=COLORS["surface"])
        key_row.pack(fill=tk.X, pady=(0, 10))
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

        # Row 5/6: split-mode model rows + one muted hint (packed for split
        # providers only; curated, editable, deliberately NO Fetch button).
        mimo_rows = tk.Frame(panel, bg=COLORS["surface"])
        self.add_label(mimo_rows, "Transcription model")
        transcription_combo = ttk.Combobox(
            mimo_rows, textvariable=transcription_var, style="Dark.TCombobox")
        transcription_combo.pack(fill=tk.X, pady=(0, 10))
        self.add_label(mimo_rows, "Post-processing model")
        post_combo = ttk.Combobox(mimo_rows, textvariable=model_var, style="Dark.TCombobox")
        post_combo.pack(fill=tk.X, pady=(0, 10))
        tk.Label(
            mimo_rows, text=MIMO_MODEL_HINT, bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Segoe UI", 9), justify=tk.LEFT, wraplength=380,
        ).pack(anchor=tk.W, pady=(0, 10))

        # Row 7: Model + Fetch Models (packed for every non-split provider).
        model_row = tk.Frame(panel, bg=COLORS["surface"])
        self.add_label(model_row, "Model")
        model_controls = tk.Frame(model_row, bg=COLORS["surface"])
        model_controls.pack(fill=tk.X, pady=(0, 6))
        model_combo = ttk.Combobox(model_controls, textvariable=model_var, style="Dark.TCombobox")
        model_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        fetch_btn = self.create_button(
            model_controls, "Fetch Models", lambda: start_fetch(), secondary=True)
        fetch_btn.pack(side=tk.LEFT, padx=(8, 0))

        status = tk.Label(panel, text="", bg=COLORS["surface"], fg=COLORS["muted"],
                          font=("Segoe UI", 9), justify=tk.LEFT, wraplength=380)
        buttons = tk.Frame(panel, bg=COLORS["surface"])

        def current_provider_id():
            # Registry-backed (never guessed): display label -> id, unknown -> custom.
            return providers.resolve_provider_id(
                providers.provider_id_from_display(provider_var.get()))

        def refresh_status():
            # Priority: fetch-owned message (in-flight/result) > key hint > empty.
            text, color = fetch_status["text"], fetch_status["color"]
            if text is None:
                pid = current_provider_id()
                text = key_status_hint(
                    pid, providers.resolve_variant_id(pid, variant_var.get()), key_var.get())
                color = COLORS["muted"]
            try:
                status.config(text=text, fg=color)
            except tk.TclError:
                pass  # the dialog was closed while a fetch was running

        def set_status_message(text, color):
            fetch_status["text"], fetch_status["color"] = text, color
            refresh_status()

        def show_result(message, color):
            try:
                fetch_state["token"] = None
                set_status_message(message, color)
                fetch_btn.config(state=tk.NORMAL)
            except tk.TclError:
                pass  # the dialog was closed while the fetch was running

        def set_row_visibility(pid):
            # Every row is created once and toggled with pack/pack_forget in
            # canonical order, so widget identity and tab order never change.
            variants = providers.variant_options(pid)
            preset = providers.get_provider(pid)
            split = providers.model_field_mode(pid) == providers.MODE_SPLIT
            if variants:
                variant_combo.configure(values=[variant.label for variant in variants])
            base_entry.configure(state="normal" if preset is None else "readonly")
            transcription_combo.configure(
                values=list(preset.transcription_models) if split and preset is not None else [])
            post_combo.configure(
                values=list(preset.post_process_models) if split and preset is not None else [])
            model_combo.configure(values=list(fetched_models.get(pid, ())))
            for row in (row_provider, row_variant, row_base, base_hint, row_key,
                        mimo_rows, model_row, status, buttons):
                row.pack_forget()
            row_provider.pack(fill=tk.X)
            if variants:
                row_variant.pack(fill=tk.X)
            row_base.pack(fill=tk.X)
            if preset is not None:
                base_hint.pack(anchor=tk.W, pady=(0, 10))
            row_key.pack(fill=tk.X)
            (mimo_rows if split else model_row).pack(fill=tk.X)
            status.pack(anchor=tk.W, pady=(0, 10))
            buttons.pack(fill=tk.X)

        def apply_provider_change(_event=None):
            pid = current_provider_id()
            variants = providers.variant_options(pid)
            if variants:
                # Reset to the preset default variant (stored variant applies at open only).
                default_id = providers.resolve_variant_id(pid)
                variant_var.set(next(
                    (v.label for v in variants if v.id == default_id), variants[0].label))
            preset = providers.get_provider(pid)
            # Model fields reset to registry defaults - another vendor's model
            # is never carried across; api_key is never touched here.
            model_var.set(preset.default_model if preset is not None else "")
            transcription_var.set(
                preset.default_transcription_model if preset is not None else "")
            # Presets re-resolve their registry URL; Custom keeps the URL currently shown.
            base_var.set(providers.resolve_base_url(pid, variant_var.get(), base_var.get()))
            set_row_visibility(pid)
            if fetch_state["token"] is None:
                fetch_status["text"] = fetch_status["color"] = None
            refresh_status()

        def apply_variant_change(_event=None):
            pid = current_provider_id()
            base_var.set(providers.resolve_base_url(pid, variant_var.get(), base_var.get()))
            if fetch_state["token"] is None:
                fetch_status["text"] = fetch_status["color"] = None
            refresh_status()

        def start_fetch():
            pid = current_provider_id()
            api_key = key_var.get().strip()
            if providers.needs_api_key(pid) and not api_key:
                # Presets only: skip a doomed round trip (Custom/Ollama never blocked).
                set_status_message(KEYLESS_FETCH_MESSAGE, COLORS["danger"])
                return
            variant_id = providers.resolve_variant_id(pid, variant_var.get())
            # Presets query the registry URL, never the field contents.
            base_url = providers.resolve_base_url(pid, variant_id, base_var.get().strip())
            fetch_btn.config(state=tk.DISABLED)
            set_status_message("Fetching models...", COLORS["muted"])
            fetch_state["token"] = pid  # single-flight + stale-result guard

            def worker():
                try:
                    models = fetch_models(base_url, api_key)
                except LLMError as error:
                    message = str(error)
                    self.call_in_ui(lambda: show_result(message, COLORS["danger"]))
                    return
                except Exception:
                    logger.exception("Unexpected error while fetching models")
                    self.call_in_ui(lambda: show_result(
                        "Unexpected error while fetching models.", COLORS["danger"]))
                    return

                def apply_models():
                    try:
                        if fetch_state["token"] != current_provider_id():
                            # The provider changed while fetching: discard + re-enable.
                            show_result(STALE_FETCH_MESSAGE, COLORS["muted"])
                            return
                        fetched_models[pid] = list(models)  # suggestions only
                        model_combo.configure(values=list(models))
                        if models and not model_var.get().strip():
                            model_var.set(models[0])
                        show_result(f"{len(models)} model(s) available.", COLORS["success"])
                    except tk.TclError:
                        pass  # the dialog was closed while the fetch was running

                self.call_in_ui(apply_models)

            threading.Thread(target=worker, daemon=True).start()

        def close_dialog(_event=None):
            dialog.destroy()
            self.settings_widgets = {}

        def save_settings():
            # Canonical read of whatever the widgets display; save_payload maps
            # labels -> ids, re-resolves preset URLs and enforces the R3 IFF rule.
            self.config.update(providers.save_payload({
                "provider": provider_var.get(),
                "provider_variant": variant_var.get(),
                "provider_base_url": base_var.get(),
                "api_key": key_var.get(),
                "model": model_var.get(),
                "transcription_model": transcription_var.get(),
            }))
            saved = save_config(self.config)
            close_dialog()
            if saved:
                self.set_status("Provider settings saved. They apply to the next recording.", "ready")
            else:
                self.set_status("Provider settings could not be saved to disk.", "error")

        cancel_btn = self.create_button(buttons, "Cancel", close_dialog, secondary=True)
        cancel_btn.pack(side=tk.RIGHT)
        save_btn = self.create_button(buttons, "Save", lambda: save_settings())
        save_btn.pack(side=tk.RIGHT, padx=(0, 8))

        provider_combo.bind("<<ComboboxSelected>>", lambda _e: apply_provider_change())
        variant_combo.bind("<<ComboboxSelected>>", lambda _e: apply_variant_change())
        # The muted key hint follows the field as it is typed into/cleared.
        key_var.trace_add("write", lambda *_args: refresh_status())

        set_row_visibility(state["provider"])   # initial layout for the hydrated pid
        refresh_status()
        self.settings_widgets = {
            "dialog": dialog, "panel": panel, "provider_combo": provider_combo,
            "variant_combo": variant_combo, "base_entry": base_entry,
            "key_entry": key_entry, "transcription_combo": transcription_combo,
            "post_combo": post_combo, "model_combo": model_combo, "fetch_btn": fetch_btn,
            "status": status, "variant_row": row_variant, "mimo_rows": mimo_rows,
            "model_row": model_row, "base_hint": base_hint, "buttons": buttons,
            "save_btn": save_btn, "cancel_btn": cancel_btn, "provider_var": provider_var,
            "variant_var": variant_var, "base_var": base_var, "key_var": key_var,
            "transcription_var": transcription_var, "model_var": model_var,
        }
        dialog.after_idle(provider_combo.focus_set)
        dialog.bind("<Escape>", lambda _event: close_dialog())
        dialog.grab_set()
