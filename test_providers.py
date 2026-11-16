"""Registry invariants + M1-M12 migration matrix for providers.py (plan 2.2).

Owner: qa-test-assurance (test-only writer).  Pins the plan section 1.4
canonical table (ids/labels/URLs/variants/curated models), the R-11 dialect
literal contract with ``llm_payloads``, the M1-M12 normalization matrix, the
R-13 exact public surface, the AST purity guard, and the
``save_payload(initial_state(x))`` round-trip symmetry.  No network, no Tk,
no production writes.
"""

import ast
import unittest
from pathlib import Path

import llm_payloads
import providers

REPO_DIR = Path(__file__).resolve().parent

# --- plan section 1.4 canonical table (ruling R7) -----------------------------
CANONICAL = {
    # id: (label, base_url, dialect, model_field_mode)
    "xiaomi-mimo": ("Xiaomi MiMo", "https://api.xiaomimimo.com/v1", "mimo-asr", "split"),
    "openai": ("OpenAI", "https://api.openai.com/v1", "openai", "single"),
    "google-gemini": ("Google Gemini",
                      "https://generativelanguage.googleapis.com/v1beta/openai",
                      "openai", "single"),
    # R-2: DeepSeek canonical base has NO /v1 suffix (alias-equivalent).
    "deepseek": ("DeepSeek", "https://api.deepseek.com", "openai", "single"),
    "groq": ("Groq", "https://api.groq.com/openai/v1", "openai", "single"),
    "mistral": ("Mistral", "https://api.mistral.ai/v1", "openai", "single"),
    "openrouter": ("OpenRouter", "https://openrouter.ai/api/v1", "openai", "single"),
    "ollama-local": ("Ollama (local)", "http://localhost:11434/v1", "openai", "single"),
    "custom": ("Custom (OpenAI-compatible URL)", "", "openai", "single"),
}
CANONICAL_IDS = tuple(CANONICAL)

MIMO_VARIANT_URLS = {
    "api": "https://api.xiaomimimo.com/v1",
    "token-plan-ams": "https://token-plan-ams.xiaomimimo.com/v1",
    "token-plan-sgp": "https://token-plan-sgp.xiaomimimo.com/v1",
    "token-plan-cn": "https://token-plan-cn.xiaomimimo.com/v1",
}
MIMO_VARIANT_LABELS = {
    "api": "API (Pay-as-you-go)",
    "token-plan-ams": "Token Plan \u2014 Europe (AMS)",
    "token-plan-sgp": "Token Plan \u2014 Singapore (SGP)",
    "token-plan-cn": "Token Plan \u2014 China (CN)",
}
MIMO_TRANSCRIPTION_MODELS = ("mimo-v2.5-asr",)
MIMO_POST_MODELS = ("mimo-v2.6-flash", "mimo-v2.6-pro")
TTS_MODELS = ("mimo-v2.5-tts", "mimo-v2.5-tts-voiceclone", "mimo-v2.5-tts-voicedesign",
              "mimo-v2.5-voiceclone", "mimo-v2.5-voicedesign")


