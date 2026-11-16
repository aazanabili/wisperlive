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


class SnapshotThreadingTests(unittest.TestCase):
    """V-1 (plan 2.2): the UI-thread snapshot carries dialect +
    transcription_model to the worker thread; the 5-positional-arg contract
    stays intact."""

    def _run_stop(self, config):
        app = _app(is_exiting=False)
        app.config = dict(config)
        app.recorder = mock.MagicMock()
        app.recorder.is_recording = True
        app.recorder.stop_recording.return_value = "whisperlive-snap.wav"

        with mock.patch.object(main, "winsound"), \
                mock.patch.object(main.threading, "Thread") as thread:
            app.stop_and_process()
        return app, thread

    def test_mimo_snapshot_passes_dialect_and_transcription_model(self):
        app, thread = self._run_stop({
            "provider": "xiaomi-mimo",
            "provider_variant": "token-plan-sgp",
            "provider_base_url": "https://token-plan-sgp.xiaomimimo.com/v1",
            "api_key": "tp-key",
            "model": "mimo-v2.6-flash",
            "transcription_model": "mimo-v2.5-asr",
            "target_language": "English",
        })
        thread.assert_called_once()
        call = thread.call_args
        # bound methods compare by identity per access; compare the components
        self.assertEqual(call.kwargs["target"].__func__,
                         app.process_audio_thread.__func__)
        self.assertIs(call.kwargs["target"].__self__, app)
        # exactly 5 positional args (the D4 snapshot contract), passed to
        # Thread via its args= keyword
        self.assertEqual(call.kwargs["args"], (
            "whisperlive-snap.wav", "English", "tp-key",
            "https://token-plan-sgp.xiaomimimo.com/v1", "mimo-v2.6-flash",
        ))
        # keyword-only stage options snapshotted on the UI thread
        self.assertEqual(call.kwargs["kwargs"],
                         {"dialect": "mimo-asr",
                          "transcription_model": "mimo-v2.5-asr"})
        self.assertTrue(call.kwargs["daemon"])

    def test_default_snapshot_passes_openai_dialect_and_empty_stage1(self):
        _app_unused, thread = self._run_stop({
            "provider": "openai",
            "provider_base_url": "https://api.openai.com/v1",
            "api_key": "sk-key",
            "model": "gpt-audio",
            "transcription_model": "",
            "target_language": "English",
        })
        thread.assert_called_once()
        self.assertEqual(thread.call_args.kwargs["kwargs"],
                         {"dialect": "openai", "transcription_model": ""})

    def test_unknown_provider_snapshots_openai_dialect(self):
        _app_unused, thread = self._run_stop({
            "provider": "future-vendor",
            "provider_base_url": "https://u/v1",
            "model": "m",
            "target_language": "English",
        })
        self.assertEqual(thread.call_args.kwargs["kwargs"],
                         {"dialect": "openai", "transcription_model": ""})


class ConditionalForwardingTests(unittest.TestCase):
    """Pin the conditional kwarg forwarding of process_audio_thread: ONLY
    non-default stage options become kwargs, so the legacy 5-arg call shape
    (assert_called_once_with(*SNAPSHOT)) stays byte-exact for default runs."""

    def setUp(self):
        self.process_patch = mock.patch.object(main, "process_audio",
                                               return_value=TRANSCRIPT)
        self.paste_patch = mock.patch.object(main, "paste_text")
        self.winsound_patch = mock.patch.object(main, "winsound")
        self.process_mock = self.process_patch.start()
        self.paste_mock = self.paste_patch.start()
        self.winsound_mock = self.winsound_patch.start()
        for patcher in (self.winsound_patch, self.paste_patch, self.process_patch):
            self.addCleanup(patcher.stop)

    def test_defaults_forward_no_kwargs_at_all(self):
        app = _app(is_exiting=True)
        app.process_audio_thread(*SNAPSHOT)
        # EXACT call shape: 5 positional args, zero keyword args.
        self.process_mock.assert_called_once_with(*SNAPSHOT)
        self.assertEqual(self.process_mock.call_args.kwargs, {})

    def test_mimo_options_forward_both_kwargs(self):
        app = _app(is_exiting=True)
        app.process_audio_thread(*SNAPSHOT, dialect="mimo-asr",
                                 transcription_model="mimo-v2.5-asr")
        self.process_mock.assert_called_once_with(
            *SNAPSHOT, dialect="mimo-asr", transcription_model="mimo-v2.5-asr")

    def test_default_dialect_forwards_only_transcription_model(self):
        app = _app(is_exiting=True)
        app.process_audio_thread(*SNAPSHOT, dialect="openai",
                                 transcription_model="stage1-x")
        # dialect == DIALECT_OPENAI must NOT be forwarded (default is implied)
        self.process_mock.assert_called_once_with(
            *SNAPSHOT, transcription_model="stage1-x")
        self.assertNotIn("dialect", self.process_mock.call_args.kwargs)

    def test_empty_transcription_model_forwards_only_dialect(self):
        app = _app(is_exiting=True)
        app.process_audio_thread(*SNAPSHOT, dialect="mimo-asr",
                                 transcription_model="")
        self.process_mock.assert_called_once_with(*SNAPSHOT, dialect="mimo-asr")
        self.assertNotIn("transcription_model",
                         self.process_mock.call_args.kwargs)


