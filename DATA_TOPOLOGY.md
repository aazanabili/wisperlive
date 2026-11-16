# Data Topology — WhisperLive (Adopted Baseline)

> Mode 0 Brownfield Adoption — T-2026-11-16-provider-presets. Reflects actual persistence reality.

## 1. Persistence Inventory (no database, no server state)

| Store | Location | Content | Protection |
| :--- | :--- | :--- | :--- |
| User config | `%LOCALAPPDATA%\WhisperLive\config.json` (Windows) / `$XDG_CONFIG_HOME/WhisperLive/config.json` (Linux) | App settings incl. provider endpoint + model | `api_key` at rest: Windows DPAPI user-scope (`dpapi:` b64) / non-Windows opaque `base64:`; file atomic write (`.tmp` + `os.replace` + fsync) |
| Legacy config (read-only migration source) | exe dir / source dir / cwd `config.json` | Older installs | Migrated once via `_atomic_write(no_overwrite=True)`; plaintext `api_key` rewritten on load |
| Logs | `%LOCALAPPDATA%\WhisperLive\logs` (fallback `~/AppData/Local/...`) | Rotating `app.log` (1 MiB × 3) | No secrets/transcripts/audio/paths-with-PII policy |
| Recording temp | system temp `temp_audio.wav`-style single temp file | Raw WAV between record and process | Single-owner deletion rule (caller success / recorder failure) |
| Single-instance lock | per-user temp (Linux prefers `$XDG_RUNTIME_DIR`) | lock file | `msvcrt.locking` (Win) / `fcntl.flock` (POSIX) |
| Autostart (OS-level) | HKCU Run key (Win) / `$XDG_CONFIG_HOME/autostart/*.desktop` (Linux) | launch command | OS user scope |

## 2. Config Schema (authoritative keys — `config_manager.DEFAULT_CONFIG`)

| Key | Type | Default | Semantics |
| :--- | :--- | :--- | :--- |
| `api_key` | str (in-memory plaintext) | `""` | Optional; stored as `api_key_protected` on disk |
| `provider_base_url` | str | `""` | OpenAI-compatible base URL, e.g. `https://api.xiaomimimo.com/v1` |
| `model` | str | `""` | Model id used by the pipeline |
| `target_language` | str | `"English"` | Stage-2 translation target; `NO_TRANSLATION` sentinel = keep original |
| `shortcut`, `mode` | str | `ctrl+space`, `toggle` | Hotkey + recording behavior |
| `minimize_to_tray`, `start_minimized`, `run_at_startup` | bool | flags | Lifecycle |
| `theme` | str | `"dark"` | `dark` / `light` |
| `installed_release_tag` | str | `""` | Updater bookkeeping |
| *(disk only)* `api_key_protected` | str | — | `dpapi:<b64>` / `base64:<b64>` |

**Invariants:** unknown/missing keys normalize to defaults; plaintext `api_key` on disk triggers rewrite-on-load; DPAPI failure on Windows must never downgrade to plaintext.

## 3. Outbound Data Flows (third parties)

| Flow | Destination | Payload | Bound / policy |
| :--- | :--- | :--- | :--- |
| Model listing | `{base_url}/GET models` | Bearer key (optional) | 15 s timeout, ONE retry, idempotent |
| Transcription (stage 1) | `{base_url}/POST chat/completions` | base64 WAV (≤20 MiB raw) + Arabic literal-transcribe prompt | 120 s timeout, NO auto-retry (billable) |
| Post-processing (stage 2) | same | raw transcript + Arabic cleanup/translate prompt | same |
| Update check | GitHub releases page (browser open only) | none | no telemetry |
| Paste output | OS clipboard + keystroke | final text | clipboard restored after paste |

**Privacy:** transcripts and audio leave the machine only toward the user-chosen provider endpoint; never logged; never persisted.

## 4. Shipped Delta (T-2026-11-16-provider-presets — implemented 2026-11-16, backward compatible)

| New key | Type | Semantics |
| :--- | :--- | :--- |
| `provider` | str | preset id (`xiaomi-mimo`, `openai`, …) or `custom`; missing ⇒ behaves exactly as today |
| `provider_variant` | str | MiMo endpoint plan: `api` / `token-plan-ams` / `token-plan-sgp` / `token-plan-cn` |
| `transcription_model` | str | stage-1 model when the preset uses split models (MiMo: `mimo-v2.5-asr`); empty ⇒ fall back to `model` |

Provider presets are pure static data (`providers.py`); the resolved URL is always persisted into `provider_base_url` so downstream consumers (`main.py`, `llm_api.py`) keep a single source of truth. No new secrets, no new stores.