class RegistryInvariantTests(unittest.TestCase):
    def test_nine_preset_ids_exactly_in_ui_order(self):
        self.assertEqual(tuple(pid for pid, _label in providers.provider_choices()),
                         CANONICAL_IDS)
        # list_providers() excludes the custom sentinel (8 presets).
        self.assertEqual(tuple(p.id for p in providers.list_providers()),
                         CANONICAL_IDS[:-1])
        self.assertNotIn("custom", [p.id for p in providers.list_providers()])
        self.assertEqual(providers.provider_choices()[-1][0], "custom")

    def test_labels_match_approved_copy(self):
        for pid, (label, _url, _dialect, _mode) in CANONICAL.items():
            with self.subTest(provider=pid):
                if pid == "custom":
                    preset = providers.get_provider("custom")
                    self.assertIsNone(preset)  # custom is a sentinel, not a preset row
                    self.assertEqual(providers.provider_id_from_display(label), "custom")
                else:
                    self.assertEqual(providers.get_provider(pid).label, label)
        self.assertEqual(providers.display_names(),
                         tuple(spec[0] for spec in CANONICAL.values()))

    def test_base_urls_equal_plan_1_4_canonical_values(self):
        for pid, (_label, url, _dialect, _mode) in CANONICAL.items():
            with self.subTest(provider=pid):
                if pid == "custom":
                    self.assertEqual(providers.resolve_base_url("custom", "", "fallback"),
                                     "fallback")
                    continue
                self.assertEqual(providers.get_provider(pid).base_url, url)
                self.assertEqual(providers.resolve_base_url(pid), url)
        # R-2 explicit: DeepSeek is the root alias, NOT .../v1.
        self.assertEqual(providers.resolve_base_url("deepseek"), "https://api.deepseek.com")
        self.assertNotIn("/v1", providers.resolve_base_url("deepseek"))

    def test_mimo_variant_ids_urls_and_labels_exactly(self):
        variants = providers.variant_options("xiaomi-mimo")
        self.assertEqual(tuple(v.id for v in variants), tuple(MIMO_VARIANT_URLS))
        for variant in variants:
            with self.subTest(variant=variant.id):
                self.assertEqual(variant.base_url, MIMO_VARIANT_URLS[variant.id])
                self.assertEqual(variant.label, MIMO_VARIANT_LABELS[variant.id])
        # Only xiaomi-mimo has variants; default is the pay-as-you-go endpoint.
        for pid in CANONICAL_IDS:
            if pid in ("xiaomi-mimo", "custom"):
                continue
            with self.subTest(provider=pid):
                self.assertEqual(providers.variant_options(pid), ())
        self.assertEqual(providers.get_provider("xiaomi-mimo").default_variant_id, "api")

    def test_dialect_tags_equal_llm_payloads_literals(self):
        # R-11 contract pin: the two modules duplicate the tags (purity forbids
        # cross-imports); the copies must never drift.
        self.assertEqual(providers.DIALECT_OPENAI, llm_payloads.DIALECT_OPENAI)
        self.assertEqual(providers.DIALECT_MIMO_ASR, llm_payloads.DIALECT_MIMO_ASR)
        self.assertEqual(providers.DIALECT_OPENAI, "openai")
        self.assertEqual(providers.DIALECT_MIMO_ASR, "mimo-asr")
        for pid, (_label, _url, dialect, _mode) in CANONICAL.items():
            with self.subTest(provider=pid):
                self.assertEqual(providers.dialect_for_preset(pid), dialect)

    def test_dialect_for_preset_unknown_defaults_to_openai(self):
        for unknown in ("nope", "", None, 42, "custom", b"x", " XIAOMI-MIMO "):
            with self.subTest(value=unknown):
                self.assertEqual(providers.dialect_for_preset(unknown), "openai")
        self.assertEqual(providers.dialect_for_preset("xiaomi-mimo"), "mimo-asr")
        # A3 spells the same helper dialect_for; one implementation, two names.
        self.assertIs(providers.dialect_for, providers.dialect_for_preset)

    def test_mimo_curated_models_exactly(self):
        preset = providers.get_provider("xiaomi-mimo")
        self.assertEqual(preset.transcription_models, MIMO_TRANSCRIPTION_MODELS)
        self.assertEqual(preset.post_process_models, MIMO_POST_MODELS)
        self.assertEqual(preset.default_transcription_model, "mimo-v2.5-asr")
        self.assertEqual(preset.default_model, "mimo-v2.6-flash")
        self.assertEqual(preset.default_model, preset.post_process_models[0])

    def test_tts_models_absent_everywhere(self):
        # D2: the TTS trio is deliberately excluded; prove no registry field
        # (curated lists OR defaults) ever mentions a TTS id.
        for preset in providers.list_providers():
            strings = (tuple(preset.transcription_models)
                       + tuple(preset.post_process_models)
                       + (preset.default_model, preset.default_transcription_model))
            for value in strings:
                with self.subTest(provider=preset.id, value=value):
                    self.assertNotIn("tts", value.lower())
                    self.assertNotIn("voiceclone", value.lower())
                    self.assertNotIn("voicedesign", value.lower())
        for banned in TTS_MODELS:
            with self.subTest(banned=banned):
                self.assertFalse(providers.get_provider("xiaomi-mimo").transcription_models
                                 == (banned,))
                self.assertNotIn(banned, providers.get_provider("xiaomi-mimo").post_process_models)

    def test_api_key_required_false_only_for_ollama(self):
        false_ids = [p.id for p in providers.list_providers() if not p.api_key_required]
        self.assertEqual(false_ids, ["ollama-local"])
        self.assertFalse(providers.needs_api_key("ollama-local"))
        self.assertFalse(providers.needs_api_key("custom"))      # D2: keyless always allowed
        self.assertFalse(providers.needs_api_key("unknown"))
        self.assertFalse(providers.needs_api_key(None))
        self.assertTrue(providers.needs_api_key("openai"))
        self.assertTrue(providers.needs_api_key("xiaomi-mimo"))

    def test_mode_coherence(self):
        for preset in providers.list_providers():
            with self.subTest(provider=preset.id):
                self.assertIn(preset.model_field_mode, ("single", "split"))
                if preset.model_field_mode == "split":
                    self.assertTrue(preset.default_transcription_model)
                    self.assertTrue(preset.default_model)
                    self.assertIn(preset.default_model, preset.post_process_models)
                    self.assertIn(preset.default_transcription_model,
                                  preset.transcription_models)
                else:
                    self.assertEqual(preset.default_transcription_model, "")
                    self.assertEqual(preset.transcription_models, ())
                    self.assertEqual(preset.post_process_models, ())

    def test_variant_ids_resolve_to_known_ids_or_empty(self):
        known = set(MIMO_VARIANT_URLS)
        for value in list(MIMO_VARIANT_URLS) + ["bogus", "", None, 42,
                                                MIMO_VARIANT_LABELS["token-plan-cn"]]:
            resolved = providers.resolve_variant_id("xiaomi-mimo", value)
            with self.subTest(value=value):
                self.assertTrue(resolved in known or resolved == "")
        # M5/M6: invalid -> preset default; presets without variants -> "".
        self.assertEqual(providers.resolve_variant_id("xiaomi-mimo", "bogus"), "api")
        self.assertEqual(providers.resolve_variant_id("xiaomi-mimo", None), "api")
        self.assertEqual(providers.resolve_variant_id("openai", "api"), "")
        self.assertEqual(providers.resolve_variant_id("custom", "api"), "")

    def test_url_hygiene_static_h4_mirror(self):
        import urllib.parse

        def check(url, expect_scheme):
            parts = urllib.parse.urlsplit(url)
            self.assertIn(parts.scheme, expect_scheme, url)
            self.assertIsNone(parts.username, url)
            self.assertIsNone(parts.password, url)
            self.assertEqual(parts.query, "", url)
            self.assertEqual(parts.fragment, "", url)
            self.assertFalse(url.endswith("/"), url)
            self.assertTrue(parts.netloc, url)

        for preset in providers.list_providers():
            with self.subTest(provider=preset.id, kind="base"):
                if preset.id == "ollama-local":
                    check(preset.base_url, {"http"})   # loopback only
                    self.assertIn(urllib.parse.urlsplit(preset.base_url).hostname,
                                  ("localhost", "127.0.0.1"))
                else:
                    check(preset.base_url, {"https"})
            for variant in preset.variants:
                with self.subTest(provider=preset.id, kind=variant.id):
                    check(variant.base_url, {"https"})


