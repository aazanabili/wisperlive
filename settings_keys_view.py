"""Engines & keys tab extras: speech-key rows, U2 consent, quota countdown (A5A7 s4).

WHY its own module (R-6 Option A / annex s4 budget): the provider rows live in
``settings_provider_view``, the Dictation tab + shared copy/helpers in
``settings_dictation_view``; this module holds the tab's NEW content — K7
vendor key rows (DPAPI map slots via ``config_manager.get/set_vendor_key``),
the RED consent + paid-key attestation (U2 fail-closed), and the D6 quota
countdown rows (proven exhaustion from session notices; budgeted windows from
the public ``quota_snapshot`` accessor — no ledger internals reach the UI).
"""

import time
import tkinter as tk

import config_manager
import providers
from settings_dictation_view import (CONSENT_COPY, GEMINI_VENDOR, PAID_KEY_COPY,
                                     PAID_NOTE, focus_ring, format_reset_from_hint)
from themes import COLORS


def _engine_label(engine_id: str) -> str:
    """Human engine label from the providers registry (no duplicated names)."""
    if engine_id == "groq-whisper":
        return "Groq Whisper"
    preset = providers.get_provider(engine_id.split(":", 1)[-1])
    return preset.label if preset is not None else engine_id


def _rel_from_epoch(now_epoch: int, reset_at: int) -> str:
    """'3 h 12 m' countdown from a reset EPOCH (D6 display); past/unknown -> ''."""
    delta = int(reset_at) - int(now_epoch)
    if delta <= 0:
        return ""
    days, rem = divmod(delta, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = (rem + 59) // 60
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {minutes} m"
    return f"{max(1, minutes)} m"


class SettingsKeysMixin:
    """Builders for the Engines & keys tab extras (self = the host app)."""

    def build_engines_extras(self, panel, config, widgets):
        """Vendor key rows (K7 map slots), U2 consent/attestation (RED), quota
        countdown rows.  Returns ``(extras_frame, payload)`` — the shell packs
        extras at the panel bottom through the provider row's canonical layout."""
        c = COLORS
        extras = tk.Frame(panel, bg=c["surface"])
        tk.Frame(extras, height=1, bg=c["border"]).pack(fill=tk.X, pady=(8, 8))
        self.add_label(extras, "Speech engine keys")
        key_vars = {}

        def vendor_row(vendor, label, hint, value):
            row = tk.Frame(extras, bg=c["surface"])
            row.pack(fill=tk.X, pady=(0, 6))
            self.add_label(row, label)
            entry_row = tk.Frame(row, bg=c["surface"])
            entry_row.pack(fill=tk.X)
            var = tk.StringVar(value=value)
            entry = self.create_entry(entry_row, var, show="*")
            entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
            button = {"node": None}

            def toggle():
                node = button["node"]
                if entry.cget("show") == "*":
                    entry.config(show=""); node.config(text="Hide")
                else:
                    entry.config(show="*"); node.config(text="Show")

            button["node"] = self.create_button(entry_row, "Show", toggle, secondary=True)
            button["node"].pack(side=tk.LEFT, padx=(8, 0))
            tk.Label(row, text=hint, bg=c["surface"], fg=c["muted"],
                     font=("Segoe UI", 9), wraplength=400).pack(anchor=tk.W)
            key_vars[vendor] = var

        consent_var = tk.BooleanVar(value=bool(config.get("gemini_consent")))
        paid_var = tk.BooleanVar(value=bool(config.get("gemini_paid_key_confirmed")))
        vendor_row(GEMINI_VENDOR, "Google Gemini key",
                   "Used by Gemini Live. Stored encrypted on this computer.",
                   config_manager.get_vendor_key(config, GEMINI_VENDOR))
        # U2: RED consent gates the free tier; paid attestation is the A3 branch.
        consent = focus_ring(tk.Checkbutton(
            extras, text=CONSENT_COPY, variable=consent_var, fg=c["danger"],
            bg=c["surface"], selectcolor=c["surface_hover"], activebackground=c["surface"],
            activeforeground=c["danger"], justify=tk.LEFT, command=self._refresh_gates))
        consent.pack(anchor=tk.W, padx=(14, 0))
        tk.Label(extras, text="Required for Gemini engines when using a free-tier key.",
                 bg=c["surface"], fg=c["muted"], font=("Segoe UI", 9),
                 wraplength=400).pack(anchor=tk.W, padx=(14, 0), pady=(0, 4))
        paid = focus_ring(tk.Checkbutton(
            extras, text=PAID_KEY_COPY, variable=paid_var, fg=c["muted"],
            bg=c["surface"], selectcolor=c["surface_hover"], activebackground=c["surface"],
            command=self._refresh_gates))
        paid.pack(anchor=tk.W, padx=(14, 0), pady=(0, 4))
        paid_note = tk.Label(extras, text="", bg=c["surface"], fg=c["muted"],
                             font=("Segoe UI", 9), wraplength=400)
        paid_note.pack(anchor=tk.W, padx=(14, 0))
        provider_id = providers.resolve_provider_id(config.get("provider"))
        vendor_row("groq", "Groq key",
                   ("Provider is Groq \u2014 your provider key is used if this row is empty."
                    if provider_id == "groq" else
                    "Used by the Groq Whisper chunked engine."),
                   config_manager.get_vendor_key(config, "groq"))
        vendor_row("openai", "OpenAI key (Realtime)",
                   "Used by OpenAI Realtime when it ships.",
                   config_manager.get_vendor_key(config, "openai"))
        tk.Frame(extras, height=1, bg=c["border"]).pack(fill=tk.X, pady=(8, 8))
        self.add_label(extras, "Quota")
        quota = tk.Frame(extras, bg=c["surface"])
        quota.pack(fill=tk.X)
        self._quota_target, self._consent_check = quota, consent
        self._refresh_gates()
        self._refresh_quota_rows()
        widgets.update({"consent_check": consent, "paid_check": paid,
                        "consent_var": consent_var, "paid_var": paid_var,
                        "vendor_key_vars": key_vars, "engines_extras": extras,
                        "paid_note": paid_note})
        return extras, lambda: {
            "gemini_consent": bool(consent_var.get()),
            "gemini_paid_key_confirmed": bool(paid_var.get()),
            "_vendor_keys": {vendor: var.get() for vendor, var in key_vars.items()},
        }

    def _refresh_gates(self):
        """U2 live gates: paid note shows when attested; the Dictation tab's
        Gemini radio re-evaluates against the LIVE checkbox state."""
        widgets = getattr(self, "settings_widgets", {})
        note, paid = widgets.get("paid_note"), widgets.get("paid_var")
        if note is not None and paid is not None:
            note.config(text=PAID_NOTE if paid.get() else "")
        self._dictation_visibility()

    def _refresh_quota_rows(self):
        """D6 countdown rows: PROVEN exhaustion from session notices (0% left)
        plus budgeted engines from the public ``quota_snapshot`` (window
        ``limit >= 0`` only — unknown budget stays the exact empty-state copy)."""
        quota = getattr(self, "_quota_target", None)
        if quota is None:
            return
        for child in quota.winfo_children():
            child.destroy()
        session = getattr(self, "dictation", None)
        notices = [n for n in getattr(session, "notices", ())
                   if isinstance(n, dict) and n.get("kind") == "quota_exhausted"]
        snapshot = getattr(session, "quota_snapshot", ()) or ()
        rows = []                                            # (label, pct, rel)
        for notice in notices:
            engine_id = notice.get("engine_id", "")
            rows.append((_engine_label(engine_id), 0,
                         format_reset_from_hint(int(time.time()),
                                                notice.get("reset_hint", ""))))
        seen = {label for label, _pct, _rel in rows}
        now = int(time.time())
        for engine_id, windows in snapshot:
            label = _engine_label(engine_id)
            if label in seen:
                continue
            budgets = [(used, limit, reset_at) for _wid, used, limit, reset_at in windows
                       if isinstance(limit, int) and limit >= 0]
            if not budgets:
                continue                          # unknown budget: never fabricated
            pct = max(0, min(100, round(
                100 * min((limit - used) / limit for used, limit, _r in budgets if limit))))
            rel = next((_rel_from_epoch(now, r) for _u, _l, r in budgets
                        if r and _rel_from_epoch(now, r)), "")
            rows.append((label, pct, rel))
        if not rows:
            tk.Label(quota, text="No usage recorded yet.", bg=COLORS["surface"],
                     fg=COLORS["muted"], font=("Segoe UI", 9)).pack(anchor=tk.W)
            return
        for label, pct, rel in rows:
            row = tk.Frame(quota, bg=COLORS["surface"])
            row.pack(fill=tk.X)
            tk.Label(row, text=f"{label} \u2014 {pct}% left", bg=COLORS["surface"],
                     fg=COLORS["text"], font=("Segoe UI", 9)).pack(side=tk.LEFT)
            if rel:
                tk.Label(row, text=f"resets in {rel}", bg=COLORS["surface"],
                         fg=COLORS["muted"], font=("Segoe UI", 9)).pack(side=tk.RIGHT)