class StartRecordingGuardTests(unittest.TestCase):
    """Guard matrix (plan 2.2): the recording gate checks EFFECTIVE models -
    stage 1 = transcription_model or model, stage 2 = model (fail-closed on
    both)."""

    def _app(self, config):
        app = _app(is_exiting=False)
        app.config = dict(config)
        app.recorder = mock.MagicMock()
        app.recorder.is_recording = False
        # Shadow the real beep (winsound.Beep would sound on this host).
        app.beep = mock.MagicMock()
        return app

    def _base(self, **overrides):
        config = {
            "provider_base_url": "https://api.openai.com/v1",
            "model": "gpt-audio",
            "transcription_model": "",
        }
        config.update(overrides)
        return config

    def test_empty_transcription_model_falls_back_to_model(self):
        app = self._app(self._base(transcription_model=""))
        app.start_recording()
        app.recorder.start_recording.assert_called_once()
        app.set_status.assert_called_once_with(
            "Recording in progress. Press the shortcut to stop.", "recording")
        app.beep.assert_called_once_with(1000, 100)

    def test_split_models_allow_recording(self):
        app = self._app(self._base(transcription_model="mimo-v2.5-asr",
                                   model="mimo-v2.6-flash"))
        app.start_recording()
        app.recorder.start_recording.assert_called_once()

    def test_both_models_empty_blocks_recording(self):
        app = self._app(self._base(model="", transcription_model=""))
        app.start_recording()
        app.recorder.start_recording.assert_not_called()
        app.set_status.assert_called_once_with(
            "Open Settings and set the provider URL and model before recording.",
            "error")
        app.beep.assert_called_once_with(400, 300)  # the error beep

    def test_stage2_model_empty_blocks_even_with_stage1_set(self):
        # effective_post = model must be non-empty (fail-closed on BOTH).
        app = self._app(self._base(model="", transcription_model="stage1-x"))
        app.start_recording()
        app.recorder.start_recording.assert_not_called()
        app.set_status.assert_called_once_with(
            "Open Settings and set the provider URL and model before recording.",
            "error")

    def test_missing_base_url_blocks_recording(self):
        app = self._app(self._base(provider_base_url=""))
        app.start_recording()
        app.recorder.start_recording.assert_not_called()
        app.set_status.assert_called_once_with(
            "Open Settings and set the provider URL and model before recording.",
            "error")


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


# ---------------------------------------------------------------------------
# Dictation-mode integration (plan 2.3, G1/G4): session snapshot threading,
# M2 quit-race extension (zero injection after exit), stop-during-backlog
# ordered drain, quota no-silent-failover, and Arabic/English trim properties.
# ---------------------------------------------------------------------------
import struct  # noqa: E402  (kept next to the session fixtures below)

import dictation_session as ds  # noqa: E402
from dictation_session import DictationSession  # noqa: E402
from dictation_types import EmissionOut, build_session_config  # noqa: E402
from vad_segmenter import VadSegmenter  # noqa: E402

