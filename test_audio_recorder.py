"""Union of BOTH test suites for the merged audio_recorder.py (ui reconciliation
row 5, core section 12.1): our hardened-lifecycle `AudioRecorderTests` first,
upstream's bundle-discovery classes appended, one shared import block.

The classes are disjoint (`AudioRecorderTests` vs `BundledPortAudioTests` /
`FindLibraryWrapperTests`) so the union compiles with zero symbol collisions:
18 cases total, zero case loss vs either source. All sounddevice/soundfile
access is mocked - no audio device, no PortAudio, no real microphone.
"""

import os
import queue
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import audio_recorder
from audio_recorder import AudioRecorder, RecorderError


class FakeBlock:
    """Stand-in for a numpy audio block passed to the PortAudio callback."""

    def __init__(self, data):
        self.data = list(data)

    def copy(self):
        return FakeBlock(self.data)


class FakeSoundFile:
    """Stand-in for sf.SoundFile; writes through to the real temp path."""

    instances = []
    write_started = None  # Event set when a (stall-configured) write begins
    write_release = None  # Event; while unset, configured writes block

    def __init__(self, path, mode="w", samplerate=None, channels=None):
        self.path = path
        self.blocks = []
        self._handle = open(path, "ab")
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self._handle.close()
        return False

    def write(self, block):
        if FakeSoundFile.write_release is not None:
            if FakeSoundFile.write_started is not None:
                FakeSoundFile.write_started.set()
            FakeSoundFile.write_release.wait(timeout=10)
        self.blocks.append(block)
        self._handle.write(b"\x00\x01")
        self._handle.flush()


class FakeInputStream:
    """Stand-in for sd.InputStream; delivers queued blocks in __enter__."""

    blocks = []  # delivered synchronously when the stream opens
    enter_error = None  # raised by __enter__ to simulate a device failure

    def __init__(self, samplerate=None, channels=None, callback=None):
        self.callback = callback

    def __enter__(self):
        if FakeInputStream.enter_error is not None:
            raise FakeInputStream.enter_error
        for block in FakeInputStream.blocks:
            self.callback(block, len(block.data), None, None)
        return self

    def __exit__(self, *exc_info):
        return False


