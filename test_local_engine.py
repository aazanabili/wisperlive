"""Local engine tests (plan 2.3): lazy-import guards, RTF hook, tier mapping.

Owner: qa-test-assurance.  Every ML-extra dependency state is SIMULATED
test-side (``sys.modules`` doubles), so the D8 guards are exercised on a host
WITH and WITHOUT faster_whisper / ctranslate2 — nothing here observes or
asserts what this machine happens to have installed, while fake
backend/model-dir seams drive the rest deterministically.

Covers: missing extras -> ``EngineUnavailableError`` (never ImportError out of
the adapter), missing model -> ``ModelNotDownloadedError`` (D7: never fetch),
the measured-RTF hook (honest None until a decode), model-size -> manifest-id
tier mapping behind the capability probe (never an ISA claim), and the exact
transcribe() contract (word_timestamps, independent units, generator consumed).
"""

import io
import os
import tempfile
import types
import unittest
from unittest import mock

import dictation_types as dt
import local_engine
from llm_errors import EngineUnavailableError, ModelNotDownloadedError


def _config(**extra):
    source = {"model_size": "fast"}
    source.update(extra)
    return dt.build_session_config(source, session_id="s", started_at_epoch=1,
                                   api_keys={})


def _segment(seconds=1.0, seq=0):
    return dt.SegmentIn(segment_seq=seq, pcm16=b"\x01\x00" * int(seconds * 16000),
                        speech_start_s=0.0, speech_end_s=seconds,
                        audio_seconds=seconds)


class _Word:
    def __init__(self, start, end, word):
        self.start, self.end, self.word = start, end, word


class _Piece:
    def __init__(self, text, words=()):
        self.text, self.words = text, list(words)


class FakeWhisperModel:
    """Stands in for faster_whisper.WhisperModel (records ctor/transcribe args)."""

    instances = []

    def __init__(self, directory, **kwargs):
        self.directory = directory
        self.kwargs = kwargs
        self.closed = False
        self.pieces = [_Piece(" hello world ", [_Word(0.1, 0.4, " hello"),
                                                _Word(0.5, 0.9, " world")])]
        self.transcribe_kwargs = None
        FakeWhisperModel.instances.append(self)

    def transcribe(self, source, **kwargs):
        self.transcribe_kwargs = kwargs
        self.last_source = source

        def generator():
            for piece in self.pieces:
                yield piece
        return generator(), object()

    def close(self):
        self.closed = True