SPEECH_BLOCK = struct.pack("<320h", *([1000] * 320))     # 20 ms, amplitude != 0
SILENCE_BLOCK = struct.pack("<320h", *([0] * 320))


class _ContentVadSession:
    """Fake silero model: score follows the WINDOW CONTENT (speech vs silence)."""

    def get_inputs(self):
        return []

    def run(self, _outputs, feed):
        window = feed["x"][0]
        speaking = any(abs(value) > 0.001 for value in window)
        return [[0.95 if speaking else 0.0]]


def make_vad(min_segment_s=0.0, store_dir="", **_kwargs):
    return VadSegmenter(_ContentVadSession(), onset_windows=2, pause_windows=3,
                        min_segment_s=min_segment_s)


class SlowEngine:
    """Deterministic out-of-order completions (odd seqs finish last)."""

    last_config = None

    def __init__(self, base=0.12, texts=None):
        self.base = base
        self.processed = []
        self.texts = texts

    def start(self, config):
        SlowEngine.last_config = config

    def process(self, segment):
        import time as _time
        _time.sleep(self.base if segment.segment_seq % 2 else 0.01)
        self.processed.append(segment.segment_seq)
        text = (self.texts or {}).get(
            segment.segment_seq, f"unit-{segment.segment_seq}")
        return [EmissionOut(text, segment_seq=segment.segment_seq)]

    def close(self):
        pass


class RecordingTyper:
    def __init__(self):
        self.submitted, self.typed_any, self.stopped = [], True, 0
        self.aborted = False

    def start(self):
        pass

    def submit(self, emission):
        self.submitted.append(emission.emission_seq)
        return True

    def stop(self):
        self.stopped += 1

    def abort(self):
        self.aborted = True


class _SessionApp:
    """Duck-typed app double (never touches Tk or real audio)."""

    def __init__(self, config):
        self.config = dict(config)
        self.is_exiting = False
        self.is_processing = False
        self.statuses, self.beeps, self.finished = [], [], 0
        self.indicator = mock.MagicMock()
        self._typer = None

        class Recorder:
            is_recording = False
            starts = 0
            sink = None

            def start_block_stream(inner_self, sink):
                Recorder.starts += 1
                Recorder.sink = sink

            def stop_block_stream(inner_self):
                return None
        self.recorder = Recorder()

    def set_status(self, message, state):
        self.statuses.append((message, state))

    def beep(self, freq, dur):
        self.beeps.append((freq, dur))

    def call_in_ui(self, callback):
        if not self.is_exiting:
            callback()

    def _on_processing_finished(self):
        self.finished += 1
        self.is_processing = False

    def pump(self, speech=6, silence=8, sink=None):
        target = sink if sink is not None else self.recorder.sink
        for _ in range(speech):
            target(SPEECH_BLOCK)
        for _ in range(silence):
            target(SILENCE_BLOCK)


def chunked_config(**extra):
    config = {
        "dictation_mode": "chunked", "cleanup_mode": "cloud", "provider": "openai",
        "provider_base_url": "https://api.openai.com/v1", "model": "gpt-audio",
        "transcription_model": "", "target_language": "English",
        "chunked_engine": "", "polish_mode": "insert", "model_size": "auto",
        "live_engine": "local", "gemini_consent": False,
        "gemini_paid_key_confirmed": False, "cleanup_model": "",
        "model_store_dir": "", "local_models": {}, "api_keys_protected": {},
        "quota_limits": {}, "first_run_guidance_seen": False,
    }
    config.update(extra)
    return config


def wait_until(predicate, timeout=20.0):
    import time as _time
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if predicate():
            return True
        _time.sleep(0.02)
    return False