class NormalizeConfigMatrixTests(unittest.TestCase):
    """Plan section 3.1 rules M1-M12, one test per rule family."""

    def test_m1_legacy_config_without_new_keys_is_byte_identical(self):
        legacy = {
            "api_key": "sk-legacy",
            "provider_base_url": "https://legacy.example/v1",
            "model": "legacy-model",
            "target_language": "Arabic",
            "theme": "light",
            "shortcut": "ctrl+alt+s",
        }
        out = providers.normalize_config(dict(legacy))
        # M1: provider absent -> custom; every OTHER pre-existing key untouched.
        self.assertEqual(out["provider"], "custom")
        for key, value in legacy.items():
            self.assertEqual(out[key], value, f"M1 changed {key}")
        # M4 side effects: the two managed stray keys are written with defaults.
        self.assertEqual(out["provider_variant"], "")
        self.assertEqual(out["transcription_model"], "")

    def test_m2_non_str_blank_provider_becomes_custom(self):
        for value in (None, 42, "", "   ", [], b"x", 3.14):
            with self.subTest(value=value):
                out = providers.normalize_config({"provider": value,
                                                  "model": "keep-me",
                                                  "provider_base_url": "https://k/v1"})
                self.assertEqual(out["provider"], "custom")
                self.assertEqual(out["model"], "keep-me")
                self.assertEqual(out["provider_base_url"], "https://k/v1")

    def test_m3_unknown_provider_id_keeps_stored_url_and_model(self):
        out = providers.normalize_config({
            "provider": "future-vendor-typo",
            "provider_base_url": "https://stored.example/v1",
            "model": "stored-model",
        })
        self.assertEqual(out["provider"], "custom")
        self.assertEqual(out["provider_base_url"], "https://stored.example/v1")
        self.assertEqual(out["model"], "stored-model")

    def test_m5_bad_or_missing_variant_falls_back_to_api(self):
        for value in (None, "", "   ", 42, "bogus", ["api"]):
            with self.subTest(value=value):
                out = providers.normalize_config({"provider": "xiaomi-mimo",
                                                  "provider_variant": value})
                self.assertEqual(out["provider_variant"], "api")
        # M6: presets without variants drop any stored variant id.
        self.assertEqual(providers.normalize_config(
            {"provider": "openai", "provider_variant": "token-plan-cn"}
        )["provider_variant"], "")

    def test_m7_registry_url_wins_over_stored_value(self):
        out = providers.normalize_config({
            "provider": "deepseek",
            "provider_base_url": "https://attacker.example/v1",
            "model": "deepseek-chat",
        })
        self.assertEqual(out["provider_base_url"], "https://api.deepseek.com")
        # Variant-resolved endpoint also wins for split presets.
        out = providers.normalize_config({
            "provider": "xiaomi-mimo",
            "provider_variant": "token-plan-sgp",
            "provider_base_url": "https://stale.example/v1",
        })
        self.assertEqual(out["provider_base_url"], MIMO_VARIANT_URLS["token-plan-sgp"])

    def test_m8_m9_split_defaults_fill_empty_and_non_str(self):
        for bad in ("", "   ", None, 42, [], {}):
            with self.subTest(model=bad):
                out = providers.normalize_config({"provider": "xiaomi-mimo",
                                                  "model": bad,
                                                  "transcription_model": bad})
                self.assertEqual(out["model"], "mimo-v2.6-flash")
                self.assertEqual(out["transcription_model"], "mimo-v2.5-asr")
        # Stored non-empty values survive untouched.
        out = providers.normalize_config({"provider": "xiaomi-mimo",
                                          "model": "mimo-v2.6-pro",
                                          "transcription_model": "my-asr"})
        self.assertEqual(out["model"], "mimo-v2.6-pro")
        self.assertEqual(out["transcription_model"], "my-asr")

    def test_m10_single_mode_non_str_model_fails_closed_to_empty(self):
        for bad in (None, 42, [], "   "):
            with self.subTest(model=bad):
                out = providers.normalize_config({"provider": "openai", "model": bad})
                self.assertEqual(out["model"], "")
        # A real string is preserved.
        self.assertEqual(providers.normalize_config(
            {"provider": "openai", "model": "gpt-x"}
        )["model"], "gpt-x")

    def test_m11_clears_transcription_model_for_single_and_custom(self):
        out = providers.normalize_config({"provider": "openai",
                                          "model": "gpt-x",
                                          "transcription_model": "mimo-v2.5-asr"})
        self.assertEqual(out["transcription_model"], "")
        out = providers.normalize_config({"provider": "groq",
                                          "provider_base_url": "https://api.groq.com/openai/v1",
                                          "model": "llama",
                                          "transcription_model": "leak"})
        self.assertEqual(out["transcription_model"], "")
        out = providers.normalize_config({"provider": "custom",
                                          "provider_base_url": "https://u/v1",
                                          "model": "m",
                                          "transcription_model": "leak"})
        self.assertEqual(out["transcription_model"], "")

    def test_m12_unknown_keys_round_trip_untouched(self):
        cfg = {
            "api_key": "sk-keep",
            "api_key_protected": "dpapi:QUJD",
            "future_key": {"nested": [1, 2, 3]},
            "installed_release_tag": "v9.9.9",
            "provider": "openai",
            "provider_base_url": "https://api.openai.com/v1",
            "model": "gpt-x",
        }
        out = providers.normalize_config(cfg)
        self.assertEqual(out["api_key"], "sk-keep")
        self.assertEqual(out["api_key_protected"], "dpapi:QUJD")
        self.assertEqual(out["future_key"], {"nested": [1, 2, 3]})
        self.assertEqual(out["installed_release_tag"], "v9.9.9")
        self.assertNotIn("post_process_model", out)

    def test_normalization_is_idempotent(self):
        samples = [
            {"provider": "xiaomi-mimo", "provider_variant": "bogus"},
            {"provider": "deepseek", "provider_base_url": "https://wrong/v1"},
            {"model": "legacy"},
            {"provider": "custom", "provider_base_url": "https://u/v1", "model": "m"},
            {},
        ]
        for cfg in samples:
            with self.subTest(cfg=cfg):
                once = providers.normalize_config(dict(cfg))
                twice = providers.normalize_config(dict(once))
                self.assertEqual(once, twice)

    def test_n1_never_raises_on_garbage_input(self):
        for garbage in (None, 42, 3.14, object(), "ab", b"xy", [], [1, 2, 3]):
            with self.subTest(value=garbage):
                out = providers.normalize_config(garbage)
                self.assertIsInstance(out, dict)
                self.assertEqual(out.get("provider"), "custom")

    def test_non_dict_mapping_input_is_normalized(self):
        out = providers.normalize_config([("provider", "openai"), ("model", "gpt-x")])
        self.assertEqual(out["provider"], "openai")
        self.assertEqual(out["model"], "gpt-x")


