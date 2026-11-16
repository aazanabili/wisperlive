"""Dictation tab + shared settings helpers/copy (A5A7 s1.3; R-6 split home).

WHY this module: the Dictation-tab builder plus the pure helpers/copy shared
by the other tab modules (focus ring, option radio, consent/validation copy,
state/reset copy) — engines extras live in ``settings_keys_view``, models in
``settings_models_view``, provider rows in ``settings_provider_view``, and the
shell in ``settings_view`` (annex s4 module budget).  Registry facts (labels/
URLs) come from ``providers`` helpers only; widgets map back to flat A3 keys
through payload callables (the shell is the sole config writer, R5).

Copy strings are the exact A5A7 table values (anti-slop); new interactive
widgets carry the visible focus ring; consent uses the danger token (>= 4.5:1
in BOTH themes); colors read the LIVE ``COLORS`` map (theme-safe at open).
"""

import importlib.util
import tkinter as tk

import config_manager
import providers
from llm_api import NO_TRANSLATION
from themes import COLORS

# Exact A5A7 copy (registry knowledge stays in providers — ids only here).
LIVE_RAW_NOTICE = ("Live output is raw: no translation or cleanup while you speak. "
                   "\u201c{}\u201d applies only through \u201cPolish last dictation\u201d after you stop.")
LOCAL_MISSING_NOTE = ("On-device dictation is hidden: the optional speech packages are not "
                      "installed. Saving switches to Segmented (Chunked).")
GEMINI_REASON_COPY = "Gemini Live is off \u2014 accept the notice in Settings \u2192 Engines & keys."
LIVE_CAP_NOTE = "Live needs the Fast model on this computer \u2014 current size: {}."
CONSENT_COPY = "I understand: cloud processing \u2014 Google may use my data for training."
PAID_KEY_COPY = "I confirm this Google API key is a paid Google Cloud key."
PAID_NOTE = "Paid key confirmed \u2014 the free-tier notice does not apply."
VALIDATION_GEMINI = "Accept the Gemini notice before selecting Gemini Live."
GEMINI_VENDOR = "google-gemini"     # registry id (providers) for the consent-gated engine
STORE_ABSENT_REASON = "Downloads open when the model store is available in this build."

# Engines render enabled-with-hints; dynamic gates (consent) live in _refresh_gates (R6).
ENGINE_AVAILABILITY = {
    "local": ("On-device (Local)", True, ""),
    "gemini-live": ("Google Gemini Live", True, ""),
    "openai-realtime": ("OpenAI Realtime", True, ""),
    "ollama": ("Local with Ollama", True, ""),
}

# A5A7 s1.5 honest state vocabulary (facts optional: unknown size is omitted,
# never invented — the manifest pins MB numbers in Phase 5).
DOWNLOAD_STATE_COPY = {
    "not_downloaded": "Not downloaded yet \u2014 {size} model.",
    "ready": "{size} model verified and installed.",
    "failed": "Download failed. Nothing was installed.",
}


def download_state_copy(state, **facts):
    """Pure state -> label copy (never raises; unknown state stays honest)."""
    template = DOWNLOAD_STATE_COPY.get(state)
    if template is None:
        return "Model state is unknown."
    try:
        return template.format(size=facts.get("size", "selected"))
    except (KeyError, IndexError, ValueError):
        return "Model state is unknown."


def format_reset_from_hint(now_epoch, hint):
    """'3 h 12 m' style countdown from a local 'HH:MM' reset hint (D6 display).
    Unknown/past hint -> '' (honest blank, never a fabricated time)."""
    if not isinstance(hint, str) or ":" not in hint:
        return ""
    try:
        from datetime import datetime
        hour, minute = (int(part) for part in hint.split(":", 1))
        target = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
        delta = int(target.timestamp()) - int(now_epoch)
        if delta <= 0:
            return ""                        # past hint: reset not computed here (vendor owns it)
        days, rem = divmod(delta, 86400)
        hours, rem = divmod(rem, 3600)
        minutes = (rem + 59) // 60
        if days:
            return f"{days} d {hours} h"
        if hours:
            return f"{hours} h {minutes} m"
        return f"{max(1, minutes)} m"
    except (ValueError, TypeError, OSError):
        return ""


def validate_save(dictation_values, keys_values) -> str:
    """Save-blocker (A5A7 3.1 - the ONE hard rule): Gemini Live needs consent
    OR the paid-key attestation (U2 fail-closed).  '' = save may run."""
    if dictation_values.get("live_engine") != "gemini-live":
        return ""
    if keys_values.get("gemini_consent") or keys_values.get("gemini_paid_key_confirmed"):
        return ""
    return VALIDATION_GEMINI


