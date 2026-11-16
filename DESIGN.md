# Design System Baseline — WhisperLive (Adopted)

> Mode 0 Brownfield Adoption — T-2026-11-16-provider-presets. Reflects the existing Tkinter UI reality.

## 1. Visual Language (current)

- **Toolkit:** Tkinter + ttk; two themes defined in `themes.py` (`THEMES["dark"]`, `THEMES["light"]`) exposed as the shared live `COLORS` dict (theme toggle mutates it in place).
- **Typography:** Segoe UI (labels 9pt, headings bold); system default for inputs.
- **Surfaces:** flat `surface` frames with `border` highlight outlines; generous padding (26×22 dialogs); status line in `muted` color.
- **Accent semantics:** `success` (green), `danger` (red), `muted` (gray) for status feedback; primary buttons solid accent, secondary buttons outlined (widget factories `create_button`, `create_entry`, `add_label` in `views.py`).
- **Combobox style:** `Dark.TCombobox` ttk style shared across dialogs.

## 2. Interaction Patterns (current)

- **Settings dialog:** modal `Toplevel` (`transient`, `grab_set`, Escape closes, non-resizable); fields in vertical flow; masked API key + Show/Hide toggle; "Fetch Models" runs on a daemon thread with button-disable + status label, marshaled back via `call_in_ui`.
- **Main window:** compact always-available panel — Output language, Global shortcut (capture mode), Recording behavior (toggle/hold), theme switch, Settings button; status line for state feedback.
- **Feedback states:** `set_status(message, kind)` with `ready`/`error` kinds; beeps for start/stop/success (winsound on Windows).
- **Accessibility:** keyboard traversal via Tab (native Tk order), Escape-to-close on dialogs, `prefers_reduced_motion()` honored by the recording indicator (Windows SPI).

## 3. Anti-Slop Rules (existing craft, keep)

1. No decorative gradients/emoji/placeholder art — flat functional surfaces only.
2. Status text is specific and actionable ("Provider settings saved. They apply to the next recording."), never "Done!".
3. Errors surface in `danger` color with the real cause; background failures never vanish silently.
4. Secondary actions (Cancel/Show/Fetch) are outlined; the single primary action per dialog row is solid — one obvious primary per view.
5. Dense-but-breathable spacing rhythm: label → control (6–10 px), section gaps ≥ 10 px.

## 4. Shipped Delta (T-2026-11-16-provider-presets — implemented 2026-11-16)

- **Provider selector** (new first row of Settings): combobox of presets + "Custom (OpenAI-compatible URL)"; selecting a preset shows its base URL **read-only** (transparency), Custom re-enables the editable URL field (today's behavior).
- **MiMo endpoint/plan selector** (visible only for Xiaomi MiMo): `API (Pay-as-you-go)` / `Token Plan — Europe (AMS)` / `Token Plan — Singapore (SGP)` / `Token Plan — China (CN)`.
- **Split model rows for MiMo:** "Transcription model" (`mimo-v2.5-asr`) + "Post-processing model" (`mimo-v2.6-flash` default, `mimo-v2.6-pro`); a one-line `muted` hint explains that TTS models are excluded because the app never synthesizes speech, and that `mimo-v2.5-asr` officially supports zh/en (+ Chinese dialects) only. Non-MiMo keeps the single Model row + Fetch Models.
- **Preserved:** API-key masking, background fetch with disabled button + status line, Save/Cancel semantics, Escape, English UI copy.

## 5. Shipped Delta 2 (T-2026-11-16-dictation-modes — implemented 2026-11-17)

- **Settings is now a 3-tab Notebook** (Dictation / Engines & keys / Models) in the same shell: mode selector (Local / Chunked-VAD / Live) with honest one-line descriptions and per-mode side options; **red Gemini consent checkbox** (default OFF; paid-key bypass) with training disclosure; per-engine **quota countdown** rows; Models tab with download button + honest state copy (not-downloaded/downloading-paused/verifying/ready/failed/disk-space-error).
- **Session UX:** first-run guidance dialog (kind, one-time; never downloads on hotkey), focus-guard overlay (Resume / Copy / Discard; Escape=Resume), quota/no-silent-failover choice dialog, LIVE raw-output notice (no translation/cleanup — Polish/Translate is the escape hatch), status vocabulary mapped onto the 4-palette `set_status`.
- **Polish:** tray menu item "Polish last dictation"; safe insert + clipboard is the default; "replace in place" is explicitly experimental.
