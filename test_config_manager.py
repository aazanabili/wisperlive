import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config_manager


class ConfigManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.local = self.root / "local"
        self.source = self.root / "source"
        self.exe = self.root / "exe"
        self.cwd = self.root / "cwd"
        for directory in (self.local, self.source, self.exe, self.cwd):
            directory.mkdir()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": str(self.local)}, clear=False)
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

    def test_windows_dpapi_roundtrip_format(self):
        def fake_dpapi(protect, value):
            return b"wrapped:" + value if protect else value[len(b"wrapped:"):]

        with patch.object(config_manager.os, "name", "nt"), patch.object(
            config_manager, "_windows_dpapi", side_effect=fake_dpapi
        ):
            config_manager.save_config({"api_key": "dpapi-secret"})
            raw = config_manager.get_config_path().read_text(encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
