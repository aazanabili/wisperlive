"""Chunked cloud engine tests (plan 2.3; D1 classic pipeline retained).

Owner: qa-test-assurance.  Covers:
* the D5 per-engine minimum segment table (10 / 20 / 5 s) + quota id map,
* fail-closed start (Groq without key) and the not-started guard,
* cleanup routing cloud / local-ollama / off with the A6 invariant that a
  cleanup FAILURE RAISES (never a silent raw fallback — G4/D6),
* the classic temp-WAV file contract (caller-deletes) and the Groq multipart
  path with D5 midpoint word trimming,
* G4: a transport 429 surfaces as LLMQuotaError with NO retry/failover.
"""

import os
import unittest
from unittest import mock

import chunked_engine
import dictation_types as dt
from llm_errors import EngineUnavailableError, LLMConfigError, LLMError, LLMQuotaError


def _config(**extra):
    api_keys = extra.pop("api_keys", None)
    if api_keys is None:
        api_keys = {"openai": "sk-test"}
    source = {
        "provider": "openai", "provider_base_url": "https://api.openai.com/v1",
        "model": "gpt-audio", "transcription_model": "",
        "chunked_engine": "", "cleanup_mode": "cloud",
        "target_language": "English", "polish_mode": "insert",
        "live_engine": "local", "model_size": "auto",
    }
    source.update(extra)
    return dt.build_session_config(source, session_id="s", started_at_epoch=1,
                                   api_keys=api_keys)


def _segment(seconds=1.0, seq=3):
    return dt.SegmentIn(segment_seq=seq,
                        pcm16=b"\x01\x00" * int(seconds * 16000),
                        speech_start_s=0.0, speech_end_s=seconds,
                        audio_seconds=seconds)


class MinSegmentTableTests(unittest.TestCase):
    def test_d5_minimums_per_engine(self):
        self.assertEqual(chunked_engine.min_segment_seconds("groq-whisper"), 10.0)
        self.assertEqual(chunked_engine.min_segment_seconds("google-gemini"), 20.0)
        self.assertEqual(chunked_engine.min_segment_seconds("xiaomi-mimo"), 5.0)
        self.assertEqual(chunked_engine.min_segment_seconds("openai"), 0.0)
        self.assertEqual(chunked_engine.min_segment_seconds("unknown"), 0.0)
        for garbage in (None, 42, object(), []):
            self.assertEqual(chunked_engine.min_segment_seconds(garbage), 0.0)

    def test_quota_engine_id_vocabulary(self):
        chat = _config(provider="openai", chunked_engine="")
        self.assertEqual(chunked_engine.quota_engine_id(chat), "chat:openai")
        preset = _config(provider="mistral", chunked_engine="")
        self.assertEqual(chunked_engine.quota_engine_id(preset), "chat:mistral")
        groq = _config(chunked_engine="groq-whisper")
        self.assertEqual(chunked_engine.quota_engine_id(groq), "groq-whisper")
        mimo = _config(provider="xiaomi-mimo", chunked_engine="xiaomi-mimo")
        self.assertEqual(chunked_engine.quota_engine_id(mimo), "chat:xiaomi-mimo")


class FailClosedStartTests(unittest.TestCase):
    def test_groq_without_key_fails_closed_before_any_dispatch(self):
        engine = chunked_engine.ChunkedEngine()
        with self.assertRaises(LLMConfigError) as ctx:
            engine.start(_config(chunked_engine="groq-whisper", api_keys={}))
        self.assertIn("Groq", str(ctx.exception))

    def test_process_before_start_is_typed_unavailability(self):
        engine = chunked_engine.ChunkedEngine()
        with self.assertRaises(EngineUnavailableError) as ctx:
            engine.process(_segment())
        self.assertEqual(ctx.exception.reason, "not started")
        engine.start(_config())
        engine.close()
        with self.assertRaises(EngineUnavailableError):
            engine.process(_segment())            # closed -> not started again

    def test_groq_with_key_starts(self):
        engine = chunked_engine.ChunkedEngine()
        engine.start(_config(chunked_engine="groq-whisper",
                             api_keys={"groq": "gsk-1"}))
        engine.close()


