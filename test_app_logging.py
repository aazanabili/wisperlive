"""Isolated tests for app_logging.py (core proposal section 5.1, section 8 case 3,
and section 7 negative path: unwritable log dir must not stop app startup).

D1 GATE (binding): every test that runs `setup_logging()` captures and restores
(a) the root logger's handlers/level and (b) `sys.stderr`, in setUp/tearDown
(cleanup hooks), so a single `python -m unittest test_app_logging
test_single_instance` process can never leak a root handler into
`test_single_instance`'s stderr assertion (logging.lastResort is dynamic).
"""

import logging
import os
import sys
import tempfile
import unittest
from logging.handlers import RotatingFileHandler
from unittest import mock

import app_logging


class AppLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = logging.getLogger()
        self.app_logger = logging.getLogger("app_logging")
        self.saved_root_level = self.root.level
        self.saved_root_handlers = list(self.root.handlers)
        self.saved_app_handlers = list(self.app_logger.handlers)
        self.saved_levels = {
            name: logging.getLogger(name).level
            for name in ("google", "grpc")
        }
        # D1: sys.stderr must leave exactly as it arrived too.
        self.saved_stderr = sys.stderr
        self.addCleanup(self._restore)

    def _restore(self):
        self.root.setLevel(self.saved_root_level)
        for handler in list(self.root.handlers):
            if handler not in self.saved_root_handlers:
                self.root.removeHandler(handler)
                handler.close()
        for handler in list(self.app_logger.handlers):
            if handler not in self.saved_app_handlers:
                self.app_logger.removeHandler(handler)
        for name, level in self.saved_levels.items():
            logging.getLogger(name).setLevel(level)
        sys.stderr = self.saved_stderr

    def _new_root_handlers(self):
        return [h for h in self.root.handlers if h not in self.saved_root_handlers]

    # section 7: broken %LOCALAPPDATA% (a FILE, not a dir) -> NullHandler, never raises
    def test_setup_logging_never_raises_on_broken_localappdata(self):
        blocked = os.path.join(self.temp.name, "blocked")
        with open(blocked, "w", encoding="utf-8"):
            pass  # a plain file where LOCALAPPDATA should be a directory
        before = len(self.app_logger.handlers)

        with mock.patch.dict(os.environ, {"LOCALAPPDATA": blocked}):
            app_logging.setup_logging()  # must return normally

        self.assertEqual(len(self.app_logger.handlers), before + 1)
        self.assertIsInstance(self.app_logger.handlers[-1], logging.NullHandler)
        self.assertEqual(self._new_root_handlers(), [], "no half-created file handler on root")

    # success path: rotating file handler + formatter + quieted third-party sinks
    def test_setup_logging_installs_rotating_handler_with_formatter(self):
        local = os.path.join(self.temp.name, "local")

        with mock.patch.dict(os.environ, {"LOCALAPPDATA": local}):
            app_logging.setup_logging()

        handlers = self._new_root_handlers()
        self.assertEqual(len(handlers), 1)
        handler = handlers[0]
        self.assertIsInstance(handler, RotatingFileHandler)
        expected_log = os.path.join(local, "WhisperLive", "logs", "whisperlive.log")
        self.assertEqual(os.path.normcase(handler.baseFilename), os.path.normcase(os.path.abspath(expected_log)))
        self.assertTrue(os.path.exists(expected_log))
        self.assertEqual(handler.maxBytes, app_logging.MAX_LOG_BYTES)
        self.assertEqual(handler.backupCount, app_logging.LOG_BACKUP_COUNT)
        self.assertIsNotNone(handler.formatter)
        self.assertEqual(handler.formatter._fmt, app_logging.LOG_FORMAT)
        self.assertEqual(self.root.level, logging.INFO)
        self.assertEqual(logging.getLogger("google").level, logging.WARNING)
        self.assertEqual(logging.getLogger("grpc").level, logging.WARNING)

    # privacy policy is baked into the module; get_logger is a plain named logger
    def test_privacy_policy_documented_and_get_logger_returns_named_logger(self):
        self.assertIn("NEVER log transcript", app_logging.__doc__)
        logger = app_logging.get_logger("whisperlive.test.module")
        self.assertIs(logger, logging.getLogger("whisperlive.test.module"))

    # call-time env read: _log_dir() and setup_logging() follow the CURRENT env
    def test_log_dir_reads_env_at_call_time(self):
        first = os.path.join(self.temp.name, "first")
        second = os.path.join(self.temp.name, "second")

        with mock.patch.dict(os.environ, {"LOCALAPPDATA": first}):
            self.assertEqual(
                str(app_logging._log_dir()),
                os.path.join(first, "WhisperLive", "logs"),
            )
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": second}):
            self.assertEqual(
                str(app_logging._log_dir()),
                os.path.join(second, "WhisperLive", "logs"),
            )
            app_logging.setup_logging()  # uses _log_dir() at call time

        basenames = [os.path.normcase(getattr(h, "baseFilename", "")) for h in self._new_root_handlers()]
        self.assertTrue(
            any(name.startswith(os.path.normcase(os.path.abspath(second))) for name in basenames),
            "handler must follow the env value present at call time",
        )


if __name__ == "__main__":
    unittest.main()