class LazyImportGuardTests(unittest.TestCase):
    def test_module_imports_without_any_ml_package(self):
        # D8: no top-level faster_whisper/numpy/ctranslate2 import may exist.
        import ast
        with open(local_engine.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        top_level = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_level.extend(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level.append(node.module.split(".")[0])
        for forbidden in ("faster_whisper", "numpy", "ctranslate2", "torch",
                          "onnxruntime"):
            self.assertNotIn(forbidden, top_level,
                             f"{forbidden} must never be a top-level import")

    def test_missing_extras_raise_engine_unavailable_with_actionable_reason(self):
        # ENV-AGNOSTIC: extras SIMULATED absent — ``sys.modules[name] is None``
        # makes `from faster_whisper import ...` raise on hosts with OR without
        # the package installed.
        with mock.patch.dict("sys.modules", {"faster_whisper": None}):
            engine = local_engine.LocalEngine()
            with self.assertRaises(EngineUnavailableError) as ctx:
                engine.start(_config())
            self.assertEqual(ctx.exception.engine_id, "local")
            self.assertIn("requirements-ml", str(ctx.exception))
            self.assertTrue(ctx.exception.reason)
        # process() before start() is a typed unavailability, never a crash
        with self.assertRaises(EngineUnavailableError):
            engine.process(_segment())

    def test_start_reaches_the_missing_model_guard_when_extras_exist(self):
        # ENV-AGNOSTIC: extras SIMULATED present (stub module whose
        # WhisperModel is the fake double) so the missing-model guard is the
        # failure on a host with OR without ML extras installed.
        stub = types.ModuleType("faster_whisper")
        stub.WhisperModel = FakeWhisperModel
        with mock.patch.dict("sys.modules", {"faster_whisper": stub}):
            engine = local_engine.LocalEngine(store_dir=tempfile.mkdtemp())
            with self.assertRaises(ModelNotDownloadedError):
                engine.start(_config())


class MissingModelGuardTests(unittest.TestCase):
    def setUp(self):
        FakeWhisperModel.instances = []

    def test_absent_model_raises_model_not_downloaded_never_fetches(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(local_engine, "_load_backend",
                                  return_value=FakeWhisperModel):
            engine = local_engine.LocalEngine(store_dir=root)
            with self.assertRaises(ModelNotDownloadedError) as ctx:
                engine.start(_config(model_size="fast"))
            self.assertEqual(ctx.exception.model_id, "faster-whisper-base")
            self.assertIn("Settings", str(ctx.exception))
            # D7: NOTHING was created/downloaded in the store dir
            self.assertEqual(os.listdir(root), [])
        self.assertEqual(FakeWhisperModel.instances, [])   # model never constructed

    def test_ready_model_directory_is_found_flat_and_versioned(self):
        with tempfile.TemporaryDirectory() as root:
            # flat layout: <root>/<model_id>/model.bin
            flat = os.path.join(root, "flat-model")
            os.makedirs(flat)
            with open(os.path.join(flat, "model.bin"), "wb") as handle:
                handle.write(b"x")
            self.assertEqual(local_engine.find_model_dir(root, "flat-model"), flat)
            # devops versioned layout: <root>/<id>/<version>/model.bin
            for name in ("1", "2"):
                path = os.path.join(root, "ver-model", name)
                os.makedirs(path)
                with open(os.path.join(path, "model.bin"), "wb") as handle:
                    handle.write(b"x")
            # reversed-alphabetical scan prefers the later-named version
            found = local_engine.find_model_dir(root, "ver-model")
            self.assertTrue(found.endswith(os.path.join("ver-model", "2")))
            # absent -> None (caller raises the typed guard)
            self.assertIsNone(local_engine.find_model_dir(root, "missing-model"))
            self.assertIsNone(local_engine.find_model_dir(
                os.path.join(root, "nope"), "missing-model"))


class StartAndProcessContractTests(unittest.TestCase):
    def setUp(self):
        FakeWhisperModel.instances = []

    def _started_engine(self, store_dir):
        # model dir ready for "faster-whisper-base" (fast tier never probes)
        target = os.path.join(store_dir, "faster-whisper-base")
        os.makedirs(target)
        with open(os.path.join(target, "model.bin"), "wb") as handle:
            handle.write(b"weights")
        engine = local_engine.LocalEngine(store_dir=store_dir)
        with mock.patch.object(local_engine, "_load_backend",
                               return_value=FakeWhisperModel):
            engine.start(_config(model_size="fast"))
        return engine

    def test_start_loads_from_the_local_dir_only(self):
        with tempfile.TemporaryDirectory() as root:
            engine = self._started_engine(root)
            model = FakeWhisperModel.instances[0]
            self.assertEqual(model.directory,
                             os.path.join(root, "faster-whisper-base"))
            self.assertEqual(model.kwargs,
                             {"device": "cpu", "compute_type": "int8"})
            self.assertEqual(engine.model_id, "faster-whisper-base")
            engine.close()

    def test_transcribe_contract_and_emission_shape(self):
        with tempfile.TemporaryDirectory() as root:
            engine = self._started_engine(root)
            emissions = engine.process(_segment(seconds=2.0, seq=7))
            model = FakeWhisperModel.instances[0]
            self.assertEqual(model.transcribe_kwargs["word_timestamps"], True)
            self.assertEqual(model.transcribe_kwargs["language"], None)  # raw (D2)
            self.assertFalse(model.transcribe_kwargs["condition_on_previous_text"])
            self.assertFalse(model.transcribe_kwargs["vad_filter"])
            self.assertIsInstance(model.last_source, io.BytesIO)         # in-memory WAV
            self.assertEqual(len(emissions), 1)
            emission = emissions[0]
            self.assertEqual(emission.text, "hello world")           # joined + trimmed
            self.assertEqual(emission.segment_seq, 7)
            self.assertEqual(emission.kind, "phrase")
            self.assertTrue(emission.is_final)
            # vendor word text is carried VERBATIM (never re-spaced by the adapter)
            self.assertEqual([w.text for w in emission.words],
                             [" hello", " world"])
            self.assertEqual(emission.words[0].start_ms, 100)        # seconds -> ms
            engine.close()

    def test_empty_transcript_yields_no_emission(self):
        with tempfile.TemporaryDirectory() as root:
            engine = self._started_engine(root)
            FakeWhisperModel.instances[0].pieces = [_Piece("   ")]
            self.assertEqual(engine.process(_segment()), [])
            engine.close()

    def test_rtf_hook_is_honest_none_then_a_measured_average(self):
        with tempfile.TemporaryDirectory() as root:
            engine = self._started_engine(root)
            self.assertIsNone(engine.estimated_rtf())        # never a fabricated number
            with mock.patch.object(local_engine.time, "monotonic",
                                   side_effect=[0.0, 1.0]):  # decode cost = 1 s
                engine.process(_segment(seconds=2.0))        # RTF 0.5
            with mock.patch.object(local_engine.time, "monotonic",
                                   side_effect=[0.0, 4.0]):  # decode cost = 4 s
                engine.process(_segment(seconds=2.0))        # RTF 2.0
            self.assertEqual(engine.estimated_rtf(), 1.25)   # mean of measured samples
            engine.close()

    def test_zero_audio_seconds_never_records_an_infinite_rtf(self):
        with tempfile.TemporaryDirectory() as root:
            engine = self._started_engine(root)
            empty = dt.SegmentIn(segment_seq=0, pcm16=b"", speech_start_s=0.0,
                                 speech_end_s=0.0, audio_seconds=0.0)
            engine.process(empty)
            self.assertIsNone(engine.estimated_rtf())        # no sample recorded
            engine.close()

    def test_close_is_idempotent_and_never_raises(self):
        with tempfile.TemporaryDirectory() as root:
            engine = self._started_engine(root)
            model = FakeWhisperModel.instances[0]
            engine.close()
            engine.close()
            self.assertTrue(model.closed)
            self.assertIsNone(engine._model)                 # released for GC
            # a broken model.close() must not propagate (teardown contract)
            FakeWhisperModel.instances[0].close = mock.Mock(
                side_effect=RuntimeError("boom"))
            engine2 = local_engine.LocalEngine()
            engine2._model = FakeWhisperModel.instances[0]
            engine2.close()


class TierMappingTests(unittest.TestCase):
    def test_fast_and_standard_map_without_probing(self):
        with mock.patch.object(local_engine, "capability_probe",
                               side_effect=AssertionError("must not probe")):
            self.assertEqual(local_engine.resolve_model_id("fast"),
                             "faster-whisper-base")
            self.assertEqual(local_engine.resolve_model_id("standard"),
                             "faster-whisper-small")

    def test_auto_maps_by_probe_never_by_isa_claim(self):
        with mock.patch.object(local_engine, "capability_probe",
                               return_value=(True, "")):
            self.assertEqual(local_engine.resolve_model_id("auto"),
                             "faster-whisper-small")
        with mock.patch.object(local_engine, "capability_probe",
                               return_value=(False, "This CPU lacks int8")):
            self.assertEqual(local_engine.resolve_model_id("auto"),
                             "faster-whisper-base")

    def test_accepts_session_config_or_garbage(self):
        self.assertEqual(local_engine.resolve_model_id(_config(model_size="fast")),
                         "faster-whisper-base")
        self.assertEqual(local_engine.resolve_model_id(42),      # -> auto path
                         local_engine.resolve_model_id("auto"))
        self.assertEqual(local_engine.MODEL_IDS["auto"],
                         local_engine.MODEL_IDS["standard"])     # auto may downgrade only

    def test_capability_probe_fails_conservatively_without_extras(self):
        # ENV-AGNOSTIC: ctranslate2 SIMULATED absent (None in sys.modules), so
        # the conservative fallback is asserted on hosts with OR without it.
        with mock.patch.dict("sys.modules", {"ctranslate2": None}):
            capable, reason = local_engine.capability_probe()
        self.assertFalse(capable)
        self.assertIn("not installed", reason)

    def test_probe_failure_never_crashes_startup(self):
        fake_ct2 = mock.Mock()
        fake_ct2.get_supported_compute_types.side_effect = RuntimeError("probe bug")
        with mock.patch.dict("sys.modules", {"ctranslate2": fake_ct2}):
            capable, reason = local_engine.capability_probe()
        self.assertFalse(capable)
        self.assertIn("probe failed", reason)

    def test_probe_reports_capability_from_compute_types(self):
        fake_ct2 = mock.Mock()
        fake_ct2.get_supported_compute_types.return_value = {"int8", "float32"}
        with mock.patch.dict("sys.modules", {"ctranslate2": fake_ct2}):
            capable, reason = local_engine.capability_probe()
        self.assertTrue(capable)
        self.assertEqual(reason, "")
        fake_ct2.get_supported_compute_types.return_value = {"float32"}
        with mock.patch.dict("sys.modules", {"ctranslate2": fake_ct2}):
            capable, reason = local_engine.capability_probe()
        self.assertFalse(capable)
        self.assertIn("int8", reason)


class StoreDirTests(unittest.TestCase):
    def test_configured_value_always_wins(self):
        self.assertEqual(local_engine.default_store_dir("  /custom/dir  "),
                         "/custom/dir")
        self.assertEqual(local_engine.default_store_dir("/custom/dir"),
                         "/custom/dir")
        # non-str / blank falls back to the platform default
        fallback = local_engine.default_store_dir("")
        self.assertEqual(local_engine.default_store_dir(42), fallback)
        self.assertEqual(local_engine.default_store_dir(None), fallback)
        self.assertTrue(fallback.endswith(os.path.join("WhisperLive", "models")))

    def test_platform_default_sits_in_the_data_dir(self):
        configured = local_engine.default_store_dir("")
        if os.name == "nt":
            self.assertTrue(configured.startswith(
                os.environ.get("LOCALAPPDATA",
                               os.path.expanduser("~\\AppData\\Local"))))
            self.assertTrue(configured.endswith(os.path.join("WhisperLive", "models")))
        else:
            self.assertIn("WhisperLive", configured)


if __name__ == "__main__":
    unittest.main()