class ResolutionHelperTests(unittest.TestCase):
    def test_resolve_base_url_fallback_rules(self):
        self.assertEqual(providers.resolve_base_url("custom", "", "https://f/v1"),
                         "https://f/v1")
        self.assertEqual(providers.resolve_base_url("nope", "", "https://f/v1"),
                         "https://f/v1")
        self.assertEqual(providers.resolve_base_url("nope", "", 42), "")   # non-str -> ""
        # Known presets ignore the fallback (registry wins).
        self.assertEqual(providers.resolve_base_url("openai", "", "https://ignored"),
                         "https://api.openai.com/v1")

    def test_resolve_transcription_model_mirrors_runtime_fallback(self):
        # split: stored -> curated default -> model fallback
        self.assertEqual(
            providers.resolve_transcription_model("xiaomi-mimo", "my-asr", "m"), "my-asr")
        self.assertEqual(
            providers.resolve_transcription_model("xiaomi-mimo", "", "m"), "mimo-v2.5-asr")
        self.assertEqual(
            providers.resolve_transcription_model("xiaomi-mimo", None, "m"), "mimo-v2.5-asr")
        # single/custom: effective stage 1 == model (M11 already cleared strays)
        self.assertEqual(providers.resolve_transcription_model("openai", "stray", "m"), "m")
        self.assertEqual(providers.resolve_transcription_model("custom", "", "m"), "m")

    def test_is_known_and_get_provider_edge_cases(self):
        for pid in CANONICAL_IDS:
            with self.subTest(pid=pid):
                self.assertTrue(providers.is_known_provider(pid))
        for value in ("nope", "", None, 42, b"x"):
            self.assertFalse(providers.is_known_provider(value), value)
        self.assertIsNone(providers.get_provider("custom"))
        self.assertIsNone(providers.get_provider(None))
        self.assertIsNone(providers.get_provider(42))
        self.assertEqual(providers.get_provider(" openai ").id, "openai")  # strips

    def test_provider_id_from_display_roundtrip(self):
        for pid, (label, *_rest) in CANONICAL.items():
            with self.subTest(label=label):
                self.assertEqual(providers.provider_id_from_display(label), pid)
        self.assertIsNone(providers.provider_id_from_display("Unknown Vendor"))
        self.assertIsNone(providers.provider_id_from_display(None))
        self.assertIsNone(providers.provider_id_from_display(42))

    def test_validate_provider_selection_advisory_matrix(self):
        # normalized preset config -> OK
        self.assertEqual(providers.validate_provider_selection(
            {"provider": "deepseek", "provider_base_url": "https://api.deepseek.com",
             "model": "deepseek-chat", "transcription_model": ""}), ())
        # fully filled custom -> OK
        self.assertEqual(providers.validate_provider_selection(
            {"provider": "custom", "provider_base_url": "https://u/v1",
             "model": "my-model", "transcription_model": ""}), ())
        # custom + empty URL
        issues = providers.validate_provider_selection(
            {"provider": "custom", "provider_base_url": "", "model": "m"})
        self.assertIn("Provider Base URL is not set.", issues)
        # empty model
        issues = providers.validate_provider_selection(
            {"provider": "openai", "provider_base_url": "https://api.openai.com/v1",
             "model": "", "transcription_model": ""})
        self.assertTrue(any("No model is selected" in msg for msg in issues))
        # unknown raw provider id -> advisory warning, never raises
        issues = providers.validate_provider_selection(
            {"provider": "bogus", "provider_base_url": "https://u/v1", "model": "m"})
        self.assertTrue(any("not recognized" in msg for msg in issues))
        # split preset without a stage-1 model
        issues = providers.validate_provider_selection(
            {"provider": "xiaomi-mimo",
             "provider_base_url": MIMO_VARIANT_URLS["api"],
             "model": "mimo-v2.6-flash", "transcription_model": ""})
        self.assertTrue(any("transcription model" in msg for msg in issues))
        # garbage input never raises
        self.assertIsInstance(providers.validate_provider_selection(None), tuple)
        self.assertIsInstance(providers.validate_provider_selection("junk"), tuple)


