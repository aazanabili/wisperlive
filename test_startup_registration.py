import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import main


@unittest.skipUnless(sys.platform == "win32", "Windows registry startup registration")
class StartupRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.app = main.WhisperLiveApp.__new__(main.WhisperLiveApp)
        self.key = mock.MagicMock()
        self.open_key = mock.patch.object(main.winreg, "OpenKey")
        self.open_key_mock = self.open_key.start()
        self.open_key_mock.return_value.__enter__.return_value = self.key
        self.set_value = mock.patch.object(main.winreg, "SetValueEx")
        self.set_value_mock = self.set_value.start()
        self.delete_value = mock.patch.object(main.winreg, "DeleteValue")
        self.delete_value_mock = self.delete_value.start()
        self.addCleanup(self.open_key.stop)
        self.addCleanup(self.set_value.stop)
        self.addCleanup(self.delete_value.stop)

    def test_enables_frozen_app_with_absolute_quoted_executable(self):
        with mock.patch.object(sys, "executable", r"C:\Program Files\Whisper Live\WhisperLive.exe"), \
                mock.patch.object(sys, "frozen", True, create=True):
            self.assertTrue(self.app.set_startup_registration(True))

        command = self._set_value_command()
        self.assertEqual(command, r'"C:\Program Files\Whisper Live\WhisperLive.exe"')
        self.assertTrue(os.path.isabs(command.strip('"')))

    def test_enables_source_with_absolute_quoted_executable_and_script(self):
        with mock.patch.object(sys, "executable", r"C:\Python 3\python.exe"), \
                mock.patch.object(sys, "frozen", False, create=True), \
                mock.patch.object(main, "__file__", r"C:\Program Files\Whisper Live\main.py"):
            self.assertTrue(self.app.set_startup_registration(True))

        command = self._set_value_command()
        self.assertEqual(
            command,
            r'"C:\Python 3\python.exe" "C:\Program Files\Whisper Live\main.py"',
        )

    def test_disables_without_writing_and_ignores_missing_value(self):
        self.delete_value_mock.side_effect = FileNotFoundError

        self.assertTrue(self.app.set_startup_registration(False))

        self.delete_value_mock.assert_called_once_with(self.key, main.APP_NAME)
        self.set_value_mock.assert_not_called()

    def test_reports_disable_failure_without_claiming_success(self):
        self.delete_value_mock.side_effect = PermissionError("access denied")

        self.assertFalse(self.app.set_startup_registration(False))
        self.set_value_mock.assert_not_called()

    def test_reports_registry_failure_without_claiming_success(self):
        self.open_key_mock.side_effect = PermissionError("access denied")

        self.assertFalse(self.app.set_startup_registration(True))
        self.set_value_mock.assert_not_called()

    def _set_value_command(self):
        self.open_key_mock.assert_called_once_with(
            main.winreg.HKEY_CURRENT_USER,
            main.STARTUP_KEY,
            0,
            main.winreg.KEY_SET_VALUE,
        )
        self.set_value_mock.assert_called_once_with(
            self.key, main.APP_NAME, 0, main.winreg.REG_SZ, mock.ANY
        )
        return self.set_value_mock.call_args.args[4]


