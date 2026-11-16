"""``--smoke`` self-check tests (plan 2.3; gate G6 packaging/import-failure).

Owner: qa-test-assurance.  Covers:
* ``smoke.run()`` returns 0 on this healthy dev checkout (core imports,
  manifest pins, provider registry) with absent ML extras reported as SKIP,
* a broken core import flips the result to exit code 1 (strict checks),
* a placeholder zero-digest manifest flips it to 1 (G3 x G6 intersection),
* the shipped subprocess entry points: ``python smoke.py`` and
  ``python main.py --smoke`` both exit 0 BEFORE any Tk init (R-U5/R-8c),
* import-failure paths hide modes / fail closed and NEVER crash the app (D8).
"""

import io
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

import smoke

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON = os.path.join(REPO_DIR, "venv", "Scripts", "python.exe") \
    if os.name == "nt" else os.path.join(REPO_DIR, "venv", "bin", "python")


def run_capture(func, *args):
    from contextlib import redirect_stdout
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = func(*args)
    return code, buffer.getvalue()


class SmokeRunTests(unittest.TestCase):
    def test_run_returns_zero_and_reports_pass_on_this_checkout(self):
        code, output = run_capture(smoke.run)
        self.assertEqual(code, 0, output)
        self.assertIn("smoke: PASS", output)
        self.assertIn("[OK] models_manifest pins", output)
        self.assertIn("[OK] providers registry", output)
        # zero-digest placeholder would be a G3 finding -> must be reported
        self.assertNotIn("placeholder digests", output)

    def test_every_core_module_is_listed_and_importable(self):
        import importlib
        self.assertIn("dictation_session", smoke.CORE_MODULES)
        self.assertIn("live_cloud_engine", smoke.CORE_MODULES)
        for name in smoke.CORE_MODULES:
            with self.subTest(module=name):
                self.assertIsNotNone(importlib.import_module(name))

    def test_absent_ml_extras_are_skipped_never_failed(self):
        code, output = run_capture(smoke.run)
        self.assertEqual(code, 0)
        for package in ("faster_whisper", "onnxruntime", "websocket"):
            marker = f"optional {package}"
            if marker in output:
                line = next(line for line in output.splitlines()
                            if marker in line)
                self.assertTrue("[SKIP]" in line or "[OK]" in line,
                                f"{package} must be SKIP/OK, never FAIL: {line}")

    def test_broken_core_import_fails_the_smoke(self):
        real_import = smoke.importlib.import_module

        def sabotaged(name):
            if name == "dictation_types":
                raise ImportError("scripted import failure")
            return real_import(name)
        with mock.patch.object(smoke.importlib, "import_module",
                               side_effect=sabotaged):
            code, output = run_capture(smoke.run)
        self.assertEqual(code, 1, output)
        self.assertIn("smoke: FAIL", output)
        self.assertIn("ImportError", output)

    def test_placeholder_zero_digest_fails_the_smoke(self):
        # G3: a manifest whose digests are all zeros parses (format-pinned) but
        # is NOT release-pinned -> smoke must exit 1.
        import tempfile
        fake_manifest = {"version": 1, "models": [
            {"model_id": "m", "version": 1,
             "files": [{"path": "f.bin", "url": ["https://e.com/f"],
                        "sha256": "0" * 64, "size": 1}]}]}
        path = os.path.join(tempfile.gettempdir(), "qa-fake-manifest.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(fake_manifest, handle)
        with mock.patch.object(smoke, "_asset_path", return_value=path):
            code, output = run_capture(smoke.run)
        self.assertEqual(code, 1, output)
        self.assertIn("placeholder digests", output)

    def test_corrupt_manifest_file_fails_the_smoke(self):
        import tempfile
        path = os.path.join(tempfile.gettempdir(), "qa-corrupt-manifest.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{{{ not json")
        with mock.patch.object(smoke, "_asset_path", return_value=path):
            code, output = run_capture(smoke.run)
        self.assertEqual(code, 1, output)
        self.assertIn("models_manifest pins", output)


@unittest.skipUnless(os.path.exists(PYTHON), "venv interpreter not found")
class SmokeEntryPointTests(unittest.TestCase):
    """G6: the packaged entry points exit 0 on a healthy checkout."""

    def _run(self, *args, timeout=180):
        result = subprocess.run([PYTHON, *args], cwd=REPO_DIR,
                                capture_output=True, text=True, timeout=timeout)
        return result

    def test_smoke_py_exits_zero(self):
        result = self._run("smoke.py")
        self.assertEqual(result.returncode, 0,
                         f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        self.assertIn("smoke: PASS", result.stdout)

    def test_main_py_smoke_flag_exits_zero_before_any_tk(self):
        # R-U5: the flag dispatches BEFORE Tk init / instance lock — a second
        # copy must not trip the single-instance guard either.
        result = self._run("main.py", "--smoke")
        self.assertEqual(result.returncode, 0,
                         f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        self.assertIn("smoke: PASS", result.stdout)
        self.assertNotIn("already running", result.stdout.lower())


class ImportFailureHidesModesTests(unittest.TestCase):
    """D8/G6: missing extras hide the mode WITH a reason — never a crash."""

    def test_local_deps_check_is_find_spec_only(self):
        import settings_dictation_view as sdv
        with mock.patch.object(sdv.importlib.util, "find_spec",
                               return_value=None):
            self.assertFalse(sdv.local_deps_available())
        with mock.patch.object(sdv.importlib.util, "find_spec",
                               return_value=object()):
            self.assertTrue(sdv.local_deps_available())
        # a raised ImportError/ValueError degrades to False (never crashes)
        with mock.patch.object(sdv.importlib.util, "find_spec",
                               side_effect=ValueError("bad spec")):
            self.assertFalse(sdv.local_deps_available())

    def test_local_mode_preflight_fails_closed_with_a_typed_reason(self):
        import dictation_session as ds
        app = _app({"dictation_mode": "local", "cleanup_mode": "off",
                    "local_models": {}})
        session = ds.DictationSession(app)
        with mock.patch("importlib.util.find_spec", return_value=None):
            with self.assertRaises(Exception) as ctx:
                session.preflight(app.config)
        self.assertIn("requirements-ml", str(ctx.exception))
        # maybe_start never crashes: rejection -> status + beep, state stays IDLE
        with mock.patch("importlib.util.find_spec", return_value=None):
            self.assertTrue(session.maybe_start())
        self.assertEqual(session.state.value, "idle")
        self.assertEqual(app.recorder.starts, 0)
        self.assertTrue(any(state == "error" for _m, state in app.statuses))
        self.assertEqual(app.beeps[-1], (400, 300))

    def test_missing_model_preflight_names_settings_download(self):
        import dictation_session as ds
        from llm_errors import ModelNotDownloadedError
        app = _app({"dictation_mode": "local", "cleanup_mode": "off",
                    "model_size": "fast", "local_models": {}})
        session = ds.DictationSession(app)
        with mock.patch("importlib.util.find_spec", return_value=object()):
            with self.assertRaises(ModelNotDownloadedError) as ctx:
                session.preflight(app.config)
        self.assertIn("Settings", str(ctx.exception))
        self.assertTrue(ctx.exception.model_id)


def _app(config):
    """Minimal duck-typed app (never touches Tk or real audio)."""
    app = type("App", (), {})()
    app.config = dict(config)
    app.is_exiting = False
    app.is_processing = False
    app.statuses = []
    app.beeps = []
    app.finished = 0

    class Recorder:
        starts = 0
        sink = None

        def start_block_stream(self, sink):
            Recorder.starts += 1
            Recorder.sink = sink

        def stop_block_stream(self):
            return None
    app.recorder = Recorder()
    app.indicator = type("I", (), {"show": lambda *a: None,
                                   "hide": lambda *a: None})()
    app._on_processing_finished = lambda: None

    def set_status(message, state):
        app.statuses.append((message, state))
    app.set_status = set_status
    app.beep = lambda freq, dur: app.beeps.append((freq, dur))
    app.call_in_ui = lambda callback: callback() if not app.is_exiting else None
    return app


if __name__ == "__main__":
    unittest.main()