class SessionWiringTests(unittest.TestCase):
    """main -> DictationSession routing; the classic path stays byte-identical."""

    def _session_app(self, config):
        app = _app(is_exiting=False)
        app.config = dict(config)
        app.beep = mock.MagicMock()          # never sound on this host
        return app

    def test_stop_and_process_routes_to_an_active_session(self):
        app = self._session_app(chunked_config())
        app.dictation = mock.MagicMock()
        app.dictation.is_active = True
        app.recorder = mock.MagicMock()
        app.recorder.is_recording = True

        with mock.patch.object(main.threading, "Thread") as thread:
            app.stop_and_process()
        app.dictation.request_stop.assert_called_once()
        thread.assert_not_called()                       # no classic worker started
        app.recorder.stop_recording.assert_not_called()

    def test_start_recording_routes_session_modes_and_keeps_classic_byte_identical(self):
        app = self._session_app(chunked_config())
        app.dictation = mock.MagicMock()
        app.dictation.maybe_start.return_value = True
        app.recorder = mock.MagicMock()
        app.recorder.is_recording = False

        app.start_recording()
        app.dictation.maybe_start.assert_called_once()
        app.recorder.start_recording.assert_not_called()  # session handled it

        # classic config (NO dictation_mode): maybe_start False -> classic guard
        classic = _app(is_exiting=False)
        classic.config = {"provider_base_url": "https://api.openai.com/v1",
                          "model": "gpt-audio", "transcription_model": ""}
        classic.beep = mock.MagicMock()
        classic.dictation = mock.MagicMock()
        classic.dictation.maybe_start.return_value = False
        classic.recorder = mock.MagicMock()
        classic.recorder.is_recording = False
        classic.start_recording()
        classic.recorder.start_recording.assert_called_once()

    def test_quit_app_aborts_the_session_before_destroying_the_root(self):
        app = self._session_app(chunked_config())
        app.dictation = mock.MagicMock()
        app.tray_icon = mock.MagicMock()
        app.indicator = mock.MagicMock()
        app.root = mock.MagicMock()
        app.clear_hotkeys = mock.MagicMock()

        app.quit_app()
        self.assertTrue(app.is_exiting)
        app.dictation.abort_all.assert_called_once_with("quit")   # M2 extended
        app.root.destroy.assert_called_once()


class SessionSnapshotThreadingTests(unittest.TestCase):
    """D10: the snapshot is built on the UI thread at start; workers read ONLY
    it — mid-session Settings edits apply to the NEXT recording."""

    def test_mid_session_config_edits_never_reach_the_running_snapshot(self):
        app = _SessionApp(chunked_config())
        engine = SlowEngine(base=0.01)
        session = DictationSession(
            app, engine_factory=lambda cfg, store="": engine,
            vad_factory=make_vad,
            typer_factory=lambda _a, _s: RecordingTyper())
        with mock.patch.object(ds, "paste_text"):
            self.assertTrue(session.maybe_start())
            snapshot = SlowEngine.last_config
            self.assertIsInstance(snapshot, object)
            self.assertEqual(snapshot.chunked_engine, "openai")   # "" inherited at start
            # Settings edits mid-session mutate the LIVE dict only
            app.config["model"] = "CHANGED"
            app.config["chunked_engine"] = "groq-whisper"
            app.config["target_language"] = "German"
            app.config["dictation_mode"] = "live"
            self.assertEqual(snapshot.post_model, "gpt-audio")
            self.assertEqual(snapshot.chunked_engine, "openai")
            self.assertEqual(snapshot.target_language, "English")
            self.assertEqual(snapshot.dictation_mode, "chunked")
            with self.assertRaises(Exception):
                snapshot.post_model = "hacked"                # frozen (D10)
            session.abort_all("test_cleanup")
        self.assertEqual(session.state.value, "idle")

    def test_snapshot_carries_only_the_least_privilege_key_set(self):
        app = _SessionApp(chunked_config(provider="openai"))
        app.config["api_keys_protected"] = {}
        config_manager = ds.config_manager
        config_manager.set_vendor_key(app.config, "openai", "sk-1")
        config_manager.set_vendor_key(app.config, "groq", "gsk-other")
        engine = SlowEngine(base=0.01)
        session = DictationSession(
            app, engine_factory=lambda cfg, store="": engine,
            vad_factory=make_vad,
            typer_factory=lambda _a, _s: RecordingTyper())
        with mock.patch.object(ds, "paste_text"):
            self.assertTrue(session.maybe_start())
            snapshot = SlowEngine.last_config
            self.assertEqual(set(snapshot.api_keys), {"openai"})  # least privilege (A3)
            session.abort_all("test_cleanup")