class SaveInitialRoundTripTests(unittest.TestCase):
    def test_save_payload_keys_are_exactly_the_six_contract_keys(self):
        payload = providers.save_payload({"provider": "OpenAI", "model": "gpt-x"})
        self.assertEqual(set(payload), {"provider", "provider_variant",
                                        "provider_base_url", "api_key", "model",
                                        "transcription_model"})
        self.assertNotIn("post_process_model", payload)

    def test_save_after_initial_state_is_symmetric(self):
        cases = [
            # legacy custom: URL/model byte-identical through the dialog
            ({"provider_base_url": "https://legacy.example/v1", "model": "old-model",
              "api_key": "k"},
             {"provider": "custom", "provider_variant": "",
              "provider_base_url": "https://legacy.example/v1", "api_key": "k",
              "model": "old-model", "transcription_model": ""}),
            # split preset with a token-plan variant: re-resolves its endpoint
            ({"provider": "xiaomi-mimo", "provider_variant": "token-plan-sgp",
              "provider_base_url": "https://stale.example/v1", "model": "mimo-v2.6-pro",
              "transcription_model": "mimo-v2.5-asr", "api_key": "tp-k"},
             {"provider": "xiaomi-mimo", "provider_variant": "token-plan-sgp",
              "provider_base_url": MIMO_VARIANT_URLS["token-plan-sgp"],
              "api_key": "tp-k", "model": "mimo-v2.6-pro",
              "transcription_model": "mimo-v2.5-asr"}),
            # single preset: stray stage-1 value can never leak (R3 IFF)
            ({"provider": "openai", "provider_base_url": "https://api.openai.com/v1",
              "model": "gpt-x", "transcription_model": "mimo-v2.5-asr", "api_key": "sk"},
             {"provider": "openai", "provider_variant": "",
              "provider_base_url": "https://api.openai.com/v1", "api_key": "sk",
              "model": "gpt-x", "transcription_model": ""}),
        ]
        for config, expected in cases:
            with self.subTest(config=config):
                payload = providers.save_payload(providers.initial_state(config))
                self.assertEqual(payload, expected)
                # symmetry: feeding the payload back through the dialog is stable
                self.assertEqual(
                    providers.save_payload(providers.initial_state(payload)), expected)

    def test_save_accepts_display_labels_and_ids(self):
        by_label = providers.save_payload({"provider": "Groq", "model": "llama-x"})
        by_id = providers.save_payload({"provider": "groq", "model": "llama-x"})
        self.assertEqual(by_label, by_id)
        self.assertEqual(by_label["provider"], "groq")

    def test_initial_state_hydration_without_url_inference(self):
        # legacy config -> custom shows the stored URL verbatim (no inference)
        state = providers.initial_state({"provider_base_url": "https://legacy/v1",
                                         "model": "old", "api_key": "k"})
        self.assertEqual(state["provider"], "custom")
        self.assertEqual(state["provider_base_url"], "https://legacy/v1")
        self.assertEqual(state["model"], "old")
        # known preset -> registry endpoint + stored models; curated defaults if empty
        state = providers.initial_state({"provider": "xiaomi-mimo",
                                         "provider_variant": "token-plan-ams",
                                         "model": "", "transcription_model": "",
                                         "api_key": "tp-x"})
        self.assertEqual(state["provider"], "xiaomi-mimo")
        self.assertEqual(state["provider_variant"], "token-plan-ams")
        self.assertEqual(state["provider_base_url"], MIMO_VARIANT_URLS["token-plan-ams"])
        self.assertEqual(state["model"], "mimo-v2.6-flash")
        self.assertEqual(state["transcription_model"], "mimo-v2.5-asr")
        # non-str api_key degrades to ""
        self.assertEqual(providers.initial_state({"api_key": 42})["api_key"], "")


