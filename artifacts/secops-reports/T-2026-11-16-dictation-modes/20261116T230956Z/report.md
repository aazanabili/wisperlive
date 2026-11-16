# SecOps Final Adversarial Diff Audit — T-2026-11-16-dictation-modes (Tier 3)

- **Role:** `secops-auditor` (A8) | **Run:** `20261116T230956Z` | **Lineage:** L-dictation-modes-1
- **Scope:** uncommitted delta vs base `a6b2f45` (20 modified + 44 new files) — pure contracts, session orchestrator, engines (local / chunked / whisper_http / live-local LA-2 / live-cloud WS), VAD/PCM plumbing, injection stack (typer_actor / focus_guard / auto_typer / polish_engine), model store + manifest, settings views, `main.py`/`smoke.py`/`WhisperLive.spec`, `release.yml`, requirements, tests.
- **QA evidence base:** `artifacts/qa-reports/T-2026-11-16-dictation-modes/20261116T230125Z/report.json` — 632 tests / 622 pass / 10 pre-declared env-gated skips, G1–G6 all PASS.
- **Method:** deep-security-hunting + clean-hexagonal-enforcer; every control below was graded only after reading its enforcement point. All simulations executed against the actual tree (helper retained: `audit_static_checks.py`).

---

## 1. War-Room Focus Results