class StopDuringBacklogTests(unittest.TestCase):
    """G1: stop with a slow engine drains BOUNDED and emits strictly ordered."""

    def _run(self, bursts=5, engine=None):
        app = _SessionApp(chunked_config())
        engine = engine or SlowEngine()
        typer = RecordingTyper()
        session = DictationSession(
            app, engine_factory=lambda cfg, store="": engine,
            vad_factory=make_vad, typer_factory=lambda _a, _s: typer)
        with mock.patch.object(ds, "paste_text") as paste:
            import time as _time
            started = _time.monotonic()
            self.assertTrue(session.maybe_start())
            for index in range(bursts):
                app.pump()
                if index < bursts - 1:
                    # let the workers drain between bursts -> distinct units
                    # (merge-on-backlog stays covered by the engine below)
                    _time.sleep(0.12)
            session.request_stop()
            finished = wait_until(lambda: not session.is_active)
            elapsed = _time.monotonic() - started
        return session, app, engine, typer, paste, elapsed, finished

    def test_ordered_drain_is_bounded_and_strictly_sequenced(self):
        session, app, engine, typer, paste, elapsed, finished = self._run()
        self.assertTrue(finished, "drain never finished")
        self.assertLess(elapsed, 15.0, f"drain not bounded: {elapsed:.1f}s")
        emissions = session.emissions
        self.assertTrue(emissions, "no emissions recorded")
        self.assertEqual([e.emission_seq for e in emissions],
                         list(range(len(emissions))))        # 0..n-1 gapless
        self.assertEqual([e.segment_seq for e in emissions],
                         sorted(e.segment_seq for e in emissions))  # segment order
        self.assertTrue(all(e.is_final for e in emissions))
        self.assertEqual(typer.submitted,
                         [e.emission_seq for e in emissions])  # actor got the same order
        # Phase-2 fallback paste: the actor reports typed_any=True -> paste AND
        # its completion beep are skipped (D4: ONE sink per session); exactly
        # the start + stop beeps remain.
        paste.assert_not_called()
        self.assertEqual(len(app.beeps), 2)
        self.assertIn(("Done. Ready for the next recording.", "ready"), app.statuses)
        self.assertFalse(app.is_processing)
        self.assertEqual(session.state.value, "idle")

    def test_arabic_and_english_units_drain_in_order_without_loss(self):
        texts = {0: "مرحبا بالعالم", 1: "hello world", 2: "اللغة العربية",
                 3: "final english unit"}
        engine = SlowEngine(base=0.05, texts=texts)
        session, app, _engine, typer, _paste, _elapsed, finished = self._run(
            bursts=4, engine=engine)
        self.assertTrue(finished)
        emissions = session.emissions
        got = [e.text for e in emissions]
        # gapless strictly-ordered sequencing for BOTH scripts
        self.assertEqual([e.emission_seq for e in emissions],
                         list(range(len(emissions))))
        self.assertEqual([e.segment_seq for e in emissions],
                         sorted(e.segment_seq for e in emissions))
        # the first burst is always unit 0 (queue empty at first offer)
        self.assertEqual(got[0], texts[0])                   # مرحبا بالعالم
        self.assertIn("مرحبا بالعالم", got)                  # non-ASCII survives (D11)
        self.assertTrue(set(got) <= set(texts.values()),     # never invented text
                        got)
        self.assertEqual(typer.submitted, list(range(len(got))))