class CleanupRoutingTests(unittest.TestCase):
    """D2: cloud runs the classic stage 2, off is raw, ollama reuses the chat
    path behind the presence gate — and a failure NEVER falls back to raw."""

    def _process_with_cleanup(self, mode, **extra):
        engine = chunked_engine.ChunkedEngine()
        config = _config(cleanup_mode=mode, **extra)
        engine.start(config)
        with mock.patch.object(chunked_engine, "llm_api") as api, \
                mock.patch.object(chunked_engine, "require_ollama",
                                  return_value="http://localhost:11434/v1") as ollama, \
                mock.patch.object(chunked_engine.whisper_http,
                                  "transcribe_wav") as transport:
            transport.return_value = type("T", (), {"text": "raw transcript",
                                                    "words": ()})()
            api.transcribe_audio.return_value = "raw transcript"
            api.post_process_text.return_value = "CLEANED"
            emissions = engine.process(_segment())
        engine.close()
        return emissions, api, ollama, transport

    def test_off_emits_raw_without_any_cleanup_call(self):
        emissions, api, _ollama, transport = self._process_with_cleanup(
            "off", provider="openai")
        self.assertEqual(emissions[0].text, "raw transcript")
        api.post_process_text.assert_not_called()
        transport.assert_not_called()                # chat path uses llm_api stages

    def test_cloud_routes_to_classic_stage_two_with_provider_credentials(self):
        with mock.patch.object(chunked_engine, "llm_api") as api:
            api.transcribe_audio.return_value = "RAW TEXT"
            api.post_process_text.return_value = "FINAL TEXT"
            engine = chunked_engine.ChunkedEngine()
            engine.start(_config(cleanup_mode="cloud"))
            emissions = engine.process(_segment())
        self.assertEqual(emissions[0].text, "FINAL TEXT")
        api.transcribe_audio.assert_called_once()
        api.post_process_text.assert_called_once_with(
            "RAW TEXT", "English", "sk-test", "https://api.openai.com/v1",
            "gpt-audio")

    def test_local_ollama_gates_through_presence_with_keyless_chat(self):
        emissions, api, ollama, _transport = self._process_with_cleanup(
            "local-ollama", cleanup_model="qwen", model="fallback-model")
        self.assertEqual(emissions[0].text, "CLEANED")
        ollama.assert_called_once_with()              # presence gate FIRST
        api.post_process_text.assert_called_once_with(
            "raw transcript", "English", "", "http://localhost:11434/v1",
            "qwen")                                   # no key; cleanup_model wins

    def test_local_ollama_cleanup_inherits_post_model_when_blank(self):
        _emissions, api, _ollama, _transport = self._process_with_cleanup(
            "local-ollama", cleanup_model="", model="gpt-audio")
        self.assertEqual(api.post_process_text.call_args.args[4], "gpt-audio")

    def test_cleanup_failure_propagates_never_falls_back_to_raw(self):
        engine = chunked_engine.ChunkedEngine()
        engine.start(_config(cleanup_mode="cloud"))
        with mock.patch.object(chunked_engine, "llm_api") as api:
            api.transcribe_audio.return_value = "RAW"
            api.post_process_text.side_effect = LLMError("cleanup exploded")
            with self.assertRaises(LLMError) as ctx:
                engine.process(_segment())
        self.assertEqual(str(ctx.exception), "cleanup exploded")
        engine.close()

    def test_absent_ollama_raises_typed_error_before_cleanup(self):
        engine = chunked_engine.ChunkedEngine()
        engine.start(_config(cleanup_mode="local-ollama"))
        with mock.patch.object(chunked_engine, "require_ollama",
                               side_effect=EngineUnavailableError(
                                   "Ollama is not running.", engine_id="cleanup",
                                   reason="ollama unreachable")), \
                mock.patch.object(chunked_engine, "llm_api") as api:
            with self.assertRaises(EngineUnavailableError) as ctx:
                engine.process(_segment())
            self.assertEqual(ctx.exception.engine_id, "cleanup")
            api.post_process_text.assert_not_called()   # never a silent cloud fallback
        engine.close()

    def test_unknown_cleanup_mode_fails_closed(self):
        engine = chunked_engine.ChunkedEngine()
        engine.start(_config(cleanup_mode="cloud"))
        engine._config = dt.replace(engine._config, cleanup_mode="weird-mode")
        with mock.patch.object(chunked_engine, "llm_api") as api:
            api.transcribe_audio.return_value = "RAW"
            with self.assertRaises(EngineUnavailableError):
                engine.process(_segment())
            api.post_process_text.assert_not_called()
        engine.close()

    def test_empty_cleanup_output_yields_no_emission(self):
        engine = chunked_engine.ChunkedEngine()
        engine.start(_config(cleanup_mode="off"))
        with mock.patch.object(chunked_engine, "llm_api") as api:
            api.transcribe_audio.return_value = "   "
            self.assertEqual(engine.process(_segment()), [])
        engine.close()


