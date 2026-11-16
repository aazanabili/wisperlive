# Stack & Patterns Matrix — WhisperLive (Adopted Baseline)

> Mode 0 Brownfield Adoption — T-2026-11-16-provider-presets.
> This document reflects the ACTUAL codebase reality (verified by inventory scan), not aspirations.

## 1. Product & Platforms

| Item | Reality |
| :--- | :--- |
| Product | WhisperLive — global-hotkey voice dictation: record → transcribe → clean/translate → paste as keystrokes |
| Primary platform | **Windows** (10/11) |
| Secondary platform | **Linux Desktop (X11/Xwayland only; Wayland-only NOT supported)** |
| Not targeted | macOS, Android, iOS, Web — no code present |
| Distribution | PyInstaller one-file (`WhisperLive.spec`); Windows `.exe`; Linux `deb`/`rpm`/`archlinux` (nfpm) + AppImage |

## 2. Languages, Runtimes & Toolchain

| Layer | Choice | Notes |
| :--- | :--- | :--- |
| Language | Python 3 (CI: 3.11; local dev venv: 3.14) | Single codebase, no polyglot packages |
| UI | Tkinter + ttk (`views.py`, `settings_view.py`, `themes.py`) | Mixin-composed `WhisperLiveApp` |
| HTTP client | stdlib `urllib` only (`llm_api.py`) | No SDKs; deliberate (portability + frozen builds) |
| Package manager | `pip` via `requirements.txt` / `requirements-build.txt` (pinned) | Runtime deps: keyboard, numpy, Pillow, pyperclip, pystray, python-xlib, sounddevice, soundfile |
| Build | PyInstaller 6.x (`WhisperLive.spec`), nfpm (Linux packages), appimagetool (AppImage) | CI: `.github/workflows/release.yml` (tag `v*`) |
| Tests | stdlib `unittest` + `unittest.mock`; `python -m unittest discover -v` | Root-level `test_*.py`; 150 tests incl. env-gated skips |

## 3. Architectural Style & Module Map

Hexagonal-light **mixin architecture** on a single desktop process (no server, no DB):

| Concern | Module(s) | Pattern |
| :--- | :--- | :--- |
| Composition root / state machine | `main.py` | `WhisperLiveApp(MainUiMixin, SettingsDialogMixin)`; UI-thread queue marshaling (`call_in_ui`) |
| UI (main window) | `views.py` (346 ln — at the <=350 gate) | Mixin + widget factories |
| UI (settings dialog) | `settings_view.py` (120 ln) | Second mixin (split to respect the <=350-line gate) |
| Provider API client | `llm_api.py` (374 ln — exceeds 350 gate; known debt) | Pure functions, stdlib urllib, bounded timeouts, typed error hierarchy (`LLMError` family) |
| Config persistence | `config_manager.py` | Atomic write (`os.replace` + fsync), DPAPI-protected `api_key` on Windows, legacy migration |
| Audio capture | `audio_recorder.py` | State machine IDLE→RECORDING→IDLE; lazy audio-libs seam |
| Output | `auto_typer.py` | Clipboard save/restore + paste keystroke |
| Global hotkeys | `windows_hotkey.py` / `linux_hotkey.py` | Platform strategy behind one interface; `parse_shortcut_parts` shared |
| Tray / indicator / theme | `tray.py`, `indicator.py`, `themes.py` | Tolerant-lazy tray; shared live `COLORS` map |
| Autostart / single instance / logging / updates | `startup.py`, `single_instance.py`, `app_logging.py`, `updater.py` | Registry (Win) / XDG `.desktop` (Linux); per-user lock file; rotating logs (no secrets/transcripts) |

**Dependency rule:** `llm_api` and `config_manager` never import UI; UI modules never speak HTTP; `main` wires everything. Enforced by tests (AST acyclicity guard in `test_views_startup_split.py`, `print` guard in `test_updater.py`).

## 4. Patterns Catalog (existing, enforced)

1. **Two-stage LLM pipeline** (`llm_api.py`): `transcribe_audio` (audio → raw) → `post_process_text` (cleanup + optional translation); `process_audio` composes both. No fallback from stage 2 to raw transcript (invariant).
2. **OpenAI-compatible REST dialect**: `GET /models` + `POST /chat/completions`; any compatible endpoint works (custom URL model today).
3. **Bounded I/O**: explicit timeouts (`MODELS_TIMEOUT=15`, `GENERATION_TIMEOUT=120`), `MAX_RESPONSE_BYTES=16 MiB`, `MAX_AUDIO_BYTES=20 MiB` raw, total-deadline body reads; NO auto-retry on billable completions (H6); exactly ONE retry on idempotent `GET /models`.
4. **Optional API key**: keyless local providers (Ollama) supported; `Authorization` omitted when empty.
5. **Secret hygiene**: `api_key` DPAPI-at-rest (`dpapi:` prefix) / opaque `base64:` fallback off-Windows; `_safe_url_for_log` strips userinfo/query/fragment; logs never contain transcripts/prompts/audio/keys.
6. **Atomic config writes** with legacy plaintext-key rewrite-on-load and legacy-path migration.
7. **UI thread discipline**: background worker threads marshal results via `call_in_ui` queue; `tk.TclError` swallowed for late callbacks (dialog-closed race).
8. **Single temp-file ownership** for recordings; caller deletes on success, recorder on failure.
9. **Intention-revealing hazard comments** (`H1..H6`, `M2`) documenting invariants.

## 5. Known Debt / Constraints (honest)

- `llm_api.py` was 374 ln (> 350 gate) — **RESOLVED** 2026-11-16 (T-2026-11-16-provider-presets): pure wire-format code extracted to `llm_payloads.py` (157 ln); `llm_api.py` now 349 ln. All modules ≤350 (providers.py 349, settings_view.py 335, main.py 337, config_manager.py 269).
- Linux API-key at-rest protection is obfuscation (`base64:`), not encryption (no stdlib DPAPI equivalent) — documented, accepted.
- `app_logging` log-dir fallback is Windows-shaped on Linux (pre-existing cosmetic issue).
- Wayland-only Linux sessions unsupported (X11 required for hotkeys/paste).
- UI language is English-only (prompts sent to providers are Arabic by design of the transcript cleanup stage).

## 6. Provider Presets Feature (SHIPPED 2026-11-16 — T-2026-11-16-provider-presets)

Provider presets + Custom mode are implemented and verified (QA 250/250, SecOps `PASS_WITH_RESIDUAL_RISKS`, 0 findings): pure `providers.py` registry (9 presets incl. Xiaomi MiMo with 4 endpoint variants), dialect-aware request building in `llm_api.py` + `llm_payloads.py` (MiMo ASR wire shape per vendor docs), config keys `provider` / `provider_variant` / `transcription_model` (backward compatible, rules M1–M12), and the revised settings dialog. See `.orchestrator/plans/feature-provider-presets.md` for the full record.