class AudioRecorderTests(unittest.TestCase):
    def setUp(self):
        FakeSoundFile.instances = []
        FakeSoundFile.write_started = None
        FakeSoundFile.write_release = None
        FakeInputStream.blocks = []
        FakeInputStream.enter_error = None
        self.recorders = []
        self.sd_patch = mock.patch.object(audio_recorder, "sd", mock.Mock(InputStream=FakeInputStream))
        self.sf_patch = mock.patch.object(audio_recorder, "sf", mock.Mock(SoundFile=FakeSoundFile))
        self.sd_patch.start()
        self.sf_patch.start()
        self.addCleanup(self.sf_patch.stop)
        self.addCleanup(self.sd_patch.stop)
        self.addCleanup(self._cleanup_recorders)

    def _cleanup_recorders(self):
        # Unblock any stalled writer FIRST, then join, then remove leftovers.
        if FakeSoundFile.write_release is not None:
            FakeSoundFile.write_release.set()
        for recorder in self.recorders:
            if recorder._thread is not None:
                recorder._thread.join(timeout=5)
            path = recorder._current_path
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def make_recorder(self):
        recorder = AudioRecorder()
        self.recorders.append(recorder)
        return recorder

    @staticmethod
    def wait_until(predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    # 1. success path: path returned, blocks written, temp dir not CWD
    def test_stop_returns_path_and_writes_captured_blocks(self):
        FakeInputStream.blocks = [FakeBlock([0.1] * 4), FakeBlock([0.2] * 4)]
        recorder = self.make_recorder()
        recorder.start_recording()
        self.assertTrue(self.wait_until(lambda: recorder._blocks_written == 2),
                        "recorder thread did not consume both blocks")

        path = recorder.stop_recording()

        self.assertIsInstance(path, str)
        self.assertTrue(os.path.exists(path), "caller owns a real file after success")
        self.assertEqual(recorder._blocks_written, 2)
        self.assertEqual(len(FakeSoundFile.instances[0].blocks), 2)
        # %TEMP% placement, never the (possibly read-only) CWD
        temp_prefix = os.path.normcase(os.path.abspath(tempfile.gettempdir())) + os.sep
        self.assertTrue(os.path.normcase(os.path.abspath(path)).startswith(temp_prefix))
        self.assertNotEqual(
            os.path.normcase(os.path.dirname(os.path.abspath(path))),
            os.path.normcase(os.path.abspath(os.getcwd())),
        )

    # 2. unique temp file per recording (no CWD collision across instances)
    def test_temp_files_are_unique_per_recording(self):
        FakeInputStream.blocks = [FakeBlock([0.1])]
        recorder = self.make_recorder()
        paths = []
        for _ in range(2):
            recorder.start_recording()
            self.assertTrue(self.wait_until(lambda: recorder._blocks_written == 1))
            paths.append(recorder.stop_recording())
        self.assertTrue(all(os.path.exists(p) for p in paths))
        self.assertNotEqual(paths[0], paths[1])

    # 3. thread failure -> user-safe RecorderError, cause never leaks, file deleted
    def test_stop_raises_recorder_error_when_thread_fails(self):
        FakeInputStream.enter_error = OSError("device unplugged (internal detail)")
        recorder = self.make_recorder()
        recorder.start_recording()
        self.assertTrue(self.wait_until(lambda: recorder._error is not None),
                        "thread failure was not recorded")
        path = recorder._current_path

        with self.assertRaises(RecorderError) as ctx:
            recorder.stop_recording()

        self.assertEqual(str(ctx.exception), "Recording failed and no audio was saved. Please try again.")
        self.assertNotIn("device unplugged", str(ctx.exception))  # str(error) is user-safe
        self.assertFalse(os.path.exists(path), "recorder deletes the temp file on failure")
        self.assertFalse(recorder.is_recording)

    # 4. zero audio captured -> RecorderError + file deleted
    def test_stop_raises_on_zero_audio(self):
        recorder = self.make_recorder()
        recorder.start_recording()
        path = recorder._current_path

        with self.assertRaises(RecorderError) as ctx:
            recorder.stop_recording()

        self.assertIn("No audio was captured", str(ctx.exception))
        self.assertFalse(os.path.exists(path))
        self.assertFalse(recorder.is_recording)

    # 5. regression: stop must be bounded even when the stream/disk wedges
    def test_stop_is_bounded_when_stream_stalls(self):
        FakeSoundFile.write_started = threading.Event()
        FakeSoundFile.write_release = threading.Event()
        FakeInputStream.blocks = [FakeBlock([0.1])]
        recorder = self.make_recorder()
        with mock.patch.object(audio_recorder, "THREAD_JOIN_TIMEOUT_S", 0.3) as join_timeout:
            recorder.start_recording()
            self.assertTrue(self.wait_until(lambda: FakeSoundFile.write_started.is_set()),
                            "writer never reached the blocking write")
            started = time.monotonic()
            with self.assertRaises(RecorderError) as ctx:
                recorder.stop_recording()
            elapsed = time.monotonic() - started

        self.assertLessEqual(elapsed, join_timeout + 2.0, "stop_recording exceeded its bounded join")
        self.assertIn("did not stop cleanly", str(ctx.exception))
        self.assertTrue(recorder._thread.is_alive(), "join timed out; the wedged daemon thread is outlived")
        # documented module contract: the shipped join bound is 5 seconds
        self.assertEqual(audio_recorder.THREAD_JOIN_TIMEOUT_S, 5.0)

    # 6. start guard: a stale (still-alive) run can never overlap a new one
    def test_start_refuses_while_previous_thread_alive(self):
        FakeSoundFile.write_started = threading.Event()
        FakeSoundFile.write_release = threading.Event()
        FakeInputStream.blocks = [FakeBlock([0.1])]
        recorder = self.make_recorder()
        with mock.patch.object(audio_recorder, "THREAD_JOIN_TIMEOUT_S", 0.3):
            recorder.start_recording()
            self.assertTrue(self.wait_until(lambda: FakeSoundFile.write_started.is_set()))
            with self.assertRaises(RecorderError):
                recorder.stop_recording()  # wedged -> "did not stop cleanly", thread alive
            stale_path = recorder._current_path

            with self.assertRaises(RecorderError) as ctx:
                recorder.start_recording()

        self.assertIn("previous recording is still shutting down", str(ctx.exception))
        self.assertEqual(recorder._current_path, stale_path, "no new temp file was allocated")
        self.assertFalse(recorder.is_recording)

    # 7a. cancel during a healthy recording: no raise, file deleted
    def test_cancel_recording_deletes_file_without_raising(self):
        FakeInputStream.blocks = [FakeBlock([0.1])]
        recorder = self.make_recorder()
        recorder.start_recording()
        self.assertTrue(self.wait_until(lambda: recorder._blocks_written == 1))
        path = recorder._current_path

        recorder.cancel_recording()  # must not raise

        self.assertFalse(recorder.is_recording)
        self.assertFalse(os.path.exists(path), "cancel_recording owns deletion internally")

    # 7b. cancel after a thread failure: still no raise, file still deleted
    def test_cancel_recording_never_raises_when_thread_failed(self):
        FakeInputStream.enter_error = OSError("device unplugged")
        recorder = self.make_recorder()
        recorder.start_recording()
        self.assertTrue(self.wait_until(lambda: recorder._error is not None))
        path = recorder._current_path

        recorder.cancel_recording()  # teardown must never raise

        self.assertFalse(os.path.exists(path))

    # 8. PortAudio status flag -> logged warning, never an exception, block buffered
    def test_callback_status_logged_not_raised(self):
        recorder = self.make_recorder()
        recorder._queue = queue.Queue()
        block = FakeBlock([1, 2, 3])

        with self.assertLogs(audio_recorder.logger, level="WARNING") as captured:
            recorder._callback(block, 3, None, "input overflow")

        self.assertTrue(any("sounddevice stream status" in line for line in captured.output))
        self.assertEqual(recorder._queue.qsize(), 1, "block must still be buffered")

    # 9. not recording -> None (idempotent stop)
    def test_stop_returns_none_when_not_recording(self):
        recorder = self.make_recorder()
        self.assertIsNone(recorder.stop_recording())

        FakeInputStream.blocks = [FakeBlock([0.1])]
        recorder.start_recording()
        self.assertTrue(self.wait_until(lambda: recorder._blocks_written == 1))
        self.assertIsNotNone(recorder.stop_recording())
        self.assertIsNone(recorder.stop_recording(), "second stop after success is a no-op")


# --- UPSTREAM suite (fix/issue-14), appended VERBATIM (ui row 5 / core 12.1) ---


class BundledPortAudioTests(unittest.TestCase):
    def test_no_bundle_without_meipass(self):
        with mock.patch.object(audio_recorder.sys, "_MEIPASS", None, create=True):
            self.assertIsNone(audio_recorder._bundled_portaudio())

    def test_finds_bundle_at_meipass_root(self):
        with tempfile.TemporaryDirectory() as root:
            bundled = Path(root) / "libportaudio.so.2"
            bundled.write_bytes(b"fake")
            with mock.patch.object(audio_recorder.sys, "_MEIPASS", root, create=True):
                self.assertEqual(audio_recorder._bundled_portaudio(), str(bundled))

    def test_finds_bundle_in_internal_layout(self):
        with tempfile.TemporaryDirectory() as root:
            internal = Path(root) / "_internal"
            internal.mkdir()
            bundled = internal / "libportaudio.so.2"
            bundled.write_bytes(b"fake")
            with mock.patch.object(audio_recorder.sys, "_MEIPASS", root, create=True):
                self.assertEqual(audio_recorder._bundled_portaudio(), str(bundled))

    def test_returns_none_without_bundled_file(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(audio_recorder.sys, "_MEIPASS", root, create=True):
            self.assertIsNone(audio_recorder._bundled_portaudio())


class FindLibraryWrapperTests(unittest.TestCase):
    def test_no_wrapper_outside_frozen_build(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(audio_recorder.sys, "_MEIPASS", root, create=True):
            (Path(root) / "libportaudio.so.2").write_bytes(b"fake")
            self.assertIsNone(audio_recorder._find_library_with_bundled_portaudio())

    def test_no_wrapper_without_bundled_file(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(audio_recorder.sys, "_MEIPASS", root, create=True), \
                mock.patch.object(audio_recorder.sys, "frozen", True, create=True):
            self.assertIsNone(audio_recorder._find_library_with_bundled_portaudio())

    def test_falls_back_to_bundle_only_for_portaudio(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(audio_recorder.sys, "_MEIPASS", root, create=True), \
                mock.patch.object(audio_recorder.sys, "frozen", True, create=True), \
                mock.patch("ctypes.util.find_library", return_value=None):
            bundled = Path(root) / "libportaudio.so.2"
            bundled.write_bytes(b"fake")
            wrapper = audio_recorder._find_library_with_bundled_portaudio()

            self.assertEqual(wrapper("portaudio"), str(bundled))
            self.assertIsNone(wrapper("X11"))

    def test_system_lookup_still_wins_when_available(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(audio_recorder.sys, "_MEIPASS", root, create=True), \
                mock.patch.object(audio_recorder.sys, "frozen", True, create=True), \
                mock.patch("ctypes.util.find_library", return_value="libportaudio.so.2"):
            (Path(root) / "libportaudio.so.2").write_bytes(b"fake")
            wrapper = audio_recorder._find_library_with_bundled_portaudio()

            self.assertEqual(wrapper("portaudio"), "libportaudio.so.2")


if __name__ == "__main__":
    unittest.main()
