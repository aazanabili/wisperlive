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
# extended by T-2026-11-16-provider-presets with the two pure modules.
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
)


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
    # anywhere in the app modules (logging is the only output channel).
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
                    self.assertNotIn(
                        name, ("print", "print_exc"),
                        f"{module}.py:{node.lineno} still calls {name}()",
                    )


if __name__ == "__main__":
    unittest.main()