class ClassicFileContractTests(unittest.TestCase):
    """D1: the chat path keeps the temp-WAV caller-deletes contract."""

    def test_temp_wav_exists_during_the_call_and_is_deleted_after(self):
        observed = {}

        def fake_transcribe(path, *_args, **_kwargs):
            observed["existed"] = os.path.isfile(path)
            observed["path"] = path
            with open(path, "rb") as handle:
                observed["bytes"] = handle.read()
            return "RAW"

        engine = chunked_engine.ChunkedEngine()
        engine.start(_config(cleanup_mode="off"))
        with mock.patch.object(chunked_engine, "llm_api") as api:
            api.transcribe_audio.side_effect = fake_transcribe
            engine.process(_segment())
        self.assertTrue(observed["existed"])           # file present DURING the call
        self.assertIn(b"RIFF", observed["bytes"][:4])   # real WAV bytes written
        self.assertFalse(os.path.exists(observed["path"]))  # deleted AFTER (finally)
        engine.close()

    def test_temp_wav_deleted_even_when_the_transport_raises(self):
        engine = chunked_engine.ChunkedEngine()
        engine.start(_config(cleanup_mode="off"))
        captured = {}

        def boom(path, *_args, **_kwargs):
            captured["path"] = path
            raise LLMError("wire failed")

        with mock.patch.object(chunked_engine, "llm_api") as api:
            api.transcribe_audio.side_effect = boom
            with self.assertRaises(LLMError):
                engine.process(_segment())
        self.assertFalse(os.path.exists(captured["path"]))
        engine.close()


