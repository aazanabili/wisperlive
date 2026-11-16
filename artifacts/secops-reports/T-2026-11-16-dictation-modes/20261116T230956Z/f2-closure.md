# F-2 Closure Addendum — Gemini Consent Gate (unified `gemini_gate.py` rule)

- **Date:** 2026-11-17 | **Role:** `secops-auditor` (A8) | **Task:** T-2026-11-16-dictation-modes | **Decision:** USER Option A (gate BOTH paths)

## 1. Claims re-verified against source (read-only audit)
- `gemini_gate.py` = pure policy leaf (imports only `llm_errors` + `providers`; no I/O/UI/transport). ONE predicate `gemini_consent_ok` (literal `True` on `gemini_consent` OR `gemini_paid_key_confirmed`, M20 coercion-proof) + ONE endpoint rule `routes_to_gemini` (:68-74): preset id OR display label (`providers.provider_id_from_display`/`resolve_provider_id`) OR base-URL host `== generativelanguage.googleapis.com` / `*.host` → **cloned/hand-edited Custom Gemini URLs ARE covered**. Host parsing strips userinfo/port/path/query/fragment; exact-or-dot-suffix match → `evil-…googleapis.com.attacker.tld` suffix-confusion and `user@host` tricks **disproved**.
- Fail-closed wiring (each BEFORE any network I/O): `dictation_session.preflight` :202 (local + cloud cleanup), :212-213 (chunked: chat transcription + cloud cleanup on ANY engine; gate FIRST per U2/D9 order), :222-223 (`google-gemini`), :390-391 (`gemini-live`: consent → key → transport-import); `chunked_engine._cleanup` :157 before cloud `post_process_text`; `polish_engine.polish_last` :164 before cloud cleanup. SessionConfig/Mapping endpoint keys both match `_route_endpoint`. Save-blocker `save_route` → `VALIDATION_GEMINI(_PROVIDER)` (settings_dictation_view :92-98, invoked :132). Red disclosure on provider-tab Gemini key row (settings_keys_view.py:87-89).

## 2. Adversarial bypass hunt (production network call-site inventory)
- **Legacy `main.process_audio_thread` (:289):** no direct gate, but **unreachable with an ungated Gemini route** — `self.dictation` always set (main.py:99); `dictation_mode` fail-closed coerced to local/chunked/live at every load (`config_manager._normalized` → `providers.normalize_config` → `dictation_config.py:57`); the save-route blocker covers the dialog path (a Gemini route without consent cannot be persisted). → `rejected` (dead-code defense-in-depth only).
- **Fetch Models (`settings_provider_view.py:267` `fetch_models(base_url, api_key)`):** ungated request to a possible Gemini host — but carries **ZERO transcript/content bytes** (GET model list + API key only); the U2 asset (dictation content → free-tier training) is not exposed. → `hardening_note` **HN-DICT-14** (add `require_gemini_route` for rule literalism).
- **Direct `llm_api`/WS calls:** chunked stage-1 (:125) covered by preflight (engine starts only post-preflight); cleanup/Polish gated at call site; `whisper_http` = Groq-only; `live_cloud_engine` WS endpoints hardcoded constants (:28-29) — no custom-URL confusion. No other production caller.
- Zero-network claim holds for the consent case (ConsentRequiredError raised in preflight before `engine.start`); only a loopback Ollama presence GET can precede it (not Gemini-bound).

## 3. Outcome
F-2's finding (transcript reaches Gemini free tier via the general-provider/cleanup/Polish path without the gate) is fully remediated at every content-bearing enforcement point. Residuals are content-free (HN-DICT-14). Worker smoke 29/29 (cloned-Custom-Gemini-URL + zero-network) and suite 632 OK accepted as corroboration; matrix SEC-DICT-05 + report.json refreshed.

**F-2 CLOSED**