class QuitMidDrainTests(unittest.TestCase):
    """M2 EXTENDED: quitting mid-drain yields ZERO emissions and ZERO injection."""

    def test_zero_injection_after_exit(self):
        app = _SessionApp(chunked_config())
        engine = SlowEngine(base=0.35)
        typer = RecordingTyper()
        session = DictationSession(
            app, engine_factory=lambda cfg, store="": engine,
            vad_factory=make_vad, typer_factory=lambda _a, _s: typer)
        with mock.patch.object(ds, "paste_text") as paste:
            self.assertTrue(session.maybe_start())
            for _ in range(4):
                app.pump()
            session.request_stop()
            import time as _time
            _time.sleep(0.15)                                # mid-drain (engines busy)
            app.is_exiting = True                            # M2: quit flips FIRST
            session.abort_all("quit")
            self.assertEqual(session.emissions, (), "emissions survived the abort")
            self.assertEqual(session.state.value, "idle")
            self.assertTrue(typer.aborted, "actor must be frozen (M2)")
            paste.assert_not_called()
            submitted_at_abort = list(typer.submitted)
            _time.sleep(0.5)                                 # late workers stay silent
            self.assertEqual(session.emissions, ())
            self.assertEqual(paste.call_count, 0)
            self.assertEqual(typer.submitted, submitted_at_abort)  # no typing after abort
            self.assertEqual(app.finished, 0)                # call_in_ui suppressed (M2)

    def test_quit_app_through_main_aborts_the_session(self):
        # M2 extension at the main level: quit_app flips is_exiting BEFORE
        # abort_all, and injection is gated by both flags downstream.
        app = _app(is_exiting=False)
        app.config = chunked_config()
        app.dictation = mock.MagicMock()
        app.tray_icon = mock.MagicMock()
        app.clear_hotkeys = mock.MagicMock()
        app.quit_app()
        self.assertTrue(app.is_exiting)
        app.dictation.abort_all.assert_called_once_with("quit")


class QuotaNoSilentFailoverTests(unittest.TestCase):
    """G4 (session level): a tiny budget admits ONE dispatch; the rest become
    quota_exhausted notices with the D6 offering state — NO engine switch."""

    def test_exhaustion_notices_without_failover(self):
        app = _SessionApp(chunked_config(quota_limits={"chat:openai.day": 1}))
        engine = SlowEngine(base=0.01)
        session = DictationSession(
            app, engine_factory=lambda cfg, store="": engine,
            vad_factory=make_vad, typer_factory=lambda _a, _s: RecordingTyper())
        with mock.patch.object(ds, "paste_text"):
            self.assertTrue(session.maybe_start())
            for _ in range(4):
                app.pump()
            session.request_stop()
            self.assertTrue(wait_until(lambda: not session.is_active))
        quota_notices = [n for n in session.notices
                         if n["kind"] == "quota_exhausted"]
        self.assertTrue(quota_notices, "expected a quota_exhausted notice")
        self.assertEqual(quota_notices[0]["options"],
                         ("local", "finish_at_stop", "abort_with_clipboard"))
        self.assertEqual(len(engine.processed), 1, engine.processed)  # ONE dispatch
        self.assertEqual(len(session.emissions), 1)
        self.assertEqual([e.emission_seq for e in session.emissions], [0])
        # NO failover: the session engine id never changed
        self.assertEqual(session._engine_id, "chat:openai")


class PublishIdempotenceTests(unittest.TestCase):
    """G1 dedupe: a duplicate replay of one segment never lands twice —
    driven SYNCHRONOUSLY through the session's own drain (no threads), so the
    assertion is exact and race-free."""

    def test_duplicate_publish_of_the_same_seq_is_dropped(self):
        app = _SessionApp(chunked_config())
        session = DictationSession(
            app, engine_factory=lambda cfg, store="": SlowEngine(base=0.0),
            vad_factory=make_vad, typer_factory=lambda _a, _s: RecordingTyper())
        with session._cond:
            session._segments_total = 2
            session._workers_done = True
        emission = EmissionOut("once", segment_seq=0, emission_seq=-1)
        session._publish(0, [emission])
        session._publish(0, [emission])                  # duplicate replay
        session._publish(1, [])                          # failed unit
        session._publish(1, [])                          # duplicate failure
        self.assertEqual(len(session._pending), 1)       # publish-level dedupe
        self.assertEqual(session._failed, {1})
        latest = session._drain_ordered(-1)              # ordered drain (emission_seq)
        self.assertEqual(latest, 0)
        self.assertEqual(session._drain_ordered(latest), 0)   # idempotent re-drain
        texts = [e.text for e in session.emissions]
        self.assertEqual(texts, ["once"])                    # exactly once
        self.assertEqual([e.emission_seq for e in session.emissions], [0])