class GroqMultipartPathTests(unittest.TestCase):
    def test_uses_registry_asr_endpoint_model_and_language(self):
        engine = chunked_engine.ChunkedEngine()
        config = _config(chunked_engine="groq-whisper", api_keys={"groq": "gsk-1"},
                         chunked_asr_model="", target_language="Arabic",
                         cleanup_mode="off")
        engine.start(config)
        result = type("T", (), {"text": "transcript", "words": ()})()
        with mock.patch.object(chunked_engine.whisper_http,
                               "transcribe_wav", return_value=result) as send:
            emissions = engine.process(_segment())
        kwargs = send.call_args.kwargs
        self.assertEqual(kwargs["base_url"], "https://api.groq.com/openai/v1")
        self.assertEqual(kwargs["model"], chunked_engine.whisper_http.DEFAULT_MODEL)
        self.assertEqual(kwargs["language"], "ar")
        self.assertEqual(kwargs["api_key"], "gsk-1")
        self.assertIsInstance(send.call_args.args[0], bytes)   # in-memory WAV
        self.assertEqual(emissions[0].text, "transcript")
        engine.close()

    def test_word_timings_are_midpoint_trimmed_against_the_unit_span(self):
        engine = chunked_engine.ChunkedEngine()
        engine.start(_config(chunked_engine="groq-whisper",
                             api_keys={"groq": "gsk-1"}, cleanup_mode="off"))
        word_in = dt.WordSpan(start_ms=100, end_ms=300, text="in")
        word_out = dt.WordSpan(start_ms=9000, end_ms=9500, text="out")
        # unit speech span [10 s, 11 s] in session time (offset 10)
        segment = dt.SegmentIn(segment_seq=0, pcm16=b"\x00" * 640,
                               speech_start_s=10.0, speech_end_s=11.0,
                               audio_seconds=1.0)
        result = type("T", (), {"text": "in", "words": (word_in, word_out)})()
        with mock.patch.object(chunked_engine.whisper_http,
                               "transcribe_wav", return_value=result):
            emissions = engine.process(segment)
        # 100ms+10s midpoint inside the span; 9000ms+10s = 19s outside -> dropped
        # (D5: no blind overlap, vendor overhang trimmed, never guessed)
        self.assertEqual([w.text for w in emissions[0].words], ["in"])
        engine.close()

    def test_multipart_body_shape_reaches_the_wire(self):
        # G4/G3-adjacent: the builder output is what transport sends — pin the
        # request shape through the REAL llm_payloads builder.
        import llm_payloads
        body = llm_payloads.build_multipart_body(
            "BOUND", [("model", "whisper-large-v3-turbo"),
                      ("response_format", "verbose_json")],
            ("file", "segment.wav", "audio/wav", b"RIFFDATA"))
        text = body.decode("utf-8", "replace")
        self.assertTrue(text.startswith("--BOUND\r\n"))
        self.assertIn('name="model"', text)
        self.assertIn('filename="segment.wav"', text)
        self.assertIn("Content-Type: audio/wav", text)
        self.assertIn(b"RIFFDATA", body)
        self.assertTrue(body.endswith(b"--BOUND--\r\n"))


class QuotaPropagationTests(unittest.TestCase):
    """G4: a 429 from the transport surfaces as LLMQuotaError, the unit is
    dispositioned by the caller, and NO code path substitutes another vendor."""

    def test_transport_quota_error_passes_through_untouched(self):
        engine = chunked_engine.ChunkedEngine()
        engine.start(_config(chunked_engine="groq-whisper",
                             api_keys={"groq": "gsk-1"}, cleanup_mode="off"))
        quota = LLMQuotaError("Groq is rate-limiting transcription. Paused until 14:05.",
                              engine_id="groq-whisper", reset_hint="14:05")
        with mock.patch.object(chunked_engine.whisper_http,
                               "transcribe_wav", side_effect=quota) as send:
            with self.assertRaises(LLMQuotaError) as ctx:
                engine.process(_segment())
        self.assertEqual(ctx.exception.engine_id, "groq-whisper")
        self.assertEqual(ctx.exception.reset_hint, "14:05")
        self.assertEqual(send.call_count, 1)          # NO retry (H6) — no failover
        engine.close()

    def test_no_engine_substitution_on_failure(self):
        # the engine never rewrites config.chunked_engine after a failure
        engine = chunked_engine.ChunkedEngine()
        config = _config(chunked_engine="groq-whisper", api_keys={"groq": "gsk-1"})
        engine.start(config)
        with mock.patch.object(chunked_engine.whisper_http,
                               "transcribe_wav",
                               side_effect=LLMQuotaError("429", engine_id="groq-whisper")):
            with self.assertRaises(LLMQuotaError):
                engine.process(_segment())
        self.assertEqual(engine._config.chunked_engine, "groq-whisper")
        self.assertEqual(config.chunked_engine, "groq-whisper")
        engine.close()


if __name__ == "__main__":
    unittest.main()