def local_deps_available() -> bool:
    """D8 cheap check: find_spec only — never a heavy import on the UI thread."""
    try:
        return importlib.util.find_spec("faster_whisper") is not None
    except (ImportError, ValueError):
        return False


def focus_ring(widget):
    """Visible focus ring (A5A7 1.6) — tokens read live for both themes."""
    widget.configure(highlightthickness=1, highlightbackground=COLORS["border"],
                     highlightcolor=COLORS["accent"], takefocus=True)
    return widget


def option_radio(parent, text, variable, value, command=None):
    return focus_ring(tk.Radiobutton(
        parent, text=text, value=value, variable=variable, command=command,
        bg=COLORS["surface"], fg=COLORS["text"], selectcolor=COLORS["surface_hover"],
        activebackground=COLORS["surface"], activeforeground=COLORS["text"]))


class SettingsDictationMixin:
    """Builders for the notebook tabs; ``self`` is the host (never imports main)."""

    # ---------------- shell support: save payloads + validation -------------
    def apply_settings_payloads(self, payloads, notebook, engines_panel) -> str:
        """Run the ONE save validator FIRST (a blocked save must NOT touch
        config), then merge tab payloads (provider LAST - R3 IFF).  '' = the
        save may proceed; otherwise the blocker copy renders and non-empty."""
        dictation_values, keys_values, models_values = payloads
        issue = validate_save(dictation_values, keys_values)
        if issue:
            self.settings_widgets["validation_label"].config(text=issue)
            notebook.select(engines_panel)
            self.settings_widgets["consent_check"].focus_set()
            self.set_status(issue, "error")
            return issue
        for vendor, value in keys_values.pop("_vendor_keys", {}).items():
            config_manager.set_vendor_key(self.config, vendor, value)
        self.config.update({**dictation_values, **keys_values, **models_values})
        return ""

    # ---------------- Tab A: Dictation --------------------------------------
    def build_dictation_tab(self, panel, config, notebook, widgets):
        """Mode radios + matrix + side options + ONE polish block; returns the save payload callable."""
        c = COLORS
        mode_var = tk.StringVar(value=config.get("dictation_mode", "local"))
        if mode_var.get() not in ("local", "chunked", "live"):
            mode_var.set("local")
        size_var = tk.StringVar(value=config.get("model_size", "auto"))
        cleanup_var = tk.StringVar(value=config.get("cleanup_mode", "cloud"))
        chunked_var = tk.StringVar(value=config.get("chunked_engine", ""))
        live_var = tk.StringVar(value=config.get("live_engine", "local"))
        polish_var = tk.StringVar(value=config.get("polish_mode", "insert"))

        def visibility(*_args):
            self._dictation_visibility()

        self.add_label(panel, "Dictation mode")
        mode_rows = tk.Frame(panel, bg=c["surface"])
        mode_rows.pack(fill=tk.X, pady=(0, 6))
        descriptions = {
            "local": "Recognizes on this computer; nothing is sent to transcribe your speech.",
            "chunked": "Sends short segments while you speak; text appears before you stop.",
            "live": "Types each confirmed phrase as you speak.",
        }
        mode_buttons = {}
        for value, text in (("local", "On-device (Local)"),
                            ("chunked", "Segmented (Chunked)"),
                            ("live", "Streaming (Live)")):
            row = tk.Frame(mode_rows, bg=c["surface"])
            row.pack(fill=tk.X)
            option_radio(row, text, mode_var, value, command=visibility).pack(side=tk.LEFT, anchor=tk.W)
            tk.Label(row, text=descriptions[value], bg=c["surface"], fg=c["muted"],
                     font=("Segoe UI", 9), justify=tk.LEFT, wraplength=400).pack(
                side=tk.LEFT, padx=(10, 0))
            mode_buttons[value] = row
        deps_missing = not local_deps_available()          # D8 honesty
        local_missing = tk.Label(panel, text=LOCAL_MISSING_NOTE, bg=c["surface"],
                                 fg=c["warning"], font=("Segoe UI", 9),
                                 justify=tk.LEFT, wraplength=400)
        if deps_missing and mode_var.get() == "local":
            mode_var.set("chunked")            # hydrate-time fallback (never silent)

        live_notice = tk.Frame(panel, bg=c["warning_bg"], highlightbackground=c["warning"],
                               highlightthickness=1, padx=10, pady=8)
        live_text = tk.Label(live_notice, bg=c["warning_bg"], fg=c["warning"],
                             font=("Segoe UI", 9), justify=tk.LEFT, wraplength=400)
        self.add_label(panel, "Side options for the selected mode")
        side = tk.Frame(panel, bg=c["surface"])
        side.pack(fill=tk.X, pady=(0, 6))

        def add_cleanup(parent):
            self.add_label(parent, "Cleanup")
            for value, text in (("cloud", "Cloud (includes translation)"),
                                ("local-ollama", "Local with Ollama"),
                                ("off", "Off \u2014 keep the raw transcript")):
                row = tk.Frame(parent, bg=c["surface"])
                row.pack(fill=tk.X)
                radio = option_radio(row, text, cleanup_var, value)
                if value == "local-ollama":                # Phase 8: enabled (presence-checked at use)
                    tk.Label(row, text="Runs locally \u2014 Ollama must be running.", bg=c["surface"],
                             fg=c["muted"], font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(10, 0))
                radio.pack(side=tk.LEFT, anchor=tk.W)
            tk.Label(parent, text="Sent to your provider for cleanup and translation.",
                     bg=c["surface"], fg=c["muted"], font=("Segoe UI", 9),
                     wraplength=400).pack(anchor=tk.W, pady=(2, 8))

        opts_local = tk.Frame(side, bg=c["surface"])
        add_cleanup(opts_local)
        self.add_label(opts_local, "Model size")
        size_echo = tk.Label(opts_local, bg=c["surface"], fg=c["muted"],
                             font=("Segoe UI", 9), justify=tk.LEFT, wraplength=400)

        opts_chunked = tk.Frame(side, bg=c["surface"])
        self.add_label(opts_chunked, "Engine")
        current = providers.resolve_provider_id(config.get("provider"))
        current_label = (providers.get_provider(current).label
                         if providers.get_provider(current) is not None
                         else providers.provider_choices()[-1][1])
        for value, text in (("", f"Current provider ({current_label})"),
                            ("groq-whisper", "Groq Whisper")):
            option_radio(opts_chunked, text, chunked_var, value).pack(anchor=tk.W)
        tk.Label(opts_chunked,
                 text="Groq Whisper uses your Groq key in Settings \u2192 Engines & keys.",
                 bg=c["surface"], fg=c["muted"], font=("Segoe UI", 9),
                 wraplength=400).pack(anchor=tk.W, pady=(2, 6))
        add_cleanup(opts_chunked)

        opts_live = tk.Frame(side, bg=c["surface"])
        self.add_label(opts_live, "Engine")
        live_radios = {}
        for value in ("local", "gemini-live", "openai-realtime"):
            label, available, reason = ENGINE_AVAILABILITY[value]
            row = tk.Frame(opts_live, bg=c["surface"])
            row.pack(fill=tk.X)
            radio = option_radio(row, label, live_var, value, command=visibility)
            if not available:
                radio.configure(state=tk.DISABLED, fg=c["muted"])
                tk.Label(row, text=reason, bg=c["surface"], fg=c["muted"],
                         font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(10, 0))
            elif value == "openai-realtime":            # Phase 7 shipped — honest key hint
                tk.Label(row, text="Needs an OpenAI API key.", bg=c["surface"], fg=c["muted"],
                         font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(10, 0))
            radio.pack(side=tk.LEFT, anchor=tk.W)
            live_radios[value] = radio
        live_engine_note = tk.Label(opts_live, bg=c["surface"], fg=c["danger"],
                                    font=("Segoe UI", 9), justify=tk.LEFT, wraplength=400)
        live_cap_note = tk.Label(opts_live, bg=c["surface"], fg=c["muted"],
                                 font=("Segoe UI", 9), wraplength=400)
        tk.Label(opts_live, text="Live output is raw: cleanup runs only through "
                 "\u201cPolish last dictation.\u201d", bg=c["surface"], fg=c["muted"],
                 font=("Segoe UI", 9), wraplength=400).pack(anchor=tk.W, pady=(2, 6))

        # ONE Polish block (A5A7 R2.2: one flat key -> one visible group).
        self.add_label(panel, "Polish")
        polish_frame = tk.Frame(panel, bg=c["surface"])
        polish_frame.pack(fill=tk.X, pady=(0, 6))
        for value, text in (("insert", "Safe insert \u2014 paste the result and keep a clipboard copy"),
                            ("replace-experimental", "Experimental \u2014 replace the text I just dictated")):
            option_radio(polish_frame, text, polish_var, value).pack(anchor=tk.W)
        tk.Label(polish_frame, text="Experimental replace selects the text just typed and "
                 "rewrites it. If the app loses focus during the swap, the clipboard copy "
                 "is your fallback.", bg=c["surface"], fg=c["muted"],
                 font=("Segoe UI", 9), wraplength=400).pack(anchor=tk.W, pady=(2, 0))
        validation = tk.Label(panel, text="", bg=c["surface"], fg=c["danger"],
                              font=("Segoe UI", 9), wraplength=400)

        self._dictation_state = {
            "mode_var": mode_var, "size_var": size_var, "cleanup_var": cleanup_var,
            "chunked_var": chunked_var, "live_var": live_var, "polish_var": polish_var,
            "rows": mode_buttons, "local_missing": local_missing, "deps_missing": deps_missing,
            "notice": (live_notice, live_text), "opts": {"local": opts_local,
            "chunked": opts_chunked, "live": opts_live}, "size_echo": size_echo,
            "live_radios": live_radios, "live_engine_note": live_engine_note,
            "live_cap_note": live_cap_note, "validation": validation,
        }
        widgets.update({
            "mode_var": mode_var, "model_size_echo_var": size_var, "cleanup_var": cleanup_var,
            "chunked_engine_var": chunked_var, "live_engine_var": live_var,
            "polish_var": polish_var, "mode_local": mode_buttons["local"],
            "validation_label": validation,
        })
        visibility()
        return lambda: {
            "dictation_mode": mode_var.get(), "cleanup_mode": cleanup_var.get(),
            "chunked_engine": chunked_var.get(), "live_engine": live_var.get(),
            "polish_mode": polish_var.get(),
        }

    def _dictation_visibility(self):
        """A5A7 visibility matrix: rows created once, toggled in canonical pack
        order (widget identity + Tab order never change across mode switches)."""
        state = getattr(self, "_dictation_state", None)
        if state is None:
            return
        widgets = getattr(self, "settings_widgets", {})
        mode = state["mode_var"].get()
        for value, row in state["rows"].items():
            if (value == "local" and state["deps_missing"]) or (value != mode):
                row.pack_forget()
            else:
                row.pack(fill=tk.X)
        # D8 note: shown whenever the Local row is hidden (never silent).
        (state["local_missing"].pack(anchor=tk.W, pady=(0, 6))
         if state["deps_missing"] else state["local_missing"].pack_forget())
        for frame in state["opts"].values():
            frame.pack_forget()
        state["opts"].get(mode, state["opts"]["local"]).pack(fill=tk.X)
        notice, notice_text = state["notice"]
        if mode == "live":
            lang = getattr(self, "lang_var", None)          # unsaved main-window choice counts
            target = lang.get() if lang is not None else getattr(
                self, "config", {}).get("target_language", NO_TRANSLATION)
            if target != NO_TRANSLATION:                     # D2 raw-output notice
                notice_text.config(text=LIVE_RAW_NOTICE.format(target))
                notice.pack(fill=tk.X, pady=(0, 8))
            else:
                notice.pack_forget()
        else:
            notice.pack_forget()
        size = (widgets.get("model_size_var") or state["size_var"]).get()
        state["size_echo"].config(text=f"Model size: {size} \u2014 change in Settings \u2192 Models.")
        echo_on = mode == "local" or (mode == "live" and state["live_var"].get() == "local")
        (state["size_echo"].pack(anchor=tk.W, pady=(0, 6))
         if echo_on else state["size_echo"].pack_forget())
        # U2 gate reads the LIVE checkbox vars (cross-tab), falling back to config.
        consent = widgets.get("consent_var")
        paid = widgets.get("paid_var")
        gemini_ok = bool(consent.get() if consent is not None
                         else getattr(self, "config", {}).get("gemini_consent"))
        gemini_ok = gemini_ok or bool(
            paid.get() if paid is not None
            else getattr(self, "config", {}).get("gemini_paid_key_confirmed"))
        state["live_radios"]["gemini-live"].configure(
            state=tk.NORMAL if gemini_ok else tk.DISABLED)
        if not gemini_ok:
            if state["live_var"].get() == "gemini-live":
                state["live_var"].set("local")              # fail closed while open
            state["live_engine_note"].config(text=GEMINI_REASON_COPY)
            state["live_engine_note"].pack(anchor=tk.W, pady=(0, 4))
        else:
            state["live_engine_note"].pack_forget()
        cap_on = mode == "live" and size == "standard"       # informational (R2.4: not a blocker)
        state["live_cap_note"].config(text=LIVE_CAP_NOTE.format(size))
        (state["live_cap_note"].pack(anchor=tk.W, pady=(0, 4))
         if cap_on else state["live_cap_note"].pack_forget())
        state["validation"].pack(anchor=tk.W, pady=(4, 0)) if state["validation"].cget(
            "text") else state["validation"].pack_forget()