class WordTrimPropertyTests(unittest.TestCase):
    """G1 trim property (Arabic + English): adjacent units keep seam words
    EXACTLY once — no blind overlap, nothing fabricated, nothing dropped."""

    SAMPLES = {
        "english": [(0.0, 0.2, "word"), (0.9, 1.2, "seam"), (1.3, 1.5, "next"),
                    (1.9, 2.3, "edge")],
        "arabic": [(0.0, 0.3, "\u0645\u0631\u062d\u0628\u0627"),   # مرحبا
                   (1.0, 1.3, "\u0628\u0627\u0644\u0639\u0627\u0644\u0645"),  # بالعالم
                   (1.5, 1.7, "\u0646\u0635\u0641"),              # نصف
                   (2.4, 2.6, "\u0643\u0644\u0645\u0629")],       # كلمة
    }

    def test_units_partition_the_words_exactly_once(self):
        from vad_segmenter import trim_word_timings
        for language, words in self.SAMPLES.items():
            with self.subTest(language=language):
                # three adjacent units covering [0,3): every word whose
                # midpoint sits in a unit is kept by EXACTLY that unit
                kept = []
                for start, end in ((0.0, 1.0), (1.0, 2.0), (2.0, 3.0)):
                    kept.extend(w[2] for w in
                                trim_word_timings(words, unit_offset_s=0.0,
                                                  speech_start_s=start,
                                                  speech_end_s=end))
                self.assertEqual(len(kept), len(set(kept)),    # exactly once
                                 f"{language}: duplicate seam words: {kept}")
                expected = [w[2] for w in words
                            if start_mid(w, 0.0, 3.0)]          # sanity, see below
                self.assertEqual(sorted(kept), sorted(expected))


def start_mid(word, _lo, _hi):
    mid = (word[0] + word[1]) / 2.0
    return _lo <= mid <= _hi


class EngineDefectDispositionTests(unittest.TestCase):
    """A6 s2.3: an engine DEFECT (non-typed exception) dispositions the unit
    with a unit_error notice and processing CONTINUES — the worker thread must
    never die from its own error handler."""

    def test_engine_defect_is_dispositioned_and_the_handler_never_crashes(self):
        # *** FAILING PIN (QA-2026-11-16): production defect reported to
        # architect-orchestrator — the generic `except Exception:` handler in
        # `dictation_session.DictationSession._process_one` reads the name
        # `error`, which is only bound `as error` in the TYPED clauses above it
        # (and deleted at each handler exit), so an engine defect raises
        # NameError instead of calling `_disposition(... "unit_error" notice)`.
        # The handler's own comment promises "CONTINUE the rest"; today the
        # worker thread dies and the unit is lost WITHOUT the degraded notice.
        # NOT fixed by QA (test-only writer); worker-executor owns the fix. ***
        from dictation_types import Segment, SegmentIn

        class BoomEngine:
            def start(self, config):
                pass

            def process(self, segment):
                raise RuntimeError("vendor payload shape drifted")

            def close(self):
                pass

        app = _SessionApp(chunked_config())
        session = DictationSession(
            app, engine_factory=lambda cfg, store="": BoomEngine(),
            vad_factory=make_vad, typer_factory=lambda _a, _s: RecordingTyper())
        resolved = session.preflight(app.config)
        snapshot = build_session_config(
            app.config, session_id="defect-pin", started_at_epoch=1, api_keys={})
        session._install("chunked", resolved, snapshot, BoomEngine(),
                         make_vad(), app.config)
        segment = SegmentIn(segment_seq=0, pcm16=b"\x00" * 640,
                            speech_start_s=0.0, speech_end_s=0.02,
                            audio_seconds=0.02)
        session._segments[0] = Segment(
            session_id=snapshot.session_id, seq=0, audio_ref="ring:0:640",
            start_ms=0, end_ms=20)
        try:
            session._process_one(segment)
        except NameError as error:
            self.fail(
                "PRODUCTION DEFECT (QA failing pin): _process_one's generic "
                f"exception handler raised NameError ({error}) instead of "
                "dispositioning the unit — the worker thread would die here "
                "and the unit_error notice is never recorded.")
        record = session._segments.get(0)
        self.assertIsNotNone(record)
        self.assertEqual(record.status, "failed")             # dispositioned
        self.assertEqual(record.error_kind, "RuntimeError")   # taxonomy, no message
        notices = [n for n in session.notices if n["kind"] == "unit_error"]
        self.assertTrue(notices, "degraded unit_error notice must be recorded (D6)")
        self.assertEqual(session._failed, {0})                # drain can finish
        session.abort_all("test_cleanup")


