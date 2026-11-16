"""Main-level processing tests: M2 (paste suppressed when quitting mid-
processing), queue-based UI marshaling, temp-file single-owner deletion, and
the error->status mapping contract (core section 6.4, ui section c row 6).

D4 LOCKED INVENTORY (re-derived from the actual file
`git show wip-cleanup-hardening:test_main_processing.py`, 10 cases) - retarget
case IDs, original name -> post-merge name:

  MP-01 ProcessAudioThreadTests.test_m2_paste_suppressed_when_exiting          (R: 5-arg snapshot, call_in_ui queue)
  MP-02 ProcessAudioThreadTests.test_pastes_and_marshals_completion_when_not_exiting (R: queue dispatch + root.after(50) pump)
  MP-03 ProcessAudioThreadTests.test_temp_file_deleted_after_processing        (R: 5-arg snapshot)
  MP-04 ProcessAudioThreadTests.test_temp_file_deleted_even_when_exiting       (R: 5-arg snapshot)
  MP-05 ProcessAudioThreadTests.test_transcription_error_maps_to_error_status  (R: llm_api.LLMError + upstream prefix)
  MP-06 ProcessAudioThreadTests.test_paste_error_maps_to_error_status          (R: upstream prefix)
  MP-07 ProcessAudioThreadTests.test_unexpected_error_maps_to_generic_status   (R: main.logger.exception)
  MP-08 StopAndProcessTests.test_recorder_error_maps_to_status_without_processing (V: main.winsound/main.threading still patched)
  MP-09 UiMarshalingTests.test_post_to_ui_swallows_destroyed_root -> test_call_in_ui_enqueues_and_becomes_noop_when_exiting (REPLACED per ui c row 6d: the destroyed-root _post_to_ui hazard no longer exists)
  MP-10 UiMarshalingTests.test_on_processing_finished_is_ui_thread_writer       (V)

No live Tk loop: root/set_status/indicator are mocks; queued callbacks are
executed inline by draining `_ui_queue` on this (mocked) pump.
"""

import os
import queue
import tempfile
import unittest
from unittest import mock

import main
from audio_recorder import RecorderError
from auto_typer import PasteError
from llm_api import LLMError

# D4 snapshot: the exact 5-arg contract the UI thread hands to the worker.
AUDIO = "whisperlive-snapshot.wav"
LANGUAGE = "English"
API_KEY = "sk-test"
BASE_URL = "https://example.com/v1"
MODEL = "gpt-audio"
SNAPSHOT = (AUDIO, LANGUAGE, API_KEY, BASE_URL, MODEL)
TRANSCRIPT = "TRANSCRIPT_TEXT"


def _app(is_exiting=False):
    app = main.WhisperLiveApp.__new__(main.WhisperLiveApp)
    app.is_exiting = is_exiting
    app.is_processing = False
    app._ui_queue = queue.Queue()
    app.root = mock.MagicMock()
    app.set_status = mock.MagicMock()
    app.indicator = mock.MagicMock()
    return app


