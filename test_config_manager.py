import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config_manager


class _EnvHarness(unittest.TestCase):
    """Shared temp-config environment (paths/env patched per test)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.local = self.root / "local"
        self.source = self.root / "source"
        self.exe = self.root / "exe"
        self.cwd = self.root / "cwd"
        for directory in (self.local, self.source, self.exe, self.cwd):
            directory.mkdir()
        self.env = patch.dict(
            os.environ,
            {"LOCALAPPDATA": str(self.local), "XDG_CONFIG_HOME": str(self.local)},
            clear=False,
        )
        self.env.start()
        self.paths = patch.object(config_manager, "__file__", str(self.source / "config_manager.py"))
        self.paths.start()
        self.executable = patch.object(sys, "executable", str(self.exe / "app.exe"))
        self.executable.start()
        self.cwd_patch = patch("config_manager.os.getcwd", return_value=str(self.cwd))
        self.cwd_patch.start()

    def tearDown(self):
        self.cwd_patch.stop()
        self.executable.stop()
        self.paths.stop()
        self.env.stop()
        self.temp.cleanup()


class ConfigManagerTests(_EnvHarness):

    def test_path_does_not_follow_cwd(self):
        config_manager.save_config({"api_key": "secret"})
        first = config_manager.get_config_path()
        with patch("config_manager.os.getcwd", return_value=str(self.root / "elsewhere")):
            self.assertEqual(config_manager.get_config_path(), first)
            self.assertEqual(config_manager.load_config()["api_key"], "secret")

    def test_roundtrip_never_writes_raw_api_key(self):
        config_manager.save_config({"api_key": "secret-value", "theme": "light"})
        raw = config_manager.get_config_path().read_text(encoding="utf-8")
        self.assertNotIn("secret-value", raw)
        self.assertEqual(config_manager.load_config()["api_key"], "secret-value")

    def test_provider_settings_roundtrip(self):
        config_manager.save_config({
            "api_key": "secret-value",
            "provider_base_url": "https://api.example.com/v1",
            "model": "custom-model",
        })
        loaded = config_manager.load_config()
        self.assertEqual(loaded["provider_base_url"], "https://api.example.com/v1")
        self.assertEqual(loaded["model"], "custom-model")
        self.assertEqual(loaded["api_key"], "secret-value")

    def test_defaults_include_empty_provider_settings(self):
        self.assertEqual(config_manager.DEFAULT_CONFIG["provider_base_url"], "")
        self.assertEqual(config_manager.DEFAULT_CONFIG["model"], "")

    def test_defaults_include_preset_keys(self):
        # T-2026-11-16-provider-presets: 3 additive keys (plan 2.1).
        self.assertEqual(config_manager.DEFAULT_CONFIG["provider"], "custom")
        self.assertEqual(config_manager.DEFAULT_CONFIG["provider_variant"], "")
        self.assertEqual(config_manager.DEFAULT_CONFIG["transcription_model"], "")

    def test_legacy_file_without_preset_keys_loads_byte_identical(self):
        # M1: a pre-preset config (no provider keys at all) loads as Custom
        # with every stored value byte-identical - no URL inference, no model
        # rewrite, no new-key surprises beyond the 3 managed defaults.
        target = config_manager.get_config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        legacy = {
            "api_key_protected": "base64:" + base64.b64encode(
                b"legacy-secret").decode("ascii"),
            "provider_base_url": "https://legacy.example/v1",
            "model": "legacy-model",
            "target_language": "Arabic",
            "theme": "light",
            "shortcut": "ctrl+alt+s",
        }
        target.write_text(json.dumps(legacy), encoding="utf-8")

        loaded = config_manager.load_config()
        self.assertEqual(loaded["provider"], "custom")
        self.assertEqual(loaded["provider_base_url"], "https://legacy.example/v1")
        self.assertEqual(loaded["model"], "legacy-model")
        self.assertEqual(loaded["provider_variant"], "")
        self.assertEqual(loaded["transcription_model"], "")
        # M12: every pre-existing key round-trips untouched.
        for key in ("target_language", "theme", "shortcut"):
            self.assertEqual(loaded[key], legacy[key])
        self.assertEqual(loaded["api_key"], "legacy-secret")

    def test_load_normalizes_preset_provider_and_wins_over_stored_url(self):
        # M5/M7 at the load path: a hand-edited file gets the registry variant
        # default and the canonical registry URL (stored value discarded).
        target = config_manager.get_config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({
            "provider": "deepseek",
            "provider_variant": "bogus",
            "provider_base_url": "https://stale.example/v1",
            "model": "deepseek-chat",
        }), encoding="utf-8")

        loaded = config_manager.load_config()
        self.assertEqual(loaded["provider"], "deepseek")
        self.assertEqual(loaded["provider_variant"], "")
        self.assertEqual(loaded["provider_base_url"], "https://api.deepseek.com")
        self.assertEqual(loaded["model"], "deepseek-chat")

    def test_load_fills_mimo_split_defaults_and_clears_single_strays(self):
        target = config_manager.get_config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        # split preset with empty models -> curated defaults (M8/M9)
        target.write_text(json.dumps({
            "provider": "xiaomi-mimo",
            "provider_variant": "token-plan-cn",
            "provider_base_url": "",
            "model": "",
            "transcription_model": "",
        }), encoding="utf-8")
        loaded = config_manager.load_config()
        self.assertEqual(loaded["provider_base_url"],
                         "https://token-plan-cn.xiaomimimo.com/v1")
        self.assertEqual(loaded["model"], "mimo-v2.6-flash")
        self.assertEqual(loaded["transcription_model"], "mimo-v2.5-asr")

        # single preset with a stray stage-1 value -> cleared (M11, R3 IFF)
        target.write_text(json.dumps({
            "provider": "groq",
            "provider_base_url": "https://api.groq.com/openai/v1",
            "model": "llama-3.3-70b-versatile",
            "transcription_model": "mimo-v2.5-asr",
        }), encoding="utf-8")
        loaded = config_manager.load_config()
        self.assertEqual(loaded["transcription_model"], "")
        self.assertEqual(loaded["model"], "llama-3.3-70b-versatile")

    def test_protected_api_key_survives_provider_normalization(self):
        # DPAPI/base64 handling and the providers hook compose: the key is
        # decrypted BEFORE normalization and never leaks into provider keys.
        def fake_dpapi(protect, value):
            wrapped = b"wrapped:"
            raw = value.encode("utf-8") if isinstance(value, str) else value
            return wrapped + raw if protect else raw[len(wrapped):]

        with patch.object(config_manager, "_IS_WINDOWS", True), patch.object(
            config_manager, "_windows_dpapi", side_effect=fake_dpapi
        ):
            config_manager.save_config({
                "api_key": "dpapi-secret",
                "provider": "xiaomi-mimo",
                "provider_variant": "token-plan-ams",
                "provider_base_url": "https://stale.example/v1",
                "model": "",
                "transcription_model": "",
            })
            raw = config_manager.get_config_path().read_text(encoding="utf-8")
            self.assertIn('"api_key_protected": "dpapi:', raw)
            self.assertNotIn("dpapi-secret", raw)
            loaded = config_manager.load_config()

        self.assertEqual(loaded["api_key"], "dpapi-secret")
        self.assertEqual(loaded["provider"], "xiaomi-mimo")
        self.assertEqual(loaded["provider_variant"], "token-plan-ams")
        self.assertEqual(loaded["provider_base_url"],
                         "https://token-plan-ams.xiaomimimo.com/v1")
        self.assertEqual(loaded["model"], "mimo-v2.6-flash")
        self.assertEqual(loaded["transcription_model"], "mimo-v2.5-asr")

    def test_windows_dpapi_roundtrip_format(self):
        def fake_dpapi(protect, value):
            wrapped = b"wrapped:"
            raw = value.encode("utf-8") if isinstance(value, str) else value
            return wrapped + raw if protect else raw[len(wrapped):]

        with patch.object(config_manager, "_IS_WINDOWS", True), patch.object(
            config_manager, "_windows_dpapi", side_effect=fake_dpapi
        ):
            config_manager.save_config({"api_key": "dpapi-secret"})
            target = self.local / "WhisperLive" / "config.json"
            self.assertEqual(config_manager.get_config_path(), target)
            self.assertTrue(target.exists())
            raw = target.read_text(encoding="utf-8")
            self.assertIn('"api_key_protected": "dpapi:', raw)
            self.assertNotIn("dpapi-secret", raw)
            self.assertEqual(config_manager.load_config()["api_key"], "dpapi-secret")

    def test_migration_order_and_idempotence(self):
        (self.exe / "config.json").write_text(json.dumps({"theme": "exe"}), encoding="utf-8")
        (self.source / "config.json").write_text(json.dumps({"theme": "source"}), encoding="utf-8")
        (self.cwd / "config.json").write_text(json.dumps({"theme": "cwd"}), encoding="utf-8")
        self.assertEqual(config_manager.load_config()["theme"], "exe")
        target = config_manager.get_config_path()
        target.write_text(target.read_text(encoding="utf-8").replace('"exe"', '"new"'), encoding="utf-8")
        self.assertEqual(config_manager.load_config()["theme"], "new")

    def test_plaintext_canonical_config_is_rewritten_and_idempotent(self):
        target = config_manager.get_config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"api_key": "legacy-secret", "theme": "light"}), encoding="utf-8")

        self.assertEqual(config_manager.load_config()["api_key"], "legacy-secret")
        rewritten = target.read_text(encoding="utf-8")
        self.assertNotIn("legacy-secret", rewritten)
        self.assertIn("api_key_protected", rewritten)
        first_rewrite = rewritten

        self.assertEqual(config_manager.load_config()["api_key"], "legacy-secret")
        self.assertEqual(target.read_text(encoding="utf-8"), first_rewrite)

    def test_plaintext_canonical_rewrite_failure_is_reported(self):
        target = config_manager.get_config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"api_key": "legacy-secret"}), encoding="utf-8")
        with patch.object(config_manager, "save_config", return_value=False) as save, patch.object(
            config_manager.logger, "warning"
        ) as warning:
            self.assertEqual(config_manager.load_config()["api_key"], "legacy-secret")
        save.assert_called_once()
        warning.assert_called_once()
        self.assertIn("legacy-secret", target.read_text(encoding="utf-8"))

    def test_legacy_migration_dpapi_failure_keeps_legacy_without_target(self):
        legacy = self.exe / "config.json"
        legacy.write_text(json.dumps({"api_key": "legacy-secret"}), encoding="utf-8")
        with patch.object(config_manager, "_IS_WINDOWS", True), patch.object(
            config_manager, "_windows_dpapi", return_value=None
        ), patch.object(config_manager.logger, "warning"):
            loaded = config_manager.load_config()

        self.assertEqual(loaded["api_key"], "legacy-secret")
        self.assertFalse(config_manager.get_config_path().exists())
        self.assertIn("legacy-secret", legacy.read_text(encoding="utf-8"))

    def test_legacy_migration_atomic_failure_keeps_legacy_without_target(self):
        legacy = self.exe / "config.json"
        legacy.write_text(json.dumps({"api_key": "legacy-secret"}), encoding="utf-8")
        with patch.object(config_manager, "_atomic_write", side_effect=OSError("disk full")), patch.object(
            config_manager.logger, "warning"
        ):
            loaded = config_manager.load_config()

        self.assertEqual(loaded["api_key"], "legacy-secret")
        self.assertFalse(config_manager.get_config_path().exists())
        self.assertIn("legacy-secret", legacy.read_text(encoding="utf-8"))

    def test_decryption_failure_is_safe_and_reported(self):
        target = config_manager.get_config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"api_key_protected": "dpapi:not-valid"}), encoding="utf-8")
        with patch.object(config_manager, "_windows_dpapi", return_value=None), patch.object(
            config_manager.logger, "warning"
        ) as warning:
            loaded = config_manager.load_config()

        self.assertEqual(loaded["api_key"], "")
        warning.assert_called_once()

    def test_defaults_and_corrupt_json_are_safe(self):
        target = config_manager.get_config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("not json", encoding="utf-8")
        loaded = config_manager.load_config()
        self.assertEqual(loaded, config_manager.DEFAULT_CONFIG)
        self.assertEqual(loaded["shortcut"], "ctrl+space")

    def test_atomic_save_uses_replace_and_leaves_no_temp_file(self):
        with patch("config_manager.os.replace", wraps=os.replace) as replace:
            self.assertTrue(config_manager.save_config({"api_key": "atomic"}))
            replace.assert_called_once()
        self.assertEqual(list(config_manager.get_config_path().parent.glob(".config.json.*")), [])


class DictationKeyNormalizationTests(_EnvHarness):
    """M13-M23 at the CONFIG boundary (plan 2.3): every load normalizes the
    dictation keys to fail-closed defaults while unknown keys round-trip."""

    def _write(self, payload):
        target = config_manager.get_config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload), encoding="utf-8")
        return target

    def test_garbage_dictation_values_load_as_shipped_defaults(self):
        self._write({
            "dictation_mode": "bogus", "cleanup_mode": "", "model_size": 42,
            "live_engine": "gpt-x", "polish_mode": " insert ",
            "chunked_engine": "unknown-engine", "chunked_asr_model": 7,
            "cleanup_model": None, "model_store_dir": [],
            "gemini_consent": "yes", "gemini_paid_key_confirmed": 1,
            "first_run_guidance_seen": "true",
            "local_models": {"fw": "downloading"},       # crash residue -> dropped (M21)
            "quota_limits": {"groq-whisper.day": -5, "x": True},
        })
        loaded = config_manager.load_config()
        self.assertEqual(loaded["dictation_mode"], "local")       # U4
        self.assertEqual(loaded["cleanup_mode"], "cloud")         # D2
        self.assertEqual(loaded["model_size"], "auto")
        self.assertEqual(loaded["live_engine"], "local")
        self.assertEqual(loaded["polish_mode"], "insert")         # U3
        self.assertEqual(loaded["chunked_engine"], "")            # never dangling
        self.assertEqual(loaded["chunked_asr_model"], "")
        self.assertIs(loaded["gemini_consent"], False)            # literal True only
        self.assertIs(loaded["gemini_paid_key_confirmed"], False)
        self.assertIs(loaded["first_run_guidance_seen"], False)
        self.assertEqual(loaded["local_models"], {})
        self.assertEqual(loaded["quota_limits"], {})

    def test_valid_dictation_values_pass_through_byte_identical(self):
        self._write({
            "dictation_mode": "live", "cleanup_mode": "local-ollama",
            "model_size": "standard", "live_engine": "gemini-live",
            "polish_mode": "replace-experimental", "chunked_engine": "groq-whisper",
            "chunked_asr_model": "whisper-large-v3", "cleanup_model": "qwen3",
            "model_store_dir": "/srv/models",
            "gemini_consent": True, "gemini_paid_key_confirmed": False,
            "first_run_guidance_seen": True,
            "local_models": {"faster-whisper-base": "ready"},
            "quota_limits": {"groq-whisper.day": 500},
        })
        loaded = config_manager.load_config()
        for key, expected in {
                "dictation_mode": "live", "cleanup_mode": "local-ollama",
                "model_size": "standard", "live_engine": "gemini-live",
                "polish_mode": "replace-experimental",
                "chunked_engine": "groq-whisper",
                "chunked_asr_model": "whisper-large-v3",
                "cleanup_model": "qwen3", "model_store_dir": "/srv/models",
        }.items():
            with self.subTest(key=key):
                self.assertEqual(loaded[key], expected)
        self.assertIs(loaded["gemini_consent"], True)
        self.assertIs(loaded["first_run_guidance_seen"], True)
        self.assertEqual(loaded["quota_limits"], {"groq-whisper.day": 500})

    def test_defaults_are_created_with_fresh_containers_per_load(self):
        loaded = config_manager.load_config()
        self.assertEqual(loaded["local_models"], {})
        self.assertEqual(loaded["quota_limits"], {})
        loaded["local_models"]["fw"] = "ready"
        loaded["quota_limits"]["k"] = 1
        self.assertEqual(config_manager.DEFAULT_CONFIG["local_models"], {})
        self.assertEqual(config_manager.DEFAULT_CONFIG["quota_limits"], {})
        again = config_manager.load_config()
        self.assertEqual(again["local_models"], {})
        self.assertEqual(again["quota_limits"], {})


class SecretMapBoundaryTests(_EnvHarness):
    """K1-K8 at the config boundary, incl. G5 DPAPI/base64 evidence."""

    def _write(self, payload):
        target = config_manager.get_config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload), encoding="utf-8")
        return target

    def test_k4_dual_write_roundtrip_and_per_vendor_slots(self):
        self.assertTrue(config_manager.save_config(
            {"api_key": "sk-alpha", "provider": "openai", "model": "m"}))
        raw = config_manager.get_config_path().read_text(encoding="utf-8")
        self.assertNotIn("sk-alpha", raw)                       # never plaintext
        data = json.loads(raw)
        self.assertIn("api_key_protected", data)                # legacy blob (K4)
        self.assertIn("api_keys_protected", data)               # vendor map
        self.assertEqual(set(data["api_keys_protected"]), {"openai"})
        loaded = config_manager.load_config()
        self.assertEqual(loaded["api_key"], "sk-alpha")
        self.assertEqual(config_manager.get_vendor_key(loaded, "openai"), "sk-alpha")
        self.assertEqual(config_manager.get_vendor_key(loaded, "groq"), "")

    def test_k2_legacy_migrates_under_the_current_provider_only(self):
        # K7 misattribution guard: legacy api_key + provider=openai lands in
        # the OPENAI slot ONLY — never groq/custom/other vendors.
        target = self._write({"api_key": "legacy-1", "provider": "openai",
                              "model": "m"})
        loaded = config_manager.load_config()
        self.assertEqual(loaded["api_key"], "legacy-1")
        self.assertEqual(set(loaded["api_keys_protected"]), {"openai"})
        self.assertNotIn("groq", loaded["api_keys_protected"])
        # K8: rewritten once; the second load is byte-stable (idempotent)
        first = target.read_text(encoding="utf-8")
        self.assertNotIn("legacy-1", first)
        self.assertEqual(config_manager.load_config()["api_key"], "legacy-1")
        self.assertEqual(target.read_text(encoding="utf-8"), first)

    def test_t11_provider_switch_derivation_k7(self):
        # A3 T11: set_vendor_key A -> switch to B (empty slot) => derived
        # api_key == "" (never vendor A's key); switch back => A restored.
        # The per-switch key_var REHYDRATION (K7 dialog half) is pinned in
        # test_settings_view; this pins the config-boundary mechanics it drives.
        config_manager.save_config({"provider": "openai", "model": "m",
                                    "api_key": "sk-A"})
        cfg = config_manager.load_config()
        self.assertEqual(cfg["api_key"], "sk-A")
        self.assertEqual(set(cfg["api_keys_protected"]), {"openai"})

        cfg["provider"] = "groq"                                # switch to B
        cfg["api_key"] = config_manager.get_vendor_key(cfg, "groq")  # K7 (b)
        self.assertEqual(cfg["api_key"], "")                    # never A's key
        config_manager.save_config(cfg)
        cfg = config_manager.load_config()
        self.assertEqual(cfg["api_key"], "")                    # derived from B's slot
        self.assertEqual(config_manager.get_vendor_key(cfg, "openai"), "sk-A")

        cfg["provider"] = "openai"                              # switch back
        cfg["api_key"] = config_manager.get_vendor_key(cfg, "openai")
        config_manager.save_config(cfg)
        cfg = config_manager.load_config()
        self.assertEqual(cfg["api_key"], "sk-A")                # restored

    def test_k7_vendor_helpers_at_the_boundary(self):
        cfg = config_manager.load_config()
        self.assertTrue(config_manager.set_vendor_key(cfg, "groq", "gsk-1"))
        self.assertEqual(config_manager.get_vendor_key(cfg, "groq"), "gsk-1")
        # current provider is custom -> derived api_key untouched by groq write
        self.assertEqual(cfg["api_key"], "")
        self.assertFalse(config_manager.set_vendor_key(cfg, "", "x"))
        # corrupt slot fails only itself; others stay intact
        cfg["api_keys_protected"] = {"openai": "dpapi:garbage",
                                     "groq": config_manager._protect_api_key("gsk-1")}
        with patch.object(config_manager.logger, "warning"):
            self.assertEqual(config_manager.get_vendor_key(cfg, "openai"), "")
        self.assertEqual(config_manager.get_vendor_key(cfg, "groq"), "gsk-1")

    def test_g5_windows_dpapi_map_roundtrip_never_touches_plaintext(self):
        def fake_dpapi(protect, value):
            wrapped = b"wrapped:"
            raw = value.encode("utf-8") if isinstance(value, str) else value
            return wrapped + raw if protect else raw[len(wrapped):]
        with patch.object(config_manager, "_IS_WINDOWS", True), patch.object(
                config_manager, "_windows_dpapi", side_effect=fake_dpapi):
            config_manager.save_config({"api_key": "dpapi-secret",
                                        "provider": "openai", "model": "m"})
            raw = config_manager.get_config_path().read_text(encoding="utf-8")
            self.assertNotIn("dpapi-secret", raw)
            data = json.loads(raw)
            for envelope in [data["api_key_protected"],
                             *data["api_keys_protected"].values()]:
                self.assertTrue(envelope.startswith("dpapi:"))
            loaded = config_manager.load_config()
            self.assertEqual(loaded["api_key"], "dpapi-secret")
            self.assertEqual(config_manager.get_vendor_key(loaded, "openai"),
                             "dpapi-secret")

    def test_g5_base64_fallback_warns_count_only_without_key_bytes(self):
        # D9: non-Windows fallback -> ONE aggregated count warning per load,
        # NEVER key material in the log record (G5 log-scrape evidence).
        envelope = "base64:" + base64.b64encode(b"secret-linux-key").decode("ascii")
        self._write({"api_keys_protected": {"openai": envelope, "groq": envelope}})
        with patch.object(config_manager, "_IS_WINDOWS", False), patch.object(
                config_manager.logger, "warning") as warning:
            loaded = config_manager.load_config()
        self.assertEqual(warning.call_count, 1)
        text = warning.call_args[0][0] % warning.call_args[0][1:]
        self.assertIn("2", text)                                # count only
        self.assertIn("base64", text)
        self.assertIn("obfuscation only", text)
        self.assertNotIn("secret-linux-key", text)              # never key bytes
        self.assertEqual(config_manager.get_vendor_key(loaded, "openai"),
                         "secret-linux-key")                    # still usable


if __name__ == "__main__":
    unittest.main()
