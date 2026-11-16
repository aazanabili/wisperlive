# SecOps Red-Team Audit Report — T-2026-11-16-integrate-finish (Phase 7)

- **Auditor:** `secops-auditor` (A8) | **Date:** 2026-11-16 | **Lineage:** L1 | **Attempt:** 1/3
- **Risk Tier:** Tier 3 | **Target:** `H:\projects\wisperlive`, branch `finish`, HEAD `a4b9c5d`
- **Diff under audit:** `git diff 9a35dd0 a4b9c5d` (32 files, +3403/−1029) — the controlled re-port of upstream v2.5 (issue-14) + our cleanup-hardening — plus new untracked surface (ZERO files at audit time) and the QA artifact `artifacts/qa-reports/T-2026-11-16-integrate-finish/RUN-20261116-001/report.json`.
- **Policies:** `.opencode/rules/02-cross-platform-security-policy.md` (A4/A5/A11, A6/A9, C-reduced, D2/D3/D5/D7/D8, E1, E2, I2) + `.opencode/rules/01-clean-code-policy.md` (Gate 1). Skills loaded: `deep-security-hunting`, `clean-hexagonal-enforcer`.
- **VERDICT: PASS** — 0 confirmed findings; 5 hardening notes (non-blocking); 2 needs-validation (external facts); accepted residuals documented.

## 0. Method

Six-step coverage-led hunt: trust-boundary map (Tk UI thread ← worker/hotkey/tray threads ← LLM provider over HTTPS; local FS/registry/clipboard) → supply-chain check → per-boundary attack-class sweep → adversarial self-disproval → evidence matrix sync. Every row of `SECURITY_EVIDENCE_MATRIX.md` (root, written this phase) carries file:line evidence. The QA suite (150 tests) was **independently re-run by the auditor**: `venv\Scripts\python.exe -m unittest discover` → `Ran 150 tests … OK (skipped=9)` (skips = platform-gated X11/Windows cases).

## 1. A11 Privacy — transcript/prompt/audio/API-key logging (Mandatory check 1)

**Verdict: PASS (Verified).** Complete call-site inventory of all 37 production log sites across 12 modules:

- **`llm_api` exception paths:** the single URL log line is sanitized — `llm_api.py:187` logs `_safe_url_for_log(request.full_url)` (strips userinfo/query/fragment; fails closed to `<redacted url>`, :114-121). File-read failures log `type(error).__name__` only (:297); oversize logs a byte count (:300). The H2 boundary catch-alls (:246, :328, :358) use `logger.exception` with **static messages** — tracebacks, no payload interpolation. No path logs `raw_text`, `audio_data`, `prompt`, or `api_key`.
- **`settings_view`:** the ONLY log site is `logger.exception("Unexpected error while fetching models")` (settings_view.py:87) with a static message. `toggle_key_visibility` (settings_view.py:42-48) toggles the entry widget's `show` option — pure UI, zero persistence/logging. `save_settings` (:104-113) routes the key straight into `config["api_key"]` → DPAPI-protected save. **No provider-key logging or leakage.**
- **`config_manager` (upstream, byte-identical):** all 8 log sites are value-free static warnings (e.g., "Unable to decrypt stored API key…; ignoring it", config_manager.py:112) — the key itself is never interpolated.
- **`main`/`auto_typer`/`audio_recorder`:** static strings; paths logged as `os.path.basename` only (main.py:282, audio_recorder.py:230).
- **QA report artifact:** `report.json` contains case dispositions/counts only — grep for key patterns/transcript fragments: clean.
- **Non-vocuous test:** `test_llm_api.py:338 test_no_transcript_or_credentials_in_logs` installs a `Capture` handler and asserts sentinels absent (:382-384), plus a direct `_safe_url_for_log("https://user:pass@example.com/v1?key=TOPSECRET#frag")` proof (:385-388). Green.

**Residuals (hardening notes, retained from prior task):** HN-1 `logger.exception` tracebacks can embed exotic library error strings; HN-2 a soundfile error raised inside `_record_thread` (audio_recorder.py:179) may embed the full %TEMP% path (Windows username), inconsistent with the module's basename policy (:230).

## 2. Secrets at rest — config_manager byte-identity + D2 optional key (check 2)

**Verdict: PASS (Verified), one external-fact note.**