class ProcessAudioThreadTests(unittest.TestCase):
    def setUp(self):
        self.process_patch = mock.patch.object(main, "process_audio", return_value=TRANSCRIPT)
        self.paste_patch = mock.patch.object(main, "paste_text")
        # Beep interception: patch the `winsound` REFERENCE in main (winsound is
        # None on POSIX, so patching main.winsound.Beep directly would be
        # Windows-only and the CI test job runs ubuntu + windows). beep() then
        # takes the winsound branch on every platform; the interception point
        # stays main.winsound.Beep through self.beep (ui c row 6g).
        self.winsound_patch = mock.patch.object(main, "winsound")
        self.process_mock = self.process_patch.start()
        self.paste_mock = self.paste_patch.start()
        self.winsound_mock = self.winsound_patch.start()
        self.beep_mock = self.winsound_mock.Beep
        for patcher in (self.winsound_patch, self.paste_patch, self.process_patch):
            self.addCleanup(patcher.stop)

    def app_status_quiescent(self, app):
        # is_exiting also makes call_in_ui a strict no-op: nothing enqueued,
        # no pump reschedule, no status change.
        self.assertTrue(app._ui_queue.empty(), "call_in_ui must be a strict no-op while exiting")
        app.root.after.assert_not_called()
        app.set_status.assert_not_called()

    # MP-01 M2: quitting mid-processing must suppress paste/beep (ui section c row 6g)
    def test_m2_paste_suppressed_when_exiting(self):
        app = _app(is_exiting=True)

        app.process_audio_thread(*SNAPSHOT)  # must not raise

        self.process_mock.assert_called_once_with(*SNAPSHOT)
        self.paste_mock.assert_not_called()
        self.beep_mock.assert_not_called()
        self.app_status_quiescent(app)

    # MP-02 control: normal path pastes, beeps, and marshals completion to the
    # UI thread THROUGH THE QUEUE (the worker never touches Tk directly)
    def test_pastes_and_marshals_completion_when_not_exiting(self):
        app = _app(is_exiting=False)

        app.process_audio_thread(*SNAPSHOT)

        self.paste_mock.assert_called_once_with(TRANSCRIPT)
        self.beep_mock.assert_called_once_with(1200, 200)
        # Worker side: the completion status is ENQUEUED, not executed.
        app.set_status.assert_not_called()
        self.assertFalse(app._ui_queue.empty(), "completion callback must be enqueued")

        app._drain_ui_queue()  # the UI thread (this test thread) drains the pump

        app.set_status.assert_called_once_with("Done. Ready for the next recording.", "ready")
        self.assertFalse(app.is_processing, "completion handler must reset the flag")
        app.indicator.hide.assert_called_once()
        # The pump reschedules itself via root.after(50, ...) while running.
        self.assertEqual(app.root.after.call_args.args[0], 50)

    # MP-03 single-owner rule: the caller deletes the temp file it received (core 1.1)
    def test_temp_file_deleted_after_processing(self):
        app = _app(is_exiting=False)
        with tempfile.TemporaryDirectory() as tmp:
            audio = os.path.join(tmp, "whisperlive-ab12.wav")
            with open(audio, "wb") as handle:
                handle.write(b"data")

            app.process_audio_thread(audio, *SNAPSHOT[1:])

            self.assertFalse(os.path.exists(audio))

    # MP-04 quit race: file must still be deleted even when paste is suppressed
    def test_temp_file_deleted_even_when_exiting(self):
        app = _app(is_exiting=True)
        with tempfile.TemporaryDirectory() as tmp:
            audio = os.path.join(tmp, "whisperlive-cd34.wav")
            with open(audio, "wb") as handle:
                handle.write(b"data")

            app.process_audio_thread(audio, *SNAPSHOT[1:])

            self.assertFalse(os.path.exists(audio))
            self.paste_mock.assert_not_called()

    # MP-05 error mapping: user-safe LLMError -> status bar, nothing pasted
    def test_transcription_error_maps_to_error_status(self):
        app = _app(is_exiting=False)
        app.is_processing = True  # non-vacuous: the completion handler must reset it
        self.process_mock.side_effect = LLMError("user-safe transcription message")

        app.process_audio_thread(*SNAPSHOT)
        app._drain_ui_queue()

        # ui c row 6c: typed errors carry UPSTREAM's prefix copy.
        app.set_status.assert_called_once_with(
            "Could not process recording: user-safe transcription message", "error"
        )
        self.paste_mock.assert_not_called()
        self.assertFalse(app.is_processing, "completion handler must reset the flag")

    # MP-06 error mapping: PasteError surfaces without crashing the worker thread
    def test_paste_error_maps_to_error_status(self):
        app = _app(is_exiting=False)
        self.paste_mock.side_effect = PasteError("The clipboard is busy. Please try again.")

        app.process_audio_thread(*SNAPSHOT)
        app._drain_ui_queue()

        app.set_status.assert_called_once_with(
            "Could not process recording: The clipboard is busy. Please try again.", "error"
        )
        self.beep_mock.assert_not_called()  # paste raised before the completion beep

    # MP-07 error mapping: unexpected exception -> generic user-safe status, logged
    def test_unexpected_error_maps_to_generic_status(self):
        app = _app(is_exiting=False)
        self.process_mock.side_effect = RuntimeError("internal detail")

        with mock.patch.object(main.logger, "exception") as logged:
            app.process_audio_thread(*SNAPSHOT)
            app._drain_ui_queue()

        app.set_status.assert_called_once_with(
            "Something went wrong while processing the recording.", "error"
        )
        logged.assert_called_once()
        # internal detail must never reach the status bar (ui c row 6d)
        self.assertNotIn("internal detail", app.set_status.call_args.args[0])


class StopAndProcessTests(unittest.TestCase):
    # MP-08 (V): failure path never starts the worker; RecorderError -> status
    def test_recorder_error_maps_to_status_without_processing(self):
        app = _app(is_exiting=False)
        app.config = {}
        app.recorder = mock.MagicMock()
        app.recorder.is_recording = True
        app.recorder.stop_recording.side_effect = RecorderError(
            "Recording failed and no audio was saved. Please try again."
        )

        with mock.patch.object(main, "winsound") as winsound, \
                mock.patch.object(main.threading, "Thread") as thread:
            app.stop_and_process()

        thread.assert_not_called()  # failure path never starts the worker
        self.assertFalse(app.is_processing)
        app.recorder.stop_recording.assert_called_once()
        app.set_status.assert_called_once_with(
            "Recording failed and no audio was saved. Please try again.", "error"
        )
        self.assertEqual(
            [call.args for call in winsound.Beep.call_args_list],
            [(800, 100), (400, 300)],
        )


class UiMarshalingTests(unittest.TestCase):
    # MP-09 (REPLACES test_post_to_ui_swallows_destroyed_root, ui c row 6d):
    # _post_to_ui no longer exists - workers never touch root.after, so the
    # destroyed-root hazard is gone. Marshaling is now: enqueue -> drain on the
    # pump -> strict no-op once is_exiting flips.
    def test_call_in_ui_enqueues_and_becomes_noop_when_exiting(self):
        app = _app(is_exiting=False)
        ran = []

        app.call_in_ui(lambda: ran.append("queued"))
        self.assertFalse(app._ui_queue.empty(), "callbacks must be enqueued for the UI thread")

        app.is_exiting = True
        app.call_in_ui(lambda: ran.append("must not run"))  # strict no-op once exiting
        self.assertEqual(app._ui_queue.qsize(), 1, "the exiting call must not be enqueued")

        app._drain_ui_queue()  # already-queued callbacks still drain during shutdown

        self.assertEqual(ran, ["queued"])
        app.root.after.assert_not_called()  # pump stops rescheduling while exiting

    # MP-10 (V): _on_processing_finished is the UI-thread writer of is_processing
    def test_on_processing_finished_is_ui_thread_writer(self):
        app = _app(is_exiting=False)
        app.is_processing = True

        app._on_processing_finished()

        self.assertFalse(app.is_processing)
        app.indicator.hide.assert_called_once()


if __name__ == "__main__":
    unittest.main()