class SaveAndApplyTests(unittest.TestCase):
    def setUp(self):
        self.app = main.WhisperLiveApp.__new__(main.WhisperLiveApp)
        self.app.config = {"run_at_startup": False, "shortcut": "ctrl+space"}
        self.app.run_at_startup_var = mock.MagicMock()
        self.app.run_at_startup_var.get.return_value = True
        self.app.clear_hotkeys = mock.MagicMock()
        self.app.setup_hotkeys = mock.MagicMock()
        self.app.set_status = mock.MagicMock()

    def test_registry_failure_saves_other_settings_without_claiming_startup(self):
        self.app.config["api_key"] = "old"
        self.app.collect_form_values = lambda: self.app.config.update(
            api_key="new", run_at_startup=True
        )
        with mock.patch.object(main.WhisperLiveApp, "set_startup_registration", return_value=False) as registry, \
                mock.patch.object(main, "save_config", return_value=True) as save:
            self.app.save_and_apply()

        registry.assert_called_once_with(True)
        save.assert_called_once_with({"run_at_startup": False, "shortcut": "ctrl+space", "api_key": "new"})
        self.assertFalse(self.app.config["run_at_startup"])
        self.app.set_status.assert_called_once_with(
            "Changes saved. Startup registration could not be updated; the startup setting was not changed.", "error"
        )

    def test_save_failure_rolls_registry_back(self):
        self.app.collect_form_values = lambda: self.app.config.update(run_at_startup=True)
        with mock.patch.object(main.WhisperLiveApp, "set_startup_registration", side_effect=[True, True]) as registry, \
                mock.patch.object(main, "save_config", return_value=False):
            self.app.save_and_apply()

        self.assertEqual(registry.call_args_list, [mock.call(True), mock.call(False)])
        self.assertFalse(self.app.config["run_at_startup"])
        self.app.set_status.assert_called_once_with(
            "Changes could not be saved. Startup registration was restored.", "error"
        )

    def test_save_failure_reports_failed_rollback(self):
        self.app.collect_form_values = lambda: self.app.config.update(run_at_startup=True)
        with mock.patch.object(main.WhisperLiveApp, "set_startup_registration", side_effect=[True, False]), \
                mock.patch.object(main, "save_config", return_value=False):
            self.app.save_and_apply()

        self.app.set_status.assert_called_once_with(
            "Changes could not be saved, and startup registration could not be restored.", "error"
        )

    def test_success_persists_matching_startup_state(self):
        self.app.collect_form_values = lambda: self.app.config.update(run_at_startup=True)
        with mock.patch.object(main.WhisperLiveApp, "set_startup_registration", return_value=True) as registry, \
                mock.patch.object(main, "save_config", return_value=True) as save:
            self.app.save_and_apply()

        registry.assert_called_once_with(True)
        save.assert_called_once_with(self.app.config)
        self.assertTrue(self.app.config["run_at_startup"])
        self.app.set_status.assert_called_once_with("Changes saved. Your shortcut is ready.", "ready")


@unittest.skipIf(sys.platform == "win32", "XDG autostart startup registration")
class LinuxStartupRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.app = main.WhisperLiveApp.__new__(main.WhisperLiveApp)

    def _autostart_dir(self):
        return Path(os.environ["XDG_CONFIG_HOME"]) / "autostart"

    def test_enable_writes_desktop_file_with_quoted_command(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": root}), \
                mock.patch.object(sys, "executable", "/opt/Whisper Live/whisperlive"), \
                mock.patch.object(main, "__file__", "/opt/Whisper Live/main.py"):
            self.assertTrue(self.app.set_startup_registration(True))

            desktop_file = self._autostart_dir() / "WhisperLive.desktop"
            content = desktop_file.read_text(encoding="utf-8")
            self.assertIn("[Desktop Entry]", content)
            self.assertIn("Type=Application", content)
            self.assertIn("Name=WhisperLive", content)
            self.assertIn(f"Exec={main.WhisperLiveApp.startup_command()}", content)
            self.assertIn('Exec="/opt/Whisper Live/whisperlive" "/opt/Whisper Live/main.py"', content)
            self.assertIn("Terminal=false", content)
            self.assertTrue(content.endswith("\n"))

    def test_enable_frozen_app_uses_executable_only(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": root}), \
                mock.patch.object(sys, "executable", "/opt/whisperlive/whisperlive"), \
                mock.patch.object(sys, "frozen", True, create=True):
            self.assertTrue(self.app.set_startup_registration(True))

            content = self._autostart_dir().joinpath("WhisperLive.desktop").read_text(encoding="utf-8")
            self.assertIn('Exec="/opt/whisperlive/whisperlive"', content)
            self.assertNotIn("main.py", content)

    def test_enable_without_xdg_config_home_uses_dot_config(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.dict(os.environ, {"HOME": root}, clear=False):
            os.environ.pop("XDG_CONFIG_HOME", None)
            self.assertTrue(self.app.set_startup_registration(True))
            self.assertTrue((Path(root) / ".config" / "autostart" / "WhisperLive.desktop").exists())

    def test_disable_removes_desktop_file_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": root}):
            self.assertTrue(self.app.set_startup_registration(True))
            desktop_file = self._autostart_dir() / "WhisperLive.desktop"
            self.assertTrue(desktop_file.exists())

            self.assertTrue(self.app.set_startup_registration(False))
            self.assertFalse(desktop_file.exists())
            self.assertTrue(self.app.set_startup_registration(False))

    def test_reports_failure_without_claiming_success(self):
        with tempfile.TemporaryDirectory() as root:
            blocked = Path(root) / "not-a-directory"
            blocked.write_text("file, not a directory", encoding="utf-8")
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(blocked)}):
                self.assertFalse(self.app.set_startup_registration(True))


if __name__ == "__main__":
    unittest.main()