class RuntimeLogScrapeTests(unittest.TestCase):
    """G5 (runtime): a full session drain with a DISTINCTIVE transcript and a
    real-shaped API key in the live config must never put either into ANY log
    record (D11 — log-scrape over the whole session pipeline)."""

    def test_transcript_and_api_key_never_reach_any_log_record(self):
        import logging
        from llm_errors import LLMError
        transcript_sentinel = "SENTINEL_TRANSCRIPT_9f3cZ\u200f\u0645\u0631\u062d\u0628\u0627"
        key_sentinel = "sk-GHA-SENTINEL-9988"
        captured = []

        class Capture(logging.Handler):
            def emit(self, record):
                captured.append(record.getMessage())

        handlers = []
        loggers = [logging.getLogger(name) for name in
                   ("dictation_session", "chunked_engine", "whisper_http",
                    "typer_actor", "polish_engine", "quota_ledger", "")]
        for logger in loggers:
            handler = Capture()
            previous_level = logger.level
            logger.addHandler(handler)
            logger.setLevel(logging.DEBUG)
            handlers.append((logger, handler, previous_level))
        try:
            # session A: the sentinel transcript flows end-to-end (happy path)
            texts = {0: transcript_sentinel, 1: "ordinary unit"}
            engine = SlowEngine(base=0.01, texts=texts)
            app = _SessionApp(chunked_config(api_key=key_sentinel,
                                             provider_base_url="https://api.x/v1"))
            app.config["api_keys_protected"] = {}
            session = DictationSession(
                app, engine_factory=lambda cfg, store="": engine,
                vad_factory=make_vad, typer_factory=lambda _a, _s: RecordingTyper())
            with mock.patch.object(ds, "paste_text"):
                self.assertTrue(session.maybe_start())
                app.pump()
                session.request_stop()
                self.assertTrue(wait_until(lambda: not session.is_active))
            self.assertIn(transcript_sentinel,
                          [e.text for e in session.emissions])

            # session B: force the LOGGING paths (unit failure -> disposition
            # warning + degraded notice) with the same live key in config.
            class FailingEngine(SlowEngine):
                def process(self, segment):
                    raise LLMError(f"vendor rejected {key_sentinel}")
            app_b = _SessionApp(chunked_config(api_key=key_sentinel,
                                               provider_base_url="https://api.x/v1"))
            app_b.config["api_keys_protected"] = {}
            session_b = DictationSession(
                app_b, engine_factory=lambda cfg, store="": FailingEngine(base=0.0),
                vad_factory=make_vad, typer_factory=lambda _a, _s: RecordingTyper())
            with mock.patch.object(ds, "paste_text"):
                self.assertTrue(session_b.maybe_start())
                app_b.pump()
                session_b.request_stop()
                self.assertTrue(wait_until(lambda: not session_b.is_active))
            # the unit failed WITHOUT leaking its (key-bearing) message:
            # disposition logged kind-only, and the emission never happens.
            self.assertEqual(session_b.emissions, ())
        finally:
            for logger, handler, previous_level in handlers:
                logger.removeHandler(handler)
                logger.setLevel(previous_level)
        self.assertTrue(captured, "capture must be non-vacuous")
        for message in captured:
            self.assertNotIn(transcript_sentinel, message)
            self.assertNotIn(key_sentinel, message)
            self.assertNotIn("\u0645\u0631\u062d\u0628\u0627", message)  # مرحبا


if __name__ == "__main__":
    unittest.main()