### D9 — Secrets (`api_keys_protected` map, K1–K8) — **Verified**
- `dictation_config.py` (pure, zero imports) implements K1–K8 exactly once: `coerce_key_map` (str→str only), `base64_fallback_count` (**count-only** aggregated warning — key bytes can never reach logs), `migrate_legacy_key` (K2: legacy migrates ONLY under the current provider, existing slot wins, DPAPI failure keeps value in memory and fails the disk write loudly — no plaintext downgrade), `derive_current_key` (K3: map authoritative), `read_vendor_key`/`with_vendor_key` (K7), `disk_key_map` (K4 dual-write: map authoritative + legacy blob mirrors the CURRENT provider's slot only), `needs_secret_rewrite` (K8: one-shot rewrite idempotent).
- `config_manager._protect_api_key` raises `RuntimeError` when Windows DPAPI is unavailable; `save_config` catches it to `False` — **no code path persists `api_key` outside an envelope**; `_fresh_defaults()` prevents cross-load dict aliasing (A3 s1.1).
- K7 per-vendor isolation re-verified by QA (provider-switch rehydration pin + discard-restore semantics) and traced by the auditor.
- **Residual (carried NEEDS_VALIDATION):** Linux `base64:` obfuscation at rest depends on home-dir privacy (SEC-D2-01).

### D9/D11 — URL logging & transcript logging — **Verified**
- `ws_url_for_log` (live_cloud_engine.py:44) and `_safe_url_for_log` (whisper_http.py:57, re-exported to model_store as `_redact_url`) strip userinfo/query/fragment and fail closed to `<redacted url>`. Live-cloud logs never include a URL at all (engine-id/type/code only); whisper_http logs scheme+host+path only; model_store redacts before its one URL-bearing warning.
- Keys travel in headers only (`Authorization: Bearer` / `x-goog-api-key`); endpoints are constants or `_endpoint`-validated (https-only, no userinfo/query/fragment).
- **D11 full call-site inventory (139 logger/print sites across 22 new modules): zero transcript, segment text, word, audio, or key bytes.** `repr=False` on every transcript/secret-bearing dataclass field (`WordSpan.text`, `SegmentIn.pcm16`, `EmissionOut.text`, `SessionConfig.api_keys`, `Transcript.text`); `error_kind` restricted to bare taxonomy names; QA runtime sentinel scrape (Arabic transcript + key sentinel, incl. the error path where the key sits inside the raised exception message) asserts zero leakage into captured log records.

### G3 — Supply chain — **Verified with one MEDIUM control defect (F-1)**
- `assets/models_manifest.json`: every digest is REAL — auditor spot-verified `faster-whisper-base/model.bin` sha256 `d01c3014…` against the **live Hugging Face LFS oid** at pinned commit `ebe41f70…` (byte-identical; all 4 file sizes match). URLs are commit-pinned `resolve/<sha>/…` on https mirrors.
- `model_manifest.parse_manifest` enforces: 64-lowercase-hex digests, 40-hex `hf_repo_sha`, positive int sizes, **https-only URLs with no userinfo/query/fragment**, and **relative paths rejecting `..`, absolute paths and drive letters** (traversal defense). Placeholder (`000…0`) digests are refused at install time (model_store.py:178). The manifest ships **inside the binary** (WhisperLive.spec datas) — users cannot edit pins via config.
- Download integrity: streamed SHA-256 over resumed+new bytes, `Range`/`If-Range` resume (entity change → full restart), final **digest AND size gate before atomic `os.replace`**, fsync before close, post-rename re-verification, mirror fallback never weakens integrity (digest is per-file pinned), placeholder/unmanifested models fail closed, tampered installed files detected by `re_check_file`.
- **F-1 (MEDIUM, confirmed control defect, NOT exploitable today):** the CI cooldown checker in `release.yml` matches pins with `^([A-Za-z0-9_.\-]+)==([\w.\-]+)$` after stripping only `;…` — **any pin with an inline `#` comment silently escapes the check**. All 6 `requirements-ml.txt` pins carry `[V]` inline comments → 0 of 6 are machine-checked (reproduced by the auditor). Current pins are all ≥ 7 days old (in-file date evidence + auditor's independent PyPI check of `websocket-client==1.9.2`, uploaded 2026-08-31 ≈ 78d), so there is **no present violation** — but the gate must be fixed (strip `#` comments; count skips) **before the next dependency change**.
- Workflow pins: version tags are mutable (incl. third-party gitleaks/trivy/anchore/softprops actions) — SHA-pinning recommended (HN-DICT-09). Gitleaks (history), Trivy (fs + SBOM, CRITICAL fails), SPDX SBOM per build, 350 MB artifact budget (catches accidental model bundling) all present and sensible.

### U2 — Gemini consent — **Verified at documented enforcement points; scope gap noted (F-2)**
- Fail-closed BEFORE any connection: `preflight()` runs before `engine.start()`; `_require_cloud_live` orders **consent → key → transport import**; chunked `resolved == "google-gemini"` gated identically. QA asserts `ConsentRequiredError` with **ZERO connects**.
- Coercion-proof: M20 normalization makes `gemini_consent`/`gemini_paid_key_confirmed` literal-`True`-only (`"yes"`/`1`/`"true"` → `False`); paid-key bypass is an explicit attestation checkbox ("I confirm this Google API key is a paid Google Cloud key."), and the disclosure copy is present **in red** (danger token): "I understand: cloud processing — Google may use my data for training."
- **F-2 (LOW-MEDIUM, hardening note):** the gate covers the Gemini *engines* (documented scope) but **not** stage-2 cleanup (`cleanup_mode="cloud"`) or Polish cloud-cleanup when the chat provider is the selectable `google-gemini` preset — the full transcript then reaches Google free tier without the consent gate. Same exposure pre-exists in the classic pipeline (accepted previously). Product decision: extend the gate or the disclosure copy.

### D3/D4 — Injection & IPC — **Verified with documented residuals (F-4/F-5/F-6)**
- **Focus guard before EVERY emission:** `guarded_emit` = abort/is_exiting gate → `focus_guard.check()` (raises `FocusLostError`, emission RETAINED for resume/copy) → re-check abort/is_exiting → inject. QA pins focus-change retention, abort freeze, is_exiting freeze, and abort-between-check-and-inject suppression. M2 `is_exiting` suppression is unbroken across `guarded_emit`, `type_text`, `_finish` paste fallback, and `abort_all` (results cleared; zero emissions land after abort).
- **`LLKHF_INJECTED` self-filter (F-4):** correctly ignores *injected* events — but the flag is set for **every process's** SendInput output and WM_CHAR/PostMessage text never reaches `WH_KEYBOARD_LL`, so foreign automation can modify the target document **without** invalidating Polish spans. Impact bounded: `insert` default; `replace-experimental` gated by Windows + `unchanged()` **proof** + 10-min TTL + physical-key invalidation + post-injection span registration + auto-fallback to insert on any doubt. Same-user class (D1). Docstring over-claims "OUR OWN"; fix wording (optional `dwExtraInfo` marker).
- **Window-switch race (F-5):** `check()` → SendInput is a micro-TOCTOU inherent to `SendInput` (no atomic hwnd binding) — at most **one phrase** can land in the new foreground window; the next emission pauses and retains. Accepted and documented.
- **Clipboard (D4):** ONE `SessionClipboard` per session, save-once/restore-once with the equality guard (a newer user copy is never clobbered); undelivered backlog preserved via `copy_only` with a logged count warning (never silent); per-phrase restore timers banned in streaming paths. Exit path skips all clipboard ops (F-6, deliberate M2).

### D6 — Quota, no failover — **Verified**
- `check_admission` + `charge` run **atomically** under `self._cond` (two workers can never both pass a budget of one); reservations are conservative (a pre-send failure KEEPS the reservation); refusal produces a typed `LLMQuotaError` + degraded notice and **never** substitutes another vendor. The quota dialog is explicit user choice (Continue-on-local changes config for the **next** recording; Finish-at-stop is the Escape default; Copy-and-stop aborts). WS reconnects bounded (≤3/session, backoff+jitter ≤2 s, exhaustion degrades loudly via the notice sink).

### D7 — Model store specifics — **Verified with F-3 note**
- Resume semantics cannot accept a tampered `.part`: resumed bytes are hashed into the same verifier; mismatch → reset → next mirror; `If-Range` change → full restart; server `Content-Range` is not validated (digest catches; bandwidth-only). Disk preflight (`size × 1.2`) runs before the first byte and cannot be bypassed from Settings. Mirror fallback is digest-pinned per file — a weaker mirror cannot weaken integrity. Traversal-safe install paths (manifest-level + `os.path.join` on validated relative names).
- **F-3 (LOW):** the post-rename `_reverify()` **return value is discarded** (model_store.py:203) before `_activate`, and the pre-existing-files fast path (:184-185) activates presence-only — both only reachable by local same-user tampering (D1), and the pre-rename digest gate + `re_check_file` remain effective. One-line fix recommended.

### D5/D10 — Concurrency & cleanup — **Verified**
- Queues genuinely bounded: `BlockQueue` 256 (drop-newest, counted), `segment_q` maxsize 3 (blocking put with timeout + overflow notice), `PcmRing` 32 MiB (drop-oldest, counted, **surfaced** overflow notice — never silent), gap backlog capped (`MAX_GAP_SUBMISSIONS=32` loss declaration with warning). Audio is memory-only (D10); the classic temp-WAV path uses `mkstemp` + `finally`-delete (chunked chat path too); `.part` retention is by-design resume state with 7-day GC. Crash paths log types/counts only (D11). All joins bounded (2–5 s).

### Gate 1 — Clean / Hexagonal — **Verified (waivers honored)**
- Auditor-executed AST purity: `dictation_config`/`llm_errors` import **nothing**; `quota_ledger`/`model_manifest`/`dictation_types`/`session_state`/`pcm_stream`/`vad_segmenter` are stdlib-only (+ the pure `providers` leaf). Import-graph DFS over all 41 root modules: **zero cycles**. Dependency direction inward-only (`providers → dictation_config`; session/engines → pure core; adapters at the edges; `dictation_session` imports no tkinter — app is duck-typed).
- Module caps: every new module ≤ 350 except recorded waivers — `dictation_session.py` 763 ≤ 850 (**HN-DICT-01**, plan R-10; extraction into `dictation_runtime.py` mandatory post-ship), `model_store.py` 482 (**HN-DICT-02**, plan R-8a), `release.yml` exempt (R-8b: the 350 signal is a Python-module gate).
- No `any`-equivalent sloppiness (untrusted params typed `object`/`Mapping` + isinstance guards), no swallowed exceptions (broad catches are documented teardown/degrade paths or re-raise as typed errors), named constants throughout.

---

## 2. Findings Table

| ID | Severity | Classification | Title | Blocks release |
|---|---|---|---|---|
| F-1 | MEDIUM | confirmed (control defect) | CI 7-day cooldown checker silently skips inline-commented pins (all 6 ML pins unchecked today; no current violation) | No — fix REQUIRED before next dependency change |
| F-2 | LOW-MEDIUM | hardening_note | U2 consent gate does not cover cleanup/Polish cloud stage routed to the `google-gemini` chat preset | No — product decision |
| F-3 | LOW | hardening_note | model_store reverify result discarded; pre-existing-files activation presence-only (local-tamper class only) | No |
| F-4 | LOW | hardening_note | `LLKHF_INJECTED` filter cannot distinguish self vs foreign injection; WM_CHAR bypass; docstring over-claim | No |
| F-5 | LOW | accepted residual | focus-guard check→SendInput micro-race (≤ one emission misdelivery) | No |
| F-6 | INFO | accepted residual | exit-path skips clipboard restore (M2 tradeoff) | No |

Additional hardening notes: HN-DICT-09 (mutable action tags), HN-DICT-10 (`urls[0]` timestamp), HN-DICT-11 (redirect guard), HN-DICT-12 (mirrored read loop), HN-DICT-13 (typer backlog bound).

## 3. Control Statuses (J2 rows SEC-DICT-01..11, mirrored in `SECURITY_EVIDENCE_MATRIX.md`)

| Control | Status |
|---|---|
| D9 vendor secret map K1–K8 | Verified |
| D9 WS/HTTP URL+auth redaction | Verified |
| G3 model manifest integrity / install verification | Verified |
| G3/I2 cooldown + CI supply chain | **Failed** (checker logic F-1; current pins compliant) |
| U2 consent gate + red disclosure + paid attestation | Verified (residual HN-DICT-04) |
| D3/D4 injection guard + M2 suppression | Verified (residuals HN-DICT-06/07/08) |
| D3 span invalidation | Verified (residual HN-DICT-06) |
| D6 quota atomicity + no-failover | Verified |
| D11 no-transcript-logging | Verified |
| D5/D10 bounded queues / cleanup / abort | Verified |
| Gate-1 clean/hexagonal (+waivers) | Verified |

## 4. Verdict

**`PASS_WITH_RESIDUAL_RISKS`** — release gate may proceed.

Rationale: zero exploitable vulnerabilities; every war-room D-ruling control holds at its enforcement point. The single MEDIUM confirmed item (F-1) is a **gate-durability defect with no present policy violation** (all current pins independently ≥ 7 days old) and carries a two-line fix mandated before the next dependency change. If the release checklist treats CI-gate soundness as independently blocking, escalate to `FAIL` pending the F-1 patch — the auditor's evidence-based assessment is that shipping is proportionate.

## 5. Strict Final Report

- **Changes:** Completed the final adversarial Tier-3 SecOps diff audit of the dictation-modes delta vs `a6b2f45` (22 new production modules + 20 modified + test suites) against the Cross-Platform Security Policy (A/B-reduced/C-reduced/D/E2/I/J) and Clean/Hexagonal rules; re-verified every war-room focus control at its enforcement point; executed 9 independent attack simulations (manifest tamper, resume-tamper, LLKHF spoof, focus race, quota double-spend, consent coercion, log/secret scrapes, AST purity, cycle DFS); emitted `artifacts/secops-reports/T-2026-11-16-dictation-modes/20261116T230956Z/report.json` + `report.md` and refreshed `SECURITY_EVIDENCE_MATRIX.md` (header + SEC-DICT-01..11 rows + stale line-reference refresh on SEC-A11-01/SEC-H4-01/SEC-A5-05/SEC-D2-02/SEC-I2-01/SEC-I2-02/SEC-CLEAN-01). State: `REVIEWED` (release-gate verdict emitted; F-1 fix outstanding).
- **Decisions:** F-1 classified `confirmed` control defect but non-blocking (no current cooldown violation; fix mandated pre-next-dependency-change) — escalate-to-FAIL option documented for the release checklist; F-2 (Gemini cleanup-stage consent gap) recorded as product decision rather than FAIL since it lies outside the documented U2 scope and mirrors a previously accepted classic-pipeline flow; F-3/F-4 kept `hardening_note` per Layer-A-disproval doctrine (local same-user attacker class / digest gate already covers).
- **Verification:** 139 log call sites inventoried (zero transcript/key bytes) — PASS; secret-pattern sweep (prod + manifest + artifacts) — PASS (NONE); AST purity of 8 pure leaves — PASS (stdlib-only); import-graph DFS 41 modules — PASS (zero cycles); manifest `model.bin` sha256 vs live HF LFS oid — PASS (byte-identical); `websocket-client==1.9.2` cooldown (78d) — PASS; cooldown-checker regex reproduction — **FAIL (F-1)**; K1–K8/K7/consent/injection/quota/M2 traces vs QA evidence — PASS. All outcomes evidence-backed in `report.json`.
- **Limitations / Next steps:** F-1 fix (strip `#` comments in the cooldown checker; count skips) is REQUIRED before the next dependency addition/upgrade; F-2 needs a user/product decision (extend U2 gate to Gemini cleanup stage or extend disclosure copy); carried NEEDS_VALIDATION: Linux `base64:` at-rest key (home-dir privacy) and `%LOCALAPPDATA%\WhisperLive` log ACL (`icacls` on a target machine); HN-DICT-01 (dictation_session extraction) remains mandatory post-ship; live-cloud WS wire shapes remain vendor-doc-verified only (no live vendor session was run in this audit).
- **Runtime / Workspace:** No services/ports/processes started; read-only audit helper `audit_static_checks.py` retained inside the run artifacts directory. Writes limited to `SECURITY_EVIDENCE_MATRIX.md` + `artifacts/secops-reports/**` per ownership. No git actions performed; working tree left exactly as found apart from the authorized artifacts.