class ExactSurfaceAndPurityTests(unittest.TestCase):
    # R-13: exactly ONE registry surface; superseded proposal names stay absent.
    EXPECTED_PUBLIC = {
        "CUSTOM_PROVIDER_ID", "DEFAULT_PROVIDER_ID", "MODE_SINGLE", "MODE_SPLIT",
        "DIALECT_OPENAI", "DIALECT_MIMO_ASR",
        "EndpointVariant", "ProviderPreset",
        "list_providers", "provider_choices", "get_provider", "is_known_provider",
        "variant_options", "model_field_mode",
        "resolve_provider_id", "resolve_variant_id", "resolve_base_url",
        "resolve_transcription_model", "dialect_for_preset", "dialect_for",
        "normalize_config", "validate_provider_selection",
        "display_names", "provider_id_from_display", "needs_api_key", "key_hint",
        "initial_state", "save_payload",
    }
    # Names struck by rulings R1/R2 + A3 R.4 (would mean a second registry).
    SUPERSEDED_ABSENT = (
        "preset_for", "CUSTOM_PRESET", "PROVIDERS", "MIMO_VARIANTS",
        "transcription_dialect", "max_audio_bytes", "language_hint",
        "fetch_models_enabled", "resolve_post_process_model", "post_process_model",
        "provider_presets", "PROVIDER_PRESETS",
    )
    # Import artifacts that are legitimately present in the module namespace.
    IMPORT_ARTIFACTS = {"dataclass", "Literal", "Mapping", "Optional", "Tuple"}

    def test_public_surface_is_exactly_the_contract(self):
        public = {name for name in vars(providers) if not name.startswith("_")}
        public -= self.IMPORT_ARTIFACTS
        self.assertEqual(public, self.EXPECTED_PUBLIC)

    def test_superseded_names_are_absent(self):
        for name in self.SUPERSEDED_ABSENT:
            with self.subTest(name=name):
                self.assertFalse(hasattr(providers, name),
                                 f"{name} must not exist (second registry / struck key)")

    def test_ast_purity_guard_imports_only_dataclasses_typing(self):
        # R-3 Option D (Revision Log): providers.normalize_config DELEGATES the
        # M13-M23/K-rule normalization to `dictation_config`, a NEW pure module
        # that itself imports NOTHING (zero-import stdlib-free leaf), so the
        # allowlist widens by exactly that one app module. Everything else stays
        # forbidden: I/O modules, tkinter, urllib, and any other app module
        # (which would break the acyclic, side-effect-free providers leaf).
        path = REPO_DIR / "providers.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        allowed = {"dataclasses", "typing", "dictation_config"}
        self.assertTrue(imported <= allowed,
                        f"providers.py imports forbidden modules: {sorted(imported - allowed)}")
        # No I/O, printing, or dynamic-import calls either.
        called = {node.func.id for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        forbidden = {"open", "print", "exec", "eval", "__import__", "input"}
        self.assertFalse(forbidden & called,
                         f"providers.py calls forbidden builtins: {sorted(forbidden & called)}")

    def test_llm_payloads_ast_purity(self):
        path = REPO_DIR / "llm_payloads.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertTrue(imported <= {"typing"},
                        f"llm_payloads.py imports forbidden modules: {sorted(imported)}")
        called = {node.func.id for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.assertFalse({"open", "print", "exec", "eval"} & called)

    def test_importing_providers_creates_no_tk_root(self):
        # R-12 side-effect guard (AST already forbids the tkinter import; this
        # pins the observable behaviour through a fresh interpreter).
        import subprocess
        import sys

        try:
            import tkinter  # noqa: F401
        except ImportError:
            self.skipTest("tkinter is not installed in this Python build")
        result = subprocess.run(
            [sys.executable, "-c",
             "import tkinter as tk; import providers; "
             "assert tk._default_root is None, 'providers created a Tk root'"],
            cwd=str(REPO_DIR), capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
