# SecOps Final Diff Audit Gate — T-2026-11-16-provider-presets

- **Auditor:** `secops-auditor` (A8) | **Run:** `20261116T140538Z` | **Date:** 2026-11-16
- **Scope:** uncommitted feature diff vs baseline `1e23d2d` — `providers.py` + `llm_payloads.py` (new), `config_manager.py`, `llm_api.py`, `main.py`, `settings_view.py`, `README.md`, tests, QA artifacts
- **Policies:** `.opencode/rules/02-cross-platform-security-policy.md` (A/B/C/D/I/J) + `.opencode/rules/01-clean-code-policy.md` (Gate 1)
- **QA input:** `artifacts/qa-reports/T-2026-11-16-provider-presets/20261116T135602Z/report.json` — 250 tests OK (241 pass / 9 env-gated skips / 0 fail), 3× stable, Linux docker reproof OK

## VERDICT: `PASS_WITH_RESIDUAL_RISKS` — release-ready, subject to documented residuals

Zero confirmed security findings. All evidence below was **independently executed by the auditor** (AST check, secret sweep, URL-hygiene check, crafted-config attack simulation, never-raise fuzz), not accepted from QA attestations alone.

## Control status

| # | Control (scope item) | Status | Key evidence |
|---|---|---|---|
| 1 | Secrets & key handling (A9/A11/D2) | **Verified** | DPAPI `api_key_protected` path untouched; `normalize_config` M12 never touches `api_key` (fuzz: key byte-preserved); zero logging in `providers`/`llm_payloads`; `_safe_url_for_log` still sole URL log path (`llm_api.py:166`); `tp-xxxxx` hint is a static placeholder shown only when the key is empty; secret sweep (sk-/AIza/ghp_/tp-<10+>/Bearer/dpapi:/base64:) over diff+tests+artifacts: **zero hits** |
| 2 | URL validation & SSRF posture (A4/B8/H4) | **Verified** | `_endpoint` (`llm_api.py:103-119`, unchanged) rejects non-http(s)/userinfo/query/fragment before any I/O; all 12 registry URLs are clean compile-time constants (auditor-verified); **registry-wins at load/save/fetch** — crafted config with preset provider + attacker URL is overwritten at load (attack sim) |
| 3 | Wire payloads (A3/A5) | **Verified** | MiMo payload = one Data-URL `input_audio`, no `format` key, no text prompt, `asr_options.language` mapped zh/en/else→`auto`; no transcript in any stage-1 payload; size caps resolved **before** read/base64/network (`transcribe_audio:275-286`): 20 MiB generic / 7 MiB MiMo (base64 < 10M chars); unknown dialect → generic shape AND generic cap (gate/shape can never disagree) |
| 4 | Input validation at new boundary | **Verified** | `normalize_config` never raises (fuzz: None/int/str/list/dict/garbage); fail-safe resolution: unknown provider→`custom`, variant→default, dialect→`openai`; no `eval`/`exec`/pickle/network in either new module (AST); empty effective stage-1 model fails closed pre-network (QA OBS-1) |
| 5 | UI (A5/C) | **Verified** | Key masked by default (`show="*"`); preset URL field read-only (Custom editable only); `save_payload` re-resolves registry URL regardless of widget contents (widget-tamper defense); status label shows static hints only; `key_status_hint` returns `''` whenever a key is present |
| 6 | Supply chain (I2) | **Verified** | `git diff` on `requirements*.txt` = **empty**; new modules stdlib-only (`llm_payloads` imports nothing; `providers` imports `dataclasses`+`typing`); 7-day cooldown N/A |
| 7 | Clean/Hexagonal (Gate 1) | **Verified** | Acyclic inward dependencies (providers/llm_payloads pure leaves; no module imports `main`); sizes ≤350 (providers 349, llm_payloads 157, llm_api 349, settings_view 335, main 337, config_manager 269); no `typing.Any`; error taxonomy preserved, no swallowed errors; dialect-tag duplication deliberate + QA contract-pinned (R-11) |
| 8 | Evidence matrix maintenance (J2) | **Verified** | SEC-D2-02 refreshed with **auditor-verified** line numbers (`save_settings` = `settings_view.py:292-308` — the brief's "300-335" was NOT confirmed); new rows SEC-PROV-01..05 added; stale refs in SEC-A11-01/SEC-H4-01/SEC-A5-05/SEC-CLEAN-01 refreshed |

## Findings (adversarial candidates, disproved or noted)

| ID | Candidate | Classification | Disproof / note |
|---|---|---|---|
| SEC-F-01 | Crafted config redirects a **preset's** key to an attacker URL | **rejected** | Registry-wins at every path: `normalize_config` M7 at load (attack sim: evil URL → `https://token-plan-ams.xiaomimimo.com/v1`), `save_payload` at save, `resolve_base_url` at fetch. Stored preset URL is dead data |
| SEC-F-02 | Malformed provider/variant crashes or bypasses guard/dialect | **rejected** (security) + **HN-11** | All resolvers isinstance-guarded, fail-safe, never raise (fuzz). Non-str config values (`null` model) can AttributeError in the guard — pre-existing pattern, same-user only, **fail-closed** (no request, no key material in traceback) |
| SEC-F-03 | MiMo builder can be tricked into logging transcript/prompt | **rejected** | Zero logging/print in `llm_payloads` (AST); one URL log line per request, redacted; full call-site inventory clean |
| SEC-F-04 | HN-9 retained: `_endpoint` scheme-error echoes `base_url!r` before the credential check | **hardening_note** | UI-display-only (never logged); fix: run credential rejection first / redact userinfo |
| SEC-F-05 | providers.py / llm_api.py at 349/350 lines | **hardening_note** | Within gate, zero headroom; extract on next growth |
| SEC-F-06 | Custom-URL mode sends the key to the configured URL | **needs_validation → accepted RES-1** | Inherent to user-configured endpoints (README: "any URL you enter"); presets immune (SEC-F-01); same-user threat (config tamper ≡ binary replace); cross-user depends on profile ACLs (carried under SEC-D2-01) |
| SEC-F-07 | Key/token-plan hints echo key material | **rejected** | `key_hint` returns `''` for filled keys (fuzz with real-looking key); `tp-xxxxx` is a static placeholder |
| SEC-F-08 | Hint-copy duplication (`_KEY_REQUIRED_HINT` / `FALLBACK_KEY_HINT`) | **hardening_note** | Deliberate purity tradeoff, documented; consolidate if copy changes |

## Residual risks

- **RES-1 (accepted, documented):** Custom-mode URL trust — a tampered `config.json` can point Custom at an attacker host and the key follows. Rationale: same-user model, presets immune, README documents the trust decision, per-user config location blocks cross-user tampering (Linux ACL fact carried as `needs_validation`).
- **RES-2 (retained HNs):** HN-1 (generic `logger.exception` tracebacks may embed exotic library strings), HN-9 (see SEC-F-04), HN-11 (see SEC-F-02). None crosses a trust boundary or reaches a log sink with secrets.
- **RES-3 (QA OBS-1):** whitespace-only `transcription_model` fails closed with `LLMConfigError` pre-network; unreachable through the app; pinned as contract test.

## Carried `needs_validation`

- SEC-D2-01 — Linux at-rest key: `base64:` fallback depends on home-dir privacy (upstream-unchanged).
- SEC-LOG-01 — log-directory ACLs (`icacls` on a target machine).

## SEC-D2-02 line-number refresh (auditor-verified)

- `settings_view.save_settings`: **`settings_view.py:292-308`** (canonical read via `providers.save_payload` at :295-302; `save_config` at :303). The range "300-335" from the task brief is **not** the actual location.
- `llm_api._build_request` key-optional check: `llm_api.py:122-130` (`if api_key:` at :124).
- `main.py` recording guard: `main.py:221-235` (effective-models fail-closed check at :228-232).
- `config_manager._disk_config` key protection: `config_manager.py:175-178`.
