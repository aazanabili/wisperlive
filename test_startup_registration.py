import os
import sys
import unittest
from unittest import mock

import main


class StartupRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.app = main.WhisperLiveApp.__new__(main.WhisperLiveApp)
        self.key = mock.MagicMock()
        self.open_key = mock.patch.object(main.winreg, "OpenKey")
        self.open_key_mock = self.open_key.start()
        self.open_key_mock.return_value.__enter__.return_value = self.key
        self.set_value = mock.patch.object(main.winreg, "SetValueEx")
        self.set_value_mock = self.set_value.start()
        self.addCleanup(self.open_key.stop)
        self.addCleanup(self.set_value.stop)

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
        self.key.DeleteValue.side_effect = FileNotFoundError

        self.assertTrue(self.app.set_startup_registration(False))

        self.key.DeleteValue.assert_called_once_with(main.APP_NAME)
        self.set_value_mock.assert_not_called()

    def test_reports_disable_failure_without_claiming_success(self):
        self.key.DeleteValue.side_effect = PermissionError("access denied")

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


if __name__ == "__main__":
    unittest.main()