- **D3 proof RE-VERIFIED:** `git diff 9a35dd0 HEAD -- config_manager.py` = **empty**; blob hash `e2bdee9edd5415d29ee1fa5984a78fd01395fb26` identical at `9a35dd0` and `HEAD`. (The T-2026-11-15 variant, blob `9a97291e…`, was deliberately NOT ported per plan §3 KEEP UNCHANGED.) DPAPI path intact: `_protect_api_key` raises `RuntimeError` when DPAPI is unavailable — **never** a plaintext/reversible downgrade on Windows (config_manager.py:95-98); `save_config` converts that to `False` (no write at all, :214-215). Legacy plaintext `api_key` files are rewritten on first load (:225-230). Atomic mkstemp + `os.replace` writes (:187-193).
- **D2 optional key — no plaintext fallback:** the empty-key path only omits the `Authorization` header (llm_api.py:145-146; `test_omits_auth_header_without_key` green). EVERY persistence path funnels through `_disk_config` → `_protect_api_key` (config_manager.py:167-171); auditor grep found no alternate key-write path in the merged tree. `collect_form_values` (views.py:170-178) never touches `api_key`.
- **H4 `provider_base_url` credential rejection — enforced BEFORE any request:** `_endpoint` (llm_api.py:124-140) rejects `parsed.username/password` (covers `user:pass@`) and `parsed.query/fragment` (covers `?key=`) and is the FIRST call in `fetch_models` (:227), `transcribe_audio` (:291), `post_process_text` (:342) — always before `_build_request`/`urlopen`. Coverage confirmed by tracing all three request paths.
- **HN-9 (new, non-blocking):** the invalid-scheme error at llm_api.py:131 echoes `base_url!r` **before** the credential check at :136 — a typo'd-scheme URL containing `user:pass@` would echo the credential into the error message. Impact bounded: the message is display-only (`set_status`, views.py:312-323 — no logging; `show_result`, settings_view.py:66-71 — no logging), so it never reaches disk. Fix: reorder the checks or redact userinfo in the message.
- **Needs-validation:** on Linux the at-rest key is `base64:` obfuscation only (config_manager.py:99-101) — upstream-unchanged (D3 mandated byte-identity), protected by mkstemp 0600 file perms + home-dir privacy. Exact missing fact: target-distro home ACLs on multi-user machines.

## 3. A5 resource bounds (check 3) — ALL enforced in code

| Bound | Constant / code | Enforced at |
|---|---|---|
| 20 MiB audio cap (H1) | `MAX_AUDIO_BYTES = 20*1024*1024` | llm_api.py:56; read capped at +1 (:295); oversize → user-safe error before base64/request (:299-303) |
| Deadline-bounded chunked reads (H5) | `READ_CHUNK_BYTES=64KiB`, wall-clock deadline, 16 MiB body cap | llm_api.py:154-182 (honest bound: `timeout` + one socket read), :51,:178-179 |
| Request timeouts | `MODELS_TIMEOUT=15`, `GENERATION_TIMEOUT=120` | llm_api.py:47-48 on every `urlopen` |
| Recorder queue/join | `QUEUE_POLL_TIMEOUT_S=0.25`, `THREAD_JOIN_TIMEOUT_S=5.0` | audio_recorder.py:164 (`q.get(timeout=…)`), :198/:217 (bounded `join`), :203-205 (timeout → cleanup + `RecorderError`) |
| Clipboard retries | `CLIPBOARD_RETRY_ATTEMPTS=2`, `CLIPBOARD_RETRY_DELAY_S=0.05` | auto_typer.py:71-78 bounded loop |
| Log rotation | `MAX_LOG_BYTES=1 MiB`, `LOG_BACKUP_COUNT=3` (4 MiB hard bound) | app_logging.py:19-20, :36-41 |

Closes prior-task HN-3 (the old "no audio cap" row is retired → Verified).

## 4. A4/D3 temp files — mkstemp single-owner both directions (check 4)

**Verdict: PASS — the upstream WAV leak is provably gone.**

