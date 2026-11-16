"""Thin split-smoke tests for the merged main.py module split (ui proposal
sections b.1/b.2/e.3 + reconciliation row 7): importability, shared COLORS
identity, theme token preservation, startup.build_startup_command (frozen vs
script), and the no-`main`-import acyclicity rule for the FINAL module set.
"""

import ast
import os
import sys
import unittest
from unittest import mock

import indicator
import main
import settings_view
import startup
import themes
import tray
import views


class SplitModuleSmokeTests(unittest.TestCase):
    def test_split_modules_expose_expected_symbols(self):
        self.assertTrue(issubclass(main.WhisperLiveApp, views.MainUiMixin))
        # row 7: the settings dialog is a second mixin on the app class
        self.assertTrue(issubclass(main.WhisperLiveApp, settings_view.SettingsDialogMixin))
        self.assertTrue(hasattr(indicator, "RecordingIndicator"))
        self.assertTrue(callable(tray.create_tray_icon))
        self.assertTrue(callable(startup.set_startup_registration))
        self.assertTrue(callable(startup.build_startup_command))
        # row 7: cross-platform startup module API (Linux XDG autostart)
        self.assertTrue(callable(startup.set_linux_startup_registration))
        self.assertTrue(callable(startup._linux_autostart_dir))
        # tray DISPLAY_NAME intentionally distinct from startup.APP_NAME
        self.assertIsInstance(tray.DISPLAY_NAME, str)
        self.assertIsInstance(startup.APP_NAME, str)

    def test_importing_split_modules_creates_no_ui_side_effects(self):
        # ui section f: no Tk root, tray icon or registry write may happen at
        # import time - all creation stays inside WhisperLiveApp.__init__.
        try:
            import tkinter as tk
        except ImportError:  # platform honesty: a Python without Tk cannot assert this
            self.skipTest("tkinter is not installed in this Python build")
        self.assertIsNone(tk._default_root, "importing the split modules must not build a Tk root")

    def test_themes_token_sets_and_shared_colors_preserved(self):
        self.assertIn("dark", themes.THEMES)
        self.assertIn("light", themes.THEMES)
        self.assertEqual(set(themes.THEMES["dark"]), set(themes.THEMES["light"]))
        # baseline pre-split main.py carried 19 tokens per theme (verified vs
        # git HEAD); ui proposal's "21 keys" count was a documentation error.
        self.assertEqual(len(themes.THEMES["dark"]), 19)
        # ONE shared mutable COLORS dict; no module may rebind/copy it.
        self.assertIsInstance(themes.COLORS, dict)
        self.assertIs(main.COLORS, themes.COLORS)
        self.assertIs(views.COLORS, themes.COLORS)
        self.assertIs(indicator.COLORS, themes.COLORS)
        # row 7: settings_view joined the COLORS-sharing set in the re-split
        self.assertIs(settings_view.COLORS, themes.COLORS)

    def test_no_split_module_imports_main(self):
        # ui section b.2 import-graph rule: main -> {themes, startup, views,
        # indicator, tray, app_logging, settings_view, single_instance, ...},
        # never the reverse (acyclic). row 7: settings_view + single_instance
        # extended into the no-main-import list.
        root_dir = os.path.dirname(os.path.abspath(__file__))
        # row 7 + provider-presets: settings_view/single_instance joined, then
        # the two pure registry/wire modules (acyclic: neither imports main);
        # dictation-modes: EVERY new production module joins the set (plan 2.3).
        modules = (
            "themes", "startup", "views", "indicator", "tray", "app_logging",
            "settings_view", "single_instance", "providers", "llm_payloads",
            # --- dictation-modes (T-2026-11-16) ---
            "dictation_config", "dictation_types", "llm_errors", "quota_ledger",
            "model_manifest", "session_state", "dictation_session", "pcm_stream",
            "vad_segmenter", "local_engine", "chunked_engine", "whisper_http",
            "typer_actor", "focus_guard", "polish_engine", "model_store",
            "live_local_engine", "live_cloud_engine",
            "settings_provider_view", "settings_keys_view",
            "settings_dictation_view", "settings_models_view",
            "session_dialogs", "smoke",
            # --- F-2 Option A: the pure consent leaf (acyclic: no main import) ---
            "gemini_gate",
        )
        for module in modules:
            path = os.path.join(root_dir, module + ".py")
            with self.subTest(module=module):
                self.assertTrue(os.path.exists(path), f"{module}.py is missing")
                with open(path, encoding="utf-8") as handle:
                    tree = ast.parse(handle.read(), filename=path)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        names = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        names = [node.module or ""]
                    else:
                        continue
                    for name in names:
                        self.assertNotEqual(
                            name.split(".")[0], "main",
                            f"{module}.py must not import main",
                        )


def _fixture_abs_path(*parts: str, drive: str = "C") -> str:
    """Absolute fake path built with the HOST OS's root + separator semantics.

    build_startup_command() normalizes sys.executable through
    os.path.abspath(), which is a no-op for native absolute paths but prefixes
    the CWD onto foreign (e.g. Windows `C:\\...`) paths on POSIX. Fixtures are
    therefore built natively (drive letter + `\\` on nt, `/` + `os.path.join`
    parts on POSIX) so the quoted-command contract is asserted EXACTLY on both
    the windows-latest and ubuntu-latest CI legs — no regex/substring loosening.
    """
    root = f"{drive}:{os.sep}" if os.name == "nt" else os.sep
    return os.path.join(root, *parts)


class StartupCommandTests(unittest.TestCase):
    def test_build_startup_command_frozen(self):
        executable = _fixture_abs_path("Program Files", "Whisper Live", "WhisperLive.exe")
        with mock.patch.object(sys, "executable", executable), \
                mock.patch.object(sys, "frozen", True, create=True):
            command = startup.build_startup_command()

        # strict contract: single executable, whole path double-quoted (spaces intact)
        self.assertEqual(command, f'"{executable}"')

    def test_build_startup_command_script_mode(self):
        executable = _fixture_abs_path("Python 3", "python.exe")
        script = _fixture_abs_path("projects", "wisperlive", "main.py", drive="H")
        with mock.patch.object(sys, "executable", executable), \
                mock.patch.object(sys, "frozen", False, create=True), \
                mock.patch.object(startup, "SCRIPT_PATH", script):
            command = startup.build_startup_command()

        # strict contract: `"<exe>" "<script>"`, both quoted, exactly one space between
        self.assertEqual(command, f'"{executable}" "{script}"')

    def test_script_path_points_at_repo_main_py(self):
        # SCRIPT_PATH is a module global read at call time (patchable in tests).
        self.assertTrue(startup.SCRIPT_PATH.endswith("main.py"))
        self.assertTrue(os.path.isabs(startup.SCRIPT_PATH))


if __name__ == "__main__":
    unittest.main()
