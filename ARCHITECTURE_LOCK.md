# ARCHITECTURE LOCK — WhisperLive

- **STATUS:** `LOCKED (ADOPTED)` — Mode 0 Brownfield Adoption
- **Locked at:** 2026-11-16 (Task T-2026-11-16-provider-presets; user approval D4 recorded in `.orchestrator/plans/feature-provider-presets.md` §1.2)
- **Basis:** Verified repository inventory (`@explore` scan) synthesized into the three companion documents below. This lock reflects **actual codebase reality**, not aspirations.

## 0. Locked Companion Documents

| Document | Scope |
| :--- | :--- |
| `STACK_AND_PATTERNS_MATRIX.md` | Languages, frameworks, module map, enforced patterns, known debt |
| `DATA_TOPOLOGY.md` | Persistence inventory, config schema, outbound data flows, privacy policy |
| `DESIGN.md` | Visual language, interaction patterns, anti-slop rules |

Any change to the stack, data topology, or design system requires an orchestrator-led plan revision (specialist consultation per risk tier) — never a silent drift.

## 1. Locked Architecture Summary

- **Product:** Desktop voice dictation (record → transcribe → clean/translate → paste) for **Windows (primary)** and **Linux X11 (secondary)**. No server, no database, no web/mobile clients.
- **Stack:** Python 3 (CI 3.11), Tkinter + ttk UI, stdlib `urllib` provider client (NO SDKs), pip-pinned deps (`requirements.txt`), PyInstaller + nfpm/AppImage packaging, `unittest` suite (`python -m unittest discover -v`).
- **Structure:** Mixin-composed `WhisperLiveApp` (`main.py` + `views.py` + `settings_view.py`); pure domain modules (`llm_api.py`, `llm_payloads.py`, `providers.py`, `config_manager.py`); platform adapters (`windows_hotkey.py` / `linux_hotkey.py`, `startup.py`, `single_instance.py`); peripherals (`audio_recorder.py`, `auto_typer.py`, `tray.py`, `indicator.py`).
- **Dependency rule (acyclic, enforced by tests):** pure modules never import UI/transport; UI never speaks HTTP; `providers.py` and `llm_payloads.py` are stdlib-only and I/O-free.

## 2. Locked Invariants (non-negotiable)

1. **Two-stage pipeline:** `transcribe_audio` → `post_process_text`; stage-2 failure never falls back to the raw transcript.
2. **Wire policy:** no auto-retry on billable completions; ONE bounded retry on idempotent `GET /models`; explicit timeouts and response/audio size bounds; optional (keyless) API keys supported.
3. **Secrets:** `api_key` DPAPI-protected at rest on Windows (`dpapi:` prefix, never downgraded to plaintext); opaque `base64:` fallback elsewhere (documented limitation); never logged; URLs logged with userinfo/query/fragment stripped.
4. **Config:** atomic writes; legacy migration; unknown keys round-trip untouched; pre-preset configs behave byte-identically (migration rules M1–M12 in the feature plan).
5. **UI thread discipline:** background work marshaled via `call_in_ui`; bounded dialog state machines.
6. **Module budget:** ≤ 350 lines per module (review signal; exceedances must be flagged and extracted, e.g. `llm_payloads.py`).
7. **Dependency hygiene:** no new third-party runtime deps without negotiation; 7-day release-age cooldown; `pip` + pinned lockfiles only.
8. **Test economy:** Tier 0–4 scaled; suite must stay green (`python -m unittest discover -v`); tests owned exclusively by `qa-test-assurance`; production code by `worker-executor` (single-writer factory governance).

## 3. Locked Provider Surface (feature baseline after T-2026-11-16 execution)

- Presets: `xiaomi-mimo`, `openai`, `google-gemini`, `deepseek`, `groq`, `mistral`, `openrouter`, `ollama-local`, plus `custom` (free URL). Canonical URLs in `providers.py` (registry is the single source of truth).
- Xiaomi MiMo: 4 endpoint variants (`api` / `token-plan-ams` / `token-plan-sgp` / `token-plan-cn`); split models (`mimo-v2.5-asr` transcription + `mimo-v2.6-flash`/`mimo-v2.6-pro` post-processing); TTS models excluded by design; MiMo ASR wire shape follows vendor documentation exactly.
- Known vendor limitations (documented, honest): `mimo-v2.5-asr` supports zh/en (+ Chinese dialects) only; Token Plan ToS targets coding tools.

## 4. Change Control

Production changes follow the factory pipeline: specialist deliberation (read-only) → user escalation on ambiguity/dispute → phased plan in `.orchestrator/plans/` → `worker-executor` single-writer execution → QA + SecOps gates → Content-Based Git Acceptance Gate. Renaming this file or reverting `STATUS` to `DRAFT` requires explicit user authorization.