- **Creation:** `tempfile.mkstemp(prefix="whisperlive-", suffix=".wav")` (audio_recorder.py:133) — O_EXCL, OS-unique, %TEMP%; the old predictable CWD `temp_audio.wav` write has zero prod hits (grep).
- **Failure direction:** every `stop_recording` failure path calls `_cleanup(path)` BEFORE raising (audio_recorder.py:200-208: thread error / join-timeout / zero-blocks) — the caller never receives the path, so a failed recording can never leak. `cancel_recording` deletes internally (:218). Covered by QA cases 3/4/7a/7b (`test_audio_recorder.py`).
- **Success direction:** path returned (:210) → caller deletes in `finally` (main.py:274-282) — including the M2 quit race (MP-04 `test_temp_file_deleted_even_when_exiting`). Double-delete idempotent (`FileNotFoundError: pass`, :279-280).
- **No user-file deletion:** path provenance is exclusively mkstemp; it never crosses user/config input. `_cleanup` unlinks exactly `self._current_path`.
- **HN-10 (new, extreme-rarity edge):** if `threading.Thread.start()` itself raised (RuntimeError) at audio_recorder.py:143 or main.py:255, the just-created WAV would orphan and `is_processing` (main.py:243) would stay set. Same rarity class as a process kill; recommend a future `try/except` wrap.

## 5. E1/E2 startup registration (check 5)

- **Windows (E1):** unchanged semantics — HKCU only, `KEY_SET_VALUE` least privilege, fully quoted executable path (`f'"{executable}" …'`, startup.py:25-27; `"` cannot appear in Windows paths → no quote-injection), `REG_SZ`, `OSError → False`. Rollback ordering preserved (main.py:148-190).
- **Linux (E2, new surface):** `set_linux_startup_registration` (startup.py:48-74) writes `_linux_autostart_dir() / f"{APP_NAME}.desktop"` — **fixed filename from the constant `APP_NAME="WhisperLive"`** (startup.py:18); zero untrusted input in path construction → **no traversal possible**. `_linux_autostart_dir` = `XDG_CONFIG_HOME` or `~/.config` + `autostart` (:77-81) — the env var is the user's own (same-user, no boundary). Content is a static template + `Exec=` of the quoted module-derived command; no secrets in the file; default-umask permissions are sane for non-sensitive content.

## 6. D5/IPC single-instance lock (check 6) — residuals stated honestly

`single_instance.py`: `os.open(..., O_RDWR|O_CREAT|O_CLOEXEC|O_NOFOLLOW, 0o600)` (:79) + POSIX owner-check `fstat(fd).st_uid == getuid()` → **fail-closed EPERM** (:83-90) + `XDG_RUNTIME_DIR` preference over world-writable /tmp (:57-64). Windows relies on per-user %TEMP% ACLs. `_notify_already_running` (main.py:41-53) logs/displays only the static `ALREADY_RUNNING_MESSAGE` ("Another instance of the application is already running.") — safe content.

**Honest residuals (accepted, documented in matrix SEC-D8-01):**
1. **Local DoS:** with `XDG_RUNTIME_DIR` unset, another local account can pre-create the /tmp lock path; the owner-check then refuses startup. Fail-closed = denial of service only, **not** a bypass; sticky-bit /tmp prevents deleting others' files.
2. **TOCTOU:** unlink+recreate of the lock path would flock a new inode (two instances). Requires write access to the lock dir (0700 XDG_RUNTIME_DIR / sticky /tmp / per-user %TEMP%) → same-user only; no cross-user compromise path.

## 7. I2 supply chain (check 7)

- **Requirements diff vs 9a35dd0 = ZERO new packages.** All changes are deletions of transitive pins + comments; the 7 runtime pins are version-identical; `python-xlib==0.33; sys_platform == "linux"` is an **unchanged upstream context line** (the "already-present marker" — confirmed, not an addition). `requirements-build.txt` adds only pyinstaller==6.22.2 + pyinstaller-hooks-contrib==2026.7 at the previously-pinned versions → 7-day cooldown N/A (no new adoption).
- **Workflow:** all jobs install only from pinned files (release.yml:33, :55, :115); test job gates builds (`needs:`). Our hunks add **no** `curl|sh` — only verify gates (Windows :65-70, Linux :126-133, 4-asset release gate :182-195).
- **Upstream tool steps (unchanged, integrity verified):** nfpm v2.48.0 download checks `EXPECTED_SHA256` via `sha256sum -c` with bounded retries (release.yml:144-161); appimagetool 1.9.1 verifies a hardcoded SHA-256 (:171-173). Both checksum-pinned; our diff leaves them byte-untouched.

## 8. Web/C — `webbrowser.open` targets (check 8)

