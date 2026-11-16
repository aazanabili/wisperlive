"""Cross-cutting regression guards (core proposal section 8: updater exports +
zero print calls across the app modules).

Reconciliation (ui section c row 8): `APP_MODULES` refreshed to the FINAL
production module set - `gemini_api` -> `llm_api`, plus `settings_view`,
`single_instance`, and `linux_hotkey`.
"""

import ast
import os
import unittest

import updater

# Every production module in the merged `finish` tree (ui section c row 8),
# extended by T-2026-11-16-provider-presets with the two pure modules, and by
# T-2026-11-16-dictation-modes with EVERY new production module (plan 2.3).
APP_MODULES = (
    "main",
    "views",
    "settings_view",
    "themes",
    "startup",
    "indicator",
    "tray",
    "app_logging",
    "audio_recorder",
    "llm_api",
    "providers",
    "llm_payloads",
    "auto_typer",
    "windows_hotkey",
    "linux_hotkey",
    "single_instance",
    "updater",
    # --- dictation-modes (T-2026-11-16) ---
    "dictation_config",
    "dictation_types",
    "llm_errors",
    "quota_ledger",
    "model_manifest",
    "session_state",
    "dictation_session",
    "pcm_stream",
    "vad_segmenter",
    "local_engine",
    "chunked_engine",
    "whisper_http",
    "typer_actor",
    "focus_guard",
    "polish_engine",
    "model_store",
    "live_local_engine",
    "live_cloud_engine",
    "settings_provider_view",
    "settings_keys_view",
    "settings_dictation_view",
    "settings_models_view",
    "session_dialogs",
    "smoke",
    # --- F-2 Option A: the ONE Gemini consent rule (pure policy leaf) ---
    "gemini_gate",
)

# `smoke` is a CLI self-check entry point: printing its report IS its output
# contract (R-8c) — logging-only applies to the app modules.
PRINT_EXEMPT = ("smoke",)


class UpdaterTests(unittest.TestCase):
    # section 8 case 1: dead code (core section 4 deletion list) stays deleted
    def test_updater_exports_only_releases_page(self):
        from updater import RELEASES_PAGE  # noqa: F401 - import must keep working

        self.assertIn("github.com", RELEASES_PAGE)
        self.assertTrue(hasattr(updater, "REPOSITORY"))
        self.assertFalse(hasattr(updater, "get_latest_release"))
        self.assertFalse(hasattr(updater, "is_newer_version"))
        self.assertFalse(hasattr(updater, "UpdateError"))
        self.assertFalse(hasattr(updater, "RELEASES_API"))
        self.assertFalse(hasattr(updater, "USER_AGENT"))
        self.assertFalse(hasattr(updater, "json"))
        self.assertFalse(hasattr(updater, "urllib"))
        self.assertFalse(hasattr(updater, "re"))
        self.assertFalse(hasattr(updater, "os"))


class NoPrintCallTests(unittest.TestCase):
    # section 8 case 2: permanent ast guard - no print()/traceback.print_exc()
    # anywhere in the app modules (logging is the only output channel; the one
    # documented CLI exception lives in PRINT_EXEMPT).
    def test_no_print_calls_in_app_modules(self):
        root_dir = os.path.dirname(os.path.abspath(__file__))
        for module in APP_MODULES:
            path = os.path.join(root_dir, module + ".py")
            with self.subTest(module=module):
                self.assertTrue(os.path.exists(path), f"{module}.py is missing")
                with open(path, encoding="utf-8") as handle:
                    tree = ast.parse(handle.read(), filename=path)
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    if isinstance(node.func, ast.Name):
                        name = node.func.id
                    elif isinstance(node.func, ast.Attribute):
                        name = node.func.attr
                    else:
                        continue
                    if module in PRINT_EXEMPT:
                        continue                    # documented CLI output contract
                    self.assertNotIn(
                        name, ("print", "print_exc"),
                        f"{module}.py:{node.lineno} still calls {name}()",
                    )


class SecretsAndLogScrapeTests(unittest.TestCase):
    """G5 (static): D11 no-transcript-logging contract over EVERY new module —
    no logger call may reference transcript/key/audio-bearing names, and no
    f-string may be interpolated directly into a log call."""

    NEW_DICTATION_MODULES = (
        "dictation_config", "dictation_types", "llm_errors", "quota_ledger",
        "model_manifest", "session_state", "dictation_session", "pcm_stream",
        "vad_segmenter", "local_engine", "chunked_engine", "whisper_http",
        "typer_actor", "focus_guard", "polish_engine", "model_store",
        "live_local_engine", "live_cloud_engine", "settings_provider_view",
        "settings_keys_view", "settings_dictation_view", "settings_models_view",
        "session_dialogs", "smoke", "gemini_gate",
    )
    FORBIDDEN_ARG_NAMES = frozenset((
        "text", "transcript", "api_key", "key", "secret", "token", "pcm",
        "audio", "payload", "body", "frame", "words", "phrase", "clipboard",
    ))

    def test_no_logger_call_can_carry_transcript_or_key_material(self):
        root_dir = os.path.dirname(os.path.abspath(__file__))
        for module in self.NEW_DICTATION_MODULES:
            path = os.path.join(root_dir, module + ".py")
            with self.subTest(module=module):
                self.assertTrue(os.path.exists(path), f"{module}.py missing")
                with open(path, encoding="utf-8") as handle:
                    tree = ast.parse(handle.read(), filename=path)
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    func = node.func
                    is_logger = (isinstance(func, ast.Attribute)
                                 and isinstance(func.value, ast.Name)
                                 and func.value.id in ("logger", "logging"))
                    if not is_logger:
                        continue
                    for arg in list(node.args) + [kw.value for kw in node.keywords]:
                        for sub in ast.walk(arg):
                            if isinstance(sub, ast.Name):
                                self.assertNotIn(
                                    sub.id.lower(), self.FORBIDDEN_ARG_NAMES,
                                    f"{module}:{node.lineno} logger.{func.attr} "
                                    f"carries a risky name {sub.id!r}")
                            if isinstance(sub, ast.JoinedStr):
                                self.fail(
                                    f"{module}:{node.lineno} logger.{func.attr} "
                                    "interpolates an f-string (format templates "
                                    "only — %s style)")

    def test_consent_disclosure_copy_is_exact_and_gated(self):
        # G5: U2 consent copy present verbatim + fail-closed validation core.
        from settings_dictation_view import CONSENT_COPY, PAID_KEY_COPY, validate_save
        self.assertEqual(
            CONSENT_COPY,
            "I understand: cloud processing \u2014 Google may use my data for training.")
        self.assertIn("paid Google Cloud key", PAID_KEY_COPY)
        # gemini-live without consent OR paid attestation blocks SAVE (U2)
        blocking = validate_save({"live_engine": "gemini-live"},
                                 {"gemini_consent": False,
                                  "gemini_paid_key_confirmed": False})
        self.assertIn("Accept the Gemini notice", blocking)
        self.assertEqual(validate_save({"live_engine": "gemini-live"},
                                       {"gemini_consent": True}), "")
        self.assertEqual(validate_save({"live_engine": "gemini-live"},
                                       {"gemini_paid_key_confirmed": True}), "")
        self.assertEqual(validate_save({"live_engine": "local"},
                                       {"gemini_consent": False}), "")
        # U2 default OFF at the config boundary
        import config_manager
        self.assertIs(config_manager.DEFAULT_CONFIG["gemini_consent"], False)
        self.assertIs(config_manager.DEFAULT_CONFIG["gemini_paid_key_confirmed"],
                      False)


if __name__ == "__main__":
    unittest.main()
