"""Pure config-rule tests (plan 2.3 matrix): M13-M23 + K1-K8 cores.

Owner: qa-test-assurance.  ``dictation_config`` is the zero-import pure leaf
(orchestrator ruling R-3 Option D) that ``providers.normalize_config`` and
``config_manager`` both delegate to, so this suite pins the rules ONCE at
their implementation, while ``test_config_manager`` re-checks the same rules
at the config boundary (A3 T-matrix).

Covers: choice/fail-closed coercion (M13/M14/M15/M18), chunked-engine no-drift
(M16), string fail-closed (M17), exact-value polish (M19), literal-True booleans
(M20/M22), local-models readiness (M21), quota-limit overrides (M23), the M12
round-trip + M12b secret-envelope hands-off, idempotence, and the K1-K8 secret
cores incl. the K7 misattribution guard (K2 legacy files under the CURRENT
provider only).
"""

import ast
import unittest

import dictation_config as dc
import providers


class PurityTests(unittest.TestCase):
    def test_module_imports_nothing(self):
        # Zero-import leaf: no stdlib, no providers (that would be circular).
        with open(dc.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imports = [node for node in ast.walk(tree)
                   if isinstance(node, (ast.Import, ast.ImportFrom))]
        self.assertEqual(imports, [], "dictation_config must contain zero imports")


class ChoiceNormalizationTests(unittest.TestCase):
    """M13/M14/M15/M18: absent | non-str | blank | unknown -> default (N1)."""

    def test_dictation_mode_fail_closed_to_local(self):
        for bad in (None, 42, "", "  ", "bogus", "Local", ["local"]):
            with self.subTest(value=bad):
                result = dc.normalize_dictation_keys({"dictation_mode": bad})
                self.assertEqual(result["dictation_mode"], "local")
        for good in ("local", "chunked", "live"):
            self.assertEqual(
                dc.normalize_dictation_keys({"dictation_mode": good})["dictation_mode"],
                good)

    def test_cleanup_mode_fail_closed_to_cloud(self):
        for bad in (None, "", "Bogus", True, 0):
            with self.subTest(value=bad):
                self.assertEqual(
                    dc.normalize_dictation_keys({"cleanup_mode": bad})["cleanup_mode"],
                    "cloud")
        for good in ("cloud", "local-ollama", "off"):
            self.assertEqual(
                dc.normalize_dictation_keys({"cleanup_mode": good})["cleanup_mode"],
                good)

    def test_model_size_and_live_engine_choices(self):
        self.assertEqual(dc.normalize_dictation_keys({"model_size": "STANDARD"})["model_size"],
                         "auto")                              # case-sensitive exact match
        self.assertEqual(dc.normalize_dictation_keys({"model_size": " fast "})["model_size"],
                         "fast")                              # surrounding ws stripped
        for size in ("auto", "fast", "standard"):
            self.assertEqual(dc.normalize_dictation_keys({"model_size": size})["model_size"],
                             size)
        for engine in ("local", "gemini-live", "openai-realtime"):
            self.assertEqual(dc.normalize_dictation_keys({"live_engine": engine})["live_engine"],
                             engine)
        self.assertEqual(dc.normalize_dictation_keys({"live_engine": "gpt-4o"})["live_engine"],
                         "local")                             # Phase-1 fail-closed default

    def test_absent_keys_get_defaults_without_deleting_anything(self):
        result = dc.normalize_dictation_keys({})
        self.assertEqual(result["dictation_mode"], "local")
        self.assertEqual(result["cleanup_mode"], "cloud")
        self.assertEqual(result["model_size"], "auto")
        self.assertEqual(result["live_engine"], "local")
        self.assertEqual(result["polish_mode"], "insert")


class M16ChunkedEngineTests(unittest.TestCase):
    def test_empty_inherit_and_known_ids_pass_unknown_clears(self):
        presets = ("openai", "groq", "xiaomi-mimo", "ollama-local")
        cases = [
            ("", presets, ""),
            ("   ", presets, ""),                   # stripped blank -> ""
            (None, presets, ""),
            (42, presets, ""),
            ("groq-whisper", presets, "groq-whisper"),
            ("openai", presets, "openai"),
            ("xiaomi-mimo", presets, "xiaomi-mimo"),
            ("unknown-engine", presets, ""),        # NEVER a dangling id
            ("openai", (), ""),                     # empty preset set clears everything
        ]
        for value, preset_ids, expected in cases:
            with self.subTest(value=value):
                result = dc.normalize_dictation_keys({"chunked_engine": value},
                                                     preset_ids)
                self.assertEqual(result["chunked_engine"], expected)

    def test_chunked_engine_never_rewritten_to_provider(self):
        # The stored value must not DRIFT into "provider" (A3 s1.2), and the
        # M-rules never manage the M1-M12 provider keys themselves.
        result = dc.normalize_dictation_keys(
            {"chunked_engine": "groq-whisper", "provider": "openai"}, ("openai",))
        self.assertEqual(result["chunked_engine"], "groq-whisper")
        self.assertEqual(result["provider"], "openai")   # byte-identical passthrough


class M17StringKeysTests(unittest.TestCase):
    def test_non_string_values_fail_closed_to_empty(self):
        for key in ("chunked_asr_model", "cleanup_model", "model_store_dir"):
            for bad in (None, 42, [], {}, True):
                with self.subTest(key=key, value=bad):
                    self.assertEqual(
                        dc.normalize_dictation_keys({key: bad})[key], "")

    def test_valid_strings_pass_through_as_is_no_stripping(self):
        result = dc.normalize_dictation_keys(
            {"chunked_asr_model": "  model  ", "cleanup_model": "c", "model_store_dir": "/d"})
        self.assertEqual(result["chunked_asr_model"], "  model  ")
        self.assertEqual(result["cleanup_model"], "c")
        self.assertEqual(result["model_store_dir"], "/d")


class M19PolishAndM20M22BooleanTests(unittest.TestCase):
    def test_polish_mode_is_exact_value_only(self):
        self.assertEqual(dc.normalize_dictation_keys({"polish_mode": "insert"})["polish_mode"],
                         "insert")
        self.assertEqual(dc.normalize_dictation_keys(
            {"polish_mode": "replace-experimental"})["polish_mode"],
            "replace-experimental")
        for bad in (" insert ", "INSERT", "off", "replace", None, 1):
            with self.subTest(value=bad):
                self.assertEqual(
                    dc.normalize_dictation_keys({"polish_mode": bad})["polish_mode"],
                    "insert")                           # U3 default, no "off" value

    def test_boolean_flags_accept_literal_true_only(self):
        for key in ("gemini_consent", "gemini_paid_key_confirmed",
                    "first_run_guidance_seen"):
            for truthy in (True,):
                self.assertTrue(
                    dc.normalize_dictation_keys({key: truthy})[key])
            for falsy in (False, "yes", "true", 1, 0, None, [], {}, "1"):
                # NB: 1 == True is NOT `is True` — literal-True only (U2 fail-closed)
                self.assertIs(
                    dc.normalize_dictation_keys({key: falsy})[key], False,
                    f"{key} accepted non-literal {falsy!r}")
            self.assertIs(dc.normalize_dictation_keys({})[key], False)


class M21M23ContainerTests(unittest.TestCase):
    def test_local_models_filters_to_known_states_with_string_ids(self):
        self.assertEqual(dc.normalize_dictation_keys({})["local_models"], {})
        self.assertEqual(
            dc.normalize_dictation_keys({"local_models": "junk"})["local_models"], {})
        result = dc.normalize_dictation_keys({"local_models": {
            "faster-whisper-base": "ready",
            "faster-whisper-small": "stale",
            "absent-one": "absent",
            "crash-residue": "downloading",       # unknown state dropped
            42: "ready",                          # non-str id dropped
            "x": None,                            # non-str state dropped
        }})["local_models"]
        self.assertEqual(result, {"faster-whisper-base": "ready",
                                  "faster-whisper-small": "stale",
                                  "absent-one": "absent"})

    def test_quota_limits_filters_to_positive_ints(self):
        self.assertEqual(dc.normalize_dictation_keys({})["quota_limits"], {})
        self.assertEqual(
            dc.normalize_dictation_keys({"quota_limits": []})["quota_limits"], {})
        result = dc.normalize_dictation_keys({"quota_limits": {
            "groq-whisper.day": 500,
            "chat:openai.minute": 1,
            "bool": True,                          # bool is not an int limit
            "zero": 0,
            "negative": -5,
            "float": 1.5,
            "str": "100",
            42: 10,                                # non-str key dropped
        }})["quota_limits"]
        self.assertEqual(result, {"groq-whisper.day": 500, "chat:openai.minute": 1})


class M12RoundTripAndIdempotenceTests(unittest.TestCase):
    def test_unknown_keys_round_trip_untouched(self):
        source = {"theme": "dark", "shortcut": "ctrl+space", "unknown_future": {"a": 1},
                  "target_language": "Arabic", "api_key": "sk-A",
                  "api_key_protected": "dpapi:xxx", "installed_release_tag": "v9"}
        result = dc.normalize_dictation_keys(dict(source))
        for key, value in source.items():
            with self.subTest(key=key):
                self.assertEqual(result[key], value)

    def test_secret_envelope_is_hands_off_for_the_m_rules(self):
        # M12b: normalize_dictation_keys never reads/rewrites the envelope.
        source = {"api_key": "sk-A", "api_key_protected": "dpapi:x",
                  "api_keys_protected": {"openai": "dpapi:y"}}
        result = dc.normalize_dictation_keys(dict(source))
        self.assertEqual(result["api_key"], "sk-A")
        self.assertEqual(result["api_key_protected"], "dpapi:x")
        self.assertEqual(result["api_keys_protected"], {"openai": "dpapi:y"})

    def test_normalization_is_idempotent_and_never_raises(self):
        messy = {"dictation_mode": "bogus", "cleanup_mode": None, "polish_mode": "x",
                 "local_models": 5, "quota_limits": "x", "gemini_consent": "yes"}
        once = dc.normalize_dictation_keys(dict(messy))
        twice = dc.normalize_dictation_keys(dict(once))
        self.assertEqual(once, twice)
        for garbage in (None, "junk", 42, [], object()):
            with self.subTest(data=garbage):
                try:
                    result = dc.normalize_dictation_keys(garbage)
                except (TypeError, ValueError):        # non-dict sources may coerce-fail
                    continue
                self.assertIsInstance(result, dict)
                self.assertEqual(result["dictation_mode"], "local")

    def test_providers_normalize_config_delegates_m13_m23(self):
        # The A3 QA T1 target: providers.normalize_config applies the SAME cores.
        result = providers.normalize_config({
            "provider": "openai", "provider_base_url": "https://x/v1",
            "model": "m", "dictation_mode": "bogus", "cleanup_mode": "bogus",
            "polish_mode": "bogus", "gemini_consent": "yes",
        })
        self.assertEqual(result["dictation_mode"], "local")
        self.assertEqual(result["cleanup_mode"], "cloud")
        self.assertEqual(result["polish_mode"], "insert")
        self.assertIs(result["gemini_consent"], False)


class KSecretCoreTests(unittest.TestCase):
    """K1-K8 cores with INJECTED protect/unprotect callables (no I/O)."""

    @staticmethod
    def _protect(value):
        if isinstance(value, str) and value == "boom":
            raise RuntimeError("DPAPI unavailable")
        return "wrap:" + value

    @staticmethod
    def _unprotect(value):
        if not isinstance(value, str) or not value.startswith("wrap:"):
            return ""
        return value[5:]

    def test_k1_coerce_key_map_str_to_str_only(self):
        self.assertEqual(dc.coerce_key_map(
            {"openai": "sk", "groq": 5, 7: "x", "x": None}), {"openai": "sk"})
        for garbage in (None, [], "x", 5):
            self.assertEqual(dc.coerce_key_map(garbage), {})

    def test_base64_fallback_count_is_count_only(self):
        keys = {"openai": "base64:QQ==", "groq": "base64:Qg==", "x": "dpapi:xx"}
        self.assertEqual(dc.base64_fallback_count(keys), 2)
        self.assertEqual(dc.base64_fallback_count("junk"), 0)

    def test_k2_legacy_migrates_under_current_vendor_only(self):
        migrated = dc.migrate_legacy_key({}, "legacy-key", "openai", self._protect)
        self.assertEqual(migrated, {"openai": "wrap:legacy-key"})
        # K7 guard: the legacy value never lands under a DIFFERENT vendor
        self.assertNotIn("groq", migrated)
        self.assertNotIn("custom", migrated)
        # blank/absent legacy -> unchanged
        self.assertEqual(dc.migrate_legacy_key({}, "", "openai", self._protect), {})
        self.assertEqual(dc.migrate_legacy_key({}, None, "openai", self._protect), {})

    def test_k2_existing_slot_always_wins_idempotent(self):
        existing = {"openai": "wrap:MAP"}
        self.assertEqual(dc.migrate_legacy_key(existing, "legacy", "openai",
                                               self._protect), existing)
        # idempotent: second migration is a no-op
        once = dc.migrate_legacy_key({}, "legacy", "groq", self._protect)
        self.assertEqual(dc.migrate_legacy_key(once, "legacy", "groq", self._protect),
                         once)

    def test_k2_protect_failure_keeps_the_map_unchanged(self):
        # Windows DPAPI unavailable -> value stays in-memory legacy; no downgrade.
        before = {"groq": "wrap:ok"}
        self.assertEqual(
            dc.migrate_legacy_key(before, "boom", "openai", self._protect), before)

    def test_k3_derive_current_key_map_wins_and_missing_returns_none(self):
        keys = {"openai": "wrap:MAPKEY"}
        self.assertEqual(dc.derive_current_key(keys, "openai", self._unprotect), "MAPKEY")
        self.assertIsNone(dc.derive_current_key(keys, "groq", self._unprotect))
        self.assertIsNone(dc.derive_current_key("junk", "openai", self._unprotect))
        # a corrupt entry decrypts to "" (slot exists, value unusable)
        self.assertEqual(dc.derive_current_key({"openai": "garbage"}, "openai",
                                               self._unprotect), "")

    def test_k7_read_and_write_helpers(self):
        keys = {"openai": "wrap:sk", "groq": "wrap:gsk"}
        self.assertEqual(dc.read_vendor_key(keys, "openai", self._unprotect), "sk")
        self.assertEqual(dc.read_vendor_key(keys, "missing", self._unprotect), "")
        # an entry the decryptor cannot decode yields "" (fails only itself)
        self.assertEqual(dc.read_vendor_key(keys, "groq", lambda _v: ""), "")
        updated = dc.with_vendor_key(keys, "mistral", "msk", self._protect)
        self.assertEqual(updated["mistral"], "wrap:msk")
        self.assertEqual(updated["openai"], "wrap:sk")      # other slots intact
        self.assertIsNone(dc.with_vendor_key(keys, "", "x", self._protect))
        self.assertIsNone(dc.with_vendor_key(keys, "  ", "x", self._protect))
        self.assertIsNone(dc.with_vendor_key(keys, None, "x", self._protect))
        # non-str plaintext coerced to "" (never str()d into junk)
        blank = dc.with_vendor_key(keys, "mistral", 42, self._protect)
        self.assertEqual(blank["mistral"], self._protect(""))

    def test_k7_protect_failures_propagate_never_downgrade(self):
        with self.assertRaises(RuntimeError):
            dc.with_vendor_key({}, "openai", "boom", self._protect)

    def test_k4_dual_write_disk_map(self):
        keys = {"openai": "wrap:old", "groq": "wrap:gsk"}
        disk = dc.disk_key_map(keys, "mistral", "wrap:new")
        self.assertEqual(disk, {"openai": "wrap:old", "groq": "wrap:gsk",
                                "mistral": "wrap:new"})
        self.assertEqual(dc.disk_key_map(keys, "openai", "wrap:new")["openai"],
                         "wrap:new")
        # non-str blob/vendor leaves the map untouched; never writes plaintext
        self.assertEqual(dc.disk_key_map(keys, "x", None), keys)
        self.assertEqual(dc.disk_key_map(keys, None, "wrap:x"), keys)
        # a non-map input starts empty but STILL files the current vendor (K4)
        self.assertEqual(dc.disk_key_map("junk", "openai", "wrap:x"),
                         {"openai": "wrap:x"})
        self.assertEqual(dc.disk_key_map("junk", None, "wrap:x"), {})

    def test_k8_rewrite_needed_matrix(self):
        # plaintext legacy without map -> rewrite once
        self.assertTrue(dc.needs_secret_rewrite({"api_key": "sk"}))
        # pre-map protected file (no api_keys_protected) -> rewrite (backfill)
        self.assertTrue(dc.needs_secret_rewrite(
            {"api_key": "sk", "api_key_protected": "wrap:x"}))
        # fully migrated file -> no rewrite (idempotent second load)
        self.assertFalse(dc.needs_secret_rewrite(
            {"api_key": "sk", "api_key_protected": "wrap:x",
             "api_keys_protected": {"openai": "wrap:x"}}))
        # map present but plaintext key absent -> nothing to strip/rewrite
        self.assertFalse(dc.needs_secret_rewrite(
            {"api_keys_protected": {"openai": "wrap:x"}}))
        self.assertFalse(dc.needs_secret_rewrite({}))
        self.assertFalse(dc.needs_secret_rewrite("junk"))

    def test_k7_misattribution_guard_end_to_end(self):
        # A3 K7: the legacy api_key belongs to the CURRENT provider — it is
        # filed under that vendor ONLY, never spread to other vendors.
        keys = dc.migrate_legacy_key({}, "legacy-1", "openai", self._protect)
        self.assertEqual(set(keys), {"openai"})
        self.assertEqual(dc.read_vendor_key(keys, "openai", self._unprotect),
                         "legacy-1")
        # idempotent under the same current vendor: the slot wins, no rewrite
        self.assertEqual(dc.migrate_legacy_key(keys, "legacy-1", "openai",
                                               self._protect), keys)
        # when the CURRENT vendor changes (and has no slot), the legacy value is
        # filed under THAT vendor only — openai's slot is never touched and no
        # third vendor (custom) gains an entry from this call.
        relocated = dc.migrate_legacy_key(keys, "legacy-1", "groq", self._protect)
        self.assertEqual(set(relocated), {"openai", "groq"})
        self.assertEqual(relocated["openai"], keys["openai"])   # untouched
        self.assertNotIn("custom", relocated)


if __name__ == "__main__":
    unittest.main()