All three prod call sites feed hardcoded constants: views.py:216-219 (About links) and views.py:233 (`RELEASES_PAGE` = updater.py:4 constant). The settings dialog has zero `webbrowser` usage. No user/config value ever reaches `webbrowser.open` → no `file://`/`javascript:` scheme injection possible. Row SEC-WEB-01 Verified.

## 9. M2/D7 residual paths (check 9)

- **Paste suppressed on quit:** `main.py:262-263` `if self.is_exiting: return` before `paste_text`; `call_in_ui` is a strict no-op after `is_exiting` (:128-129). QA MP cases green.
- **`is_processing` cannot stick:** written at :243 (UI thread) and reset ONLY by `_on_processing_finished` (:286-289, UI thread) — posted from the worker's `finally` (:284) so every completion path (success, LLMError/PasteError/ValueError, unexpected Exception) drains it; `_drain_ui_queue` isolates per-callback exceptions (:138-141) so one failure can't swallow the completion. Worker never writes the flag. The only stuck-state edge is HN-10 (Thread.start failure, extreme rarity).

## 10. Secrets sweep (check 10)

- Regex sweep (`AIza…`/`sk-…`/`ghp_…`/`Bearer …`/`dpapi:`/`base64:` blobs) across all tests + artifacts: **zero hits**; only the fake fixture `api_key="sk-test"` (test_llm_api.py:84).
- `git ls-files test_audio.wav` → **empty** (not tracked) and `git check-ignore test_audio.wav` confirms it is ignored (Phase 4 `.gitignore` union).
- **Privacy residual (noted for the user):** `test_audio.wav` (64 KB recorded speech fixture) **remains on local disk**. It is untracked and ignored, but it is the user's own local file — the factory will not delete user files; removal is the user's decision.

## 11. Clean Code gate (Gate 1)

All 16 production modules ≤350 lines (largest: llm_api.py 320, views.py 307, main.py 278). Import graph one-directional (no prod module imports `main`). Typed error taxonomy with user-safe messages (`LLMError` hierarchy, `RecorderError`, `PasteError`); no swallowed errors (broad catches re-raise typed or are documented teardown paths); named constants throughout; mixin split (views/settings_view) preserves SRP. Pre-existing nit: `windows_hotkey.py:109` f-string embeds `key_name` (not this diff).

## 12. Finding Ledger

| # | Finding | Class | Severity | Location | Disposition |
|---|---|---|---|---|---|
| 1 | Generic `logger.exception` tracebacks may embed exotic library error strings | hardening_note (HN-1) | Low | llm_api.py:246,328,358; main.py:271 | Non-blocking; retained |
| 2 | soundfile error may embed full %TEMP% path (username) in traceback | hardening_note (HN-2) | Low | audio_recorder.py:179 | Non-blocking; retained |
| 3 | Clipboard restore micro-TOCTOU | hardening_note (HN-6) | Negligible | auto_typer.py:89-91 | Accepted |
| 4 | `_endpoint` invalid-scheme error echoes `base_url!r` before the H4 credential check | hardening_note (HN-9) | Low | llm_api.py:131 vs :136 | Non-blocking (UI-display only, never logged); reorder/redact suggested |
| 5 | `Thread.start()` failure would orphan WAV / stick `is_processing` | hardening_note (HN-10) | Negligible | audio_recorder.py:140-143; main.py:243,251-255 | Non-blocking; future `try/except` wrap |
| 6 | Linux at-rest key = `base64:` obfuscation (upstream-unchanged per D3) | needs_validation | Info | config_manager.py:99-101 | External fact: home-dir ACLs |
| 7 | Log-dir DACL not explicitly set | needs_validation | Info | app_logging.py:34-41 | External fact: `icacls` on target |
| 8 | Single-instance local DoS / inode TOCTOU (fail-closed) | accepted residual | Low | single_instance.py:57-90 | Documented SEC-D8-01 |
| 9 | `test_audio.wav` remains on local disk | accepted residual (privacy) | Info | repo root | User's call |
| 10 | Transitive deps float (no hash pinning) | accepted residual | Info | requirements*.txt | Documented SEC-I2-03 |

**Confirmed (release-blocking): 0.** Previous task's HN-3 (no audio cap) is CLOSED by the 20 MiB cap; SEC-PROC-01 is superseded by Phase 6's D3/D5 byte-proofs.
