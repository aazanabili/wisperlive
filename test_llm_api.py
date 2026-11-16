"""Tests for the OpenAI-compatible LLM provider client.

These tests mock ``urllib.request.urlopen`` so no network access happens and
exercise the request shape, error classification, and the guarantee that
every request is bounded by an explicit timeout (the regression guard for
the app-freezing hangs).

Reconciliation (core section 12.7): the ``FakeResponse.read`` cursor mirrors
the adopted H5 chunked-read loop, and the seven converted/hardening cases
(H1 audio cap, H2 boundary, H3 redaction, H4 credential-URL rejection, H7
HTTP 400/429 mapping) carry the survival of the deleted ``test_gemini_api``
suite (disposition map in core section 12.8 / the Phase 5 QA report).
"""

import base64
import io
import json
import logging
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

import llm_api


class FakeResponse:
    def __init__(self, payload):
        if isinstance(payload, (bytes, bytearray)):
            self.body = bytes(payload)
        else:
            self.body = json.dumps(payload).encode("utf-8")
        # H5 cursor: sequential chunk reads must advance, never replay.
        self._pos = 0

    def read(self, limit=-1):
        if limit is None or limit < 0:
            chunk = self.body[self._pos:]
            self._pos = len(self.body)
            return chunk
        chunk = self.body[self._pos:self._pos + limit]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def headers_of(request):
    return {key.lower(): value for key, value in request.header_items()}


class EndpointTests(unittest.TestCase):
    def test_empty_base_url_is_a_config_error(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            with self.assertRaisesRegex(llm_api.LLMConfigError, "Base URL is not set"):
                llm_api.fetch_models("", "key")
        urlopen.assert_not_called()

    def test_base_url_without_scheme_is_a_config_error(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            with self.assertRaisesRegex(llm_api.LLMConfigError, "invalid"):
                llm_api.fetch_models("api.openai.com/v1", "key")
        urlopen.assert_not_called()

    def test_non_http_scheme_is_a_config_error(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            with self.assertRaisesRegex(llm_api.LLMConfigError, "invalid"):
                llm_api.fetch_models("ftp://api.example.com/v1", "key")
        urlopen.assert_not_called()

    def test_models_endpoint_appended_to_base_url(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse({"data": []})) as urlopen:
            llm_api.fetch_models("https://example.com/v1/", "key")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.com/v1/models")


class FetchModelsTests(unittest.TestCase):
    def fetch(self, payload, api_key="sk-test", base_url="https://example.com/v1", **kwargs):
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(payload)) as urlopen:
            models = llm_api.fetch_models(base_url, api_key, **kwargs)
        return models, urlopen

    def test_returns_sorted_unique_model_ids(self):
        payload = {"data": [{"id": "b-model"}, {"id": "a-model"}, {"id": "a-model"}, {"id": " "}, "raw-model", 42]}
        models, _ = self.fetch(payload)
        self.assertEqual(models, ["a-model", "b-model", "raw-model"])

    def test_sends_bearer_auth_when_key_provided(self):
        _, urlopen = self.fetch({"data": []}, api_key="sk-test")
        request = urlopen.call_args.args[0]
        self.assertEqual(headers_of(request)["authorization"], "Bearer sk-test")

    def test_omits_auth_header_without_key(self):
        _, urlopen = self.fetch({"data": []}, api_key="")
        request = urlopen.call_args.args[0]
        self.assertNotIn("authorization", headers_of(request))

    def test_auth_failure_401_is_classified(self):
        error = urllib.error.HTTPError("https://example.com/v1/models", 401, "Unauthorized", None, None)
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(llm_api.LLMAuthError, r"401.*API key"):
                llm_api.fetch_models("https://example.com/v1", "sk-bad")

    def test_auth_failure_403_is_classified(self):
        error = urllib.error.HTTPError("https://example.com/v1/models", 403, "Forbidden", None, None)
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(llm_api.LLMAuthError, r"403"):
                llm_api.fetch_models("https://example.com/v1", "sk-bad")

    def test_server_error_is_reported_with_status_code(self):
        error = urllib.error.HTTPError("https://example.com/v1/models", 500, "Server Error", None, None)
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(llm_api.LLMError, r"HTTP 500"):
                llm_api.fetch_models("https://example.com/v1", "sk-test")

    def test_unreachable_provider_is_a_network_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("name resolution failed")):
            with self.assertRaisesRegex(llm_api.LLMNetworkError, "Could not reach"):
                llm_api.fetch_models("https://example.com/v1", "sk-test")

    def test_connect_timeout_is_reported(self):
        wrapped = urllib.error.URLError(TimeoutError("timed out"))
        with mock.patch("urllib.request.urlopen", side_effect=wrapped):
            with self.assertRaisesRegex(llm_api.LLMNetworkError, "timed out"):
                llm_api.fetch_models("https://example.com/v1", "sk-test")

    def test_read_timeout_is_reported(self):
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaisesRegex(llm_api.LLMNetworkError, "timed out"):
                llm_api.fetch_models("https://example.com/v1", "sk-test")

    def test_non_json_response_is_reported(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(b"<html>gateway</html>")):
            with self.assertRaisesRegex(llm_api.LLMError, "non-JSON"):
                llm_api.fetch_models("https://example.com/v1", "sk-test")

    def test_unexpected_model_list_format_is_reported(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse({"models": []})):
            with self.assertRaisesRegex(llm_api.LLMError, "model list format"):
                llm_api.fetch_models("https://example.com/v1", "sk-test")

    def test_request_timeout_is_always_set(self):
        # Regression guard: a provider that never answers must not hang the
        # caller forever; the request is always bounded by a timeout.
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse({"data": []})) as urlopen:
            llm_api.fetch_models("https://example.com/v1", "sk-test")
        timeout = urlopen.call_args.kwargs.get("timeout")
        self.assertIsInstance(timeout, (int, float))
        self.assertGreater(timeout, 0)

    # core 12.7.5 (H4): a credential- or query-bearing base URL is rejected
    # BEFORE any network call - secret-leak vector + string-concat bug.
    def test_base_url_with_userinfo_or_query_is_rejected(self):
        credential_urls = (
            "https://user:pass@example.com/v1",
            "https://example.com/v1?key=TOPSECRET",
            "https://example.com/v1#TOPSECRET",
        )
        for base_url in credential_urls:
            with self.subTest(base_url=base_url):
                with mock.patch("urllib.request.urlopen") as urlopen:
                    with self.assertRaisesRegex(
                        llm_api.LLMConfigError, "must not contain credentials or a query string"
                    ):
                        llm_api.fetch_models(base_url, "sk-test")
                urlopen.assert_not_called()  # rejected client-side, no request is issued


class ProcessAudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.audio_path = os.path.join(self.temp.name, "clip.wav")
        with open(self.audio_path, "wb") as handle:
            handle.write(b"RIFFfakeaudio")

    def call(self, transcription_response, post_response, **kwargs):
        """Run the pipeline with one mocked response per stage."""
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=[FakeResponse(transcription_response), FakeResponse(post_response)],
        ) as urlopen:
            text = llm_api.process_audio(
                self.audio_path,
                kwargs.pop("target_language", "French"),
                kwargs.pop("api_key", "sk-test"),
                kwargs.pop("base_url", "https://example.com/v1"),
                kwargs.pop("model", "gpt-audio"),
                **kwargs,
            )
        return text, urlopen

    def test_success_runs_transcription_then_post_processing(self):
        text, urlopen = self.call(
            {"choices": [{"message": {"content": "  raw transcript  "}}]},
            {"choices": [{"message": {"content": "  final text  "}}]},
        )
        self.assertEqual(text, "final text")
        self.assertEqual(urlopen.call_count, 2)

        transcription_request = urlopen.call_args_list[0].args[0]
        self.assertEqual(transcription_request.full_url, "https://example.com/v1/chat/completions")
        body = json.loads(transcription_request.data.decode("utf-8"))
        self.assertEqual(body["model"], "gpt-audio")
        parts = body["messages"][0]["content"]
        self.assertEqual(parts[1]["type"], "input_audio")
        self.assertEqual(parts[1]["input_audio"]["format"], "wav")
        self.assertEqual(parts[1]["input_audio"]["data"], base64.b64encode(b"RIFFfakeaudio").decode("ascii"))
        self.assertNotIn("French", parts[0]["text"])

        post_request = urlopen.call_args_list[1].args[0]
        self.assertEqual(post_request.full_url, "https://example.com/v1/chat/completions")
        post_body = json.loads(post_request.data.decode("utf-8"))
        post_prompt = post_body["messages"][0]["content"]
        self.assertIn("raw transcript", post_prompt)
        self.assertIn("French", post_prompt)

        for request_call in urlopen.call_args_list:
            self.assertEqual(request_call.kwargs.get("timeout"), llm_api.GENERATION_TIMEOUT)
            self.assertEqual(headers_of(request_call.args[0])["authorization"], "Bearer sk-test")

    def test_post_processing_failure_does_not_fall_back_to_raw_text(self):
        error = urllib.error.HTTPError("https://example.com/v1/chat/completions", 500, "Server Error", None, None)
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=[FakeResponse({"choices": [{"message": {"content": "raw"}}]}), error],
        ):
            with self.assertRaisesRegex(llm_api.LLMError, r"HTTP 500"):
                llm_api.process_audio(self.audio_path, "French", "sk-test", "https://example.com/v1", "gpt-audio")

    def test_content_parts_are_joined(self):
        def parts(*words):
            return {"choices": [{"message": {"content": [{"type": "text", "text": word} for word in words]}}]}

        text, _ = self.call(parts("hello ", "world"), parts("bonjour", " le monde"))
        self.assertEqual(text, "bonjour le monde")

    def test_missing_model_is_a_config_error(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            with self.assertRaisesRegex(llm_api.LLMConfigError, "No model is selected"):
                llm_api.process_audio(self.audio_path, "French", "sk-test", "https://example.com/v1", "")
        urlopen.assert_not_called()

    def test_missing_base_url_is_a_config_error(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            with self.assertRaisesRegex(llm_api.LLMConfigError, "Base URL is not set"):
                llm_api.process_audio(self.audio_path, "French", "sk-test", "", "gpt-audio")
        urlopen.assert_not_called()

    def test_auth_failure_is_classified(self):
        error = urllib.error.HTTPError("https://example.com/v1/chat/completions", 401, "Unauthorized", None, None)
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(llm_api.LLMAuthError, r"401"):
                llm_api.process_audio(self.audio_path, "French", "sk-bad", "https://example.com/v1", "gpt-audio")

    def test_network_failure_is_classified(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
            with self.assertRaisesRegex(llm_api.LLMNetworkError, "Could not reach"):
                llm_api.process_audio(self.audio_path, "French", "sk-test", "https://example.com/v1", "gpt-audio")

    def test_timeout_is_reported(self):
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaisesRegex(llm_api.LLMNetworkError, "timed out"):
                llm_api.process_audio(self.audio_path, "French", "sk-test", "https://example.com/v1", "gpt-audio")

    def test_unexpected_response_is_reported(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse({"data": []})):
            with self.assertRaisesRegex(llm_api.LLMError, "unexpected response"):
                llm_api.process_audio(self.audio_path, "French", "sk-test", "https://example.com/v1", "gpt-audio")

    def test_empty_completion_is_reported(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse({"choices": [{"message": {"content": "   "}}]})):
            with self.assertRaisesRegex(llm_api.LLMError, "empty completion"):
                llm_api.process_audio(self.audio_path, "French", "sk-test", "https://example.com/v1", "gpt-audio")

    def test_request_timeout_is_always_set(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse({"choices": []})) as urlopen:
            with self.assertRaises(llm_api.LLMError):
                llm_api.process_audio(self.audio_path, "French", "sk-test", "https://example.com/v1", "gpt-audio")
        timeout = urlopen.call_args.kwargs.get("timeout")
        self.assertIsInstance(timeout, (int, float))
        self.assertGreater(timeout, 0)

    # --- core 12.7 ADDs: converted test_gemini_api survival + H1-H4/H7 hardening ---

    # core 12.7.1 (H1; re-added from test_gemini_api): unreadable/missing wav is
    # a user-safe error raised BEFORE any network call.
    def test_missing_audio_file_maps_to_user_error(self):
        missing = os.path.join(self.temp.name, "does-not-exist.wav")

        with mock.patch("urllib.request.urlopen") as urlopen:
            with self.assertRaises(llm_api.LLMError) as ctx:
                llm_api.process_audio(missing, "French", "sk-test", "https://example.com/v1", "gpt-audio")

        self.assertEqual(str(ctx.exception), "The recorded audio file could not be read. Please try again.")
        self.assertIsInstance(ctx.exception.__cause__, OSError)  # cause preserved for diagnostics
        urlopen.assert_not_called()

    # core 12.7.2 (H1 cap): recordings over the shipped 20 MiB cap are refused
    # before base64 encoding and before any network call.
    def test_audio_file_too_large_is_rejected(self):
        self.assertEqual(llm_api.MAX_AUDIO_BYTES, 20 * 1024 * 1024)  # shipped contract
        big = os.path.join(self.temp.name, "huge.wav")
        with open(big, "wb") as handle:
            handle.write(b"\x00" * (llm_api.MAX_AUDIO_BYTES + 1))

        with mock.patch("urllib.request.urlopen") as urlopen:
            with self.assertRaises(llm_api.LLMError) as ctx:
                llm_api.process_audio(big, "French", "sk-test", "https://example.com/v1", "gpt-audio")

        self.assertIn("too large to process (over 20 MB)", str(ctx.exception))
        urlopen.assert_not_called()

    # core 12.7.3 (H2; re-added from test_gemini_api): an unexpected exception at
    # the boundary translates to the generic message, logs once, leaks nothing.
    def test_unexpected_exception_maps_to_generic_message(self):
        error = RuntimeError("internal detail")
        with mock.patch.object(llm_api, "_request_json", side_effect=error), \
                mock.patch.object(llm_api.logger, "exception") as logged:
            with self.assertRaises(llm_api.LLMError) as ctx:
                llm_api.process_audio(
                    self.audio_path, "French", "sk-test", "https://example.com/v1", "gpt-audio"
                )

        self.assertEqual(str(ctx.exception), "Something went wrong while contacting the provider.")
        self.assertIs(ctx.exception.__cause__, error)
        logged.assert_called_once()
        self.assertNotIn("internal detail", str(ctx.exception))

    # core 12.7.4 (H3; supersedes gemini test_no_transcript_content_is_logged):
    # neither transcript, API key, nor a URL credential may reach any log record.
    def test_no_transcript_or_credentials_in_logs(self):
        sentinel = "SENTINEL_TRANSCRIPT_7f3aX"
        key = "sk-SECRET-9988"
        captured = []

        class Capture(logging.Handler):
            def emit(self, record):
                captured.append(record.getMessage())

        target = logging.getLogger("llm_api")
        handler = Capture()
        old_level = target.level
        target.addHandler(handler)
        target.setLevel(logging.DEBUG)
        try:
            # both pipeline stages, success path, distinctive transcript + key
            with mock.patch("urllib.request.urlopen", side_effect=[
                FakeResponse({"choices": [{"message": {"content": sentinel}}]}),
                FakeResponse({"choices": [{"message": {"content": "CLEAN OUTPUT"}}]}),
            ]):
                text = llm_api.process_audio(
                    self.audio_path, "French", key, "https://example.com/v1", "gpt-audio"
                )
            self.assertEqual(text, "CLEAN OUTPUT")

            # failure path with the same key
            failure = urllib.error.HTTPError(
                "https://example.com/v1/chat/completions", 500, "Server Error", None, None
            )
            with mock.patch("urllib.request.urlopen", side_effect=failure):
                with self.assertRaises(llm_api.LLMError):
                    llm_api.process_audio(
                        self.audio_path, "French", key, "https://example.com/v1", "gpt-audio"
                    )

            # ?key=-style credential URL: rejected client-side (H4), never logged
            with self.assertRaises(llm_api.LLMConfigError):
                llm_api.fetch_models("https://example.com/v1?key=TOPSECRET", key)
        finally:
            target.removeHandler(handler)
            target.setLevel(old_level)

        self.assertGreater(len(captured), 0, "capture must be non-vacuous")
        for message in captured:
            self.assertNotIn(sentinel, message)
            self.assertNotIn(key, message)
            self.assertNotIn("TOPSECRET", message)
        # direct proof of the redaction helper: userinfo, query, fragment stripped
        self.assertEqual(
            llm_api._safe_url_for_log("https://user:pass@example.com/v1?key=TOPSECRET#frag"),
            "https://example.com/v1",
        )

    # core 12.7.6 (H7, merged gemini blocked-prompt/value-error/invalid-argument
    # cases): HTTP 400 maps to a user-safe message with NO body echo and is not
    # misclassified as auth/network.
    def test_http_400_maps_to_user_safe_message_without_body_echo(self):
        body = b'{"error": {"message": "BLOCKED_PROMPT_DETAIL"}}'
        error = urllib.error.HTTPError(
            "https://example.com/v1/chat/completions", 400, "Bad Request", None, io.BytesIO(body)
        )
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(llm_api.LLMError) as ctx:
                llm_api.process_audio(
                    self.audio_path, "French", "sk-test", "https://example.com/v1", "gpt-audio"
                )

        message = str(ctx.exception)
        self.assertIn("HTTP 400", message)                      # classified...
        self.assertNotIn("BLOCKED_PROMPT_DETAIL", message)      # ...without echoing the body
        self.assertNotIn("blocked", message.lower())
        self.assertNotIsInstance(ctx.exception, (llm_api.LLMAuthError, llm_api.LLMNetworkError))

    # core 12.7.7 (H7): HTTP 429 maps to the rate-limit message, not a network error.
    def test_http_429_maps_to_rate_limit_message(self):
        error = urllib.error.HTTPError(
            "https://example.com/v1/chat/completions", 429, "Too Many Requests", None, None
        )
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(llm_api.LLMError) as ctx:
                llm_api.process_audio(
                    self.audio_path, "French", "sk-test", "https://example.com/v1", "gpt-audio"
                )

        self.assertIn("rate-limiting", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, llm_api.LLMNetworkError)


class DialectContractTests(unittest.TestCase):
    """T-2026-11-16-provider-presets: per-dialect size gate, stage-model
    fallback, MiMo end-to-end wire shape, and target_language scoping."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.audio_path = os.path.join(self.temp.name, "clip.wav")
        with open(self.audio_path, "wb") as handle:
            handle.write(b"RIFFfakeaudio")

    def write_audio(self, name, size):
        path = os.path.join(self.temp.name, name)
        with open(path, "wb") as handle:
            handle.write(b"\x00" * size)
        return path

    # --- per-dialect H1 size gate ------------------------------------------
    def test_generic_dialect_rejects_over_20mb_before_network(self):
        import llm_payloads
        big = self.write_audio("huge-generic.wav",
                               llm_payloads._audio_size_limit("openai")[0] + 1)
        with mock.patch("urllib.request.urlopen") as urlopen:
            with self.assertRaises(llm_api.LLMError) as ctx:
                llm_api.transcribe_audio(big, "sk-test", "https://example.com/v1",
                                         "gpt-audio", dialect="openai")
        # message pinned verbatim (pre-existing contract)
        self.assertEqual(
            str(ctx.exception),
            "The recording is too large to process (over 20 MB). "
            "Please record a shorter clip.")
        urlopen.assert_not_called()

    def test_mimo_dialect_rejects_over_7mb_before_network(self):
        import llm_payloads
        self.assertEqual(llm_payloads._audio_size_limit("mimo-asr"),
                         (7 * 1024 * 1024, "7 MB"))
        big = self.write_audio("huge-mimo.wav",
                               llm_payloads.MIMO_MAX_AUDIO_BYTES + 1)
        with mock.patch("urllib.request.urlopen") as urlopen:
            with self.assertRaises(llm_api.LLMError) as ctx:
                llm_api.transcribe_audio(big, "tp-test",
                                         "https://api.xiaomimimo.com/v1",
                                         "mimo-v2.5-asr", dialect="mimo-asr")
        self.assertEqual(
            str(ctx.exception),
            "The recording is too large to process (over 7 MB). "
            "Please record a shorter clip.")
        urlopen.assert_not_called()

    def test_generic_dialect_accepts_7mb_plus_one(self):
        # The 7 MB cap is MiMo-specific: the generic dialect must NOT reject
        # a recording that only exceeds the MiMo bound.
        import llm_payloads
        edge = self.write_audio("edge-7mb.wav",
                                llm_payloads.MIMO_MAX_AUDIO_BYTES + 1)
        response = FakeResponse({"choices": [{"message": {"content": "ok"}}]})
        with mock.patch("urllib.request.urlopen",
                        return_value=response) as urlopen:
            text = llm_api.transcribe_audio(edge, "sk-test",
                                            "https://example.com/v1", "gpt-audio",
                                            dialect="openai")
        self.assertEqual(text, "ok")
        urlopen.assert_called_once()

    def test_unknown_dialect_uses_generic_20mb_gate(self):
        import llm_payloads
        big = self.write_audio("huge-unknown.wav",
                               llm_payloads._audio_size_limit("openai")[0] + 1)
        with mock.patch("urllib.request.urlopen") as urlopen:
            with self.assertRaisesRegex(llm_api.LLMError, r"over 20 MB"):
                llm_api.transcribe_audio(big, "sk-test", "https://example.com/v1",
                                         "gpt-audio", dialect="future-dialect")
        urlopen.assert_not_called()

    # --- stage-model fallback (process_audio) -------------------------------
    def test_transcription_model_overrides_stage_one_and_stage_two_keeps_model(self):
        with mock.patch("urllib.request.urlopen", side_effect=[
            FakeResponse({"choices": [{"message": {"content": "raw"}}]}),
            FakeResponse({"choices": [{"message": {"content": "final"}}]}),
        ]) as urlopen:
            text = llm_api.process_audio(
                self.audio_path, "French", "sk-test", "https://example.com/v1",
                model="mimo-v2.6-flash", dialect="mimo-asr",
                transcription_model="mimo-v2.5-asr")
        self.assertEqual(text, "final")
        stage_one = json.loads(urlopen.call_args_list[0].args[0].data.decode("utf-8"))
        stage_two = json.loads(urlopen.call_args_list[1].args[0].data.decode("utf-8"))
        self.assertEqual(stage_one["model"], "mimo-v2.5-asr")
        self.assertEqual(stage_two["model"], "mimo-v2.6-flash")

    def test_empty_transcription_model_falls_back_to_model(self):
        with mock.patch("urllib.request.urlopen", side_effect=[
            FakeResponse({"choices": [{"message": {"content": "raw"}}]}),
            FakeResponse({"choices": [{"message": {"content": "final"}}]}),
        ]) as urlopen:
            llm_api.process_audio(
                self.audio_path, "French", "sk-test", "https://example.com/v1",
                model="gpt-audio", dialect="openai", transcription_model="")
        stage_one = json.loads(urlopen.call_args_list[0].args[0].data.decode("utf-8"))
        stage_two = json.loads(urlopen.call_args_list[1].args[0].data.decode("utf-8"))
        self.assertEqual(stage_one["model"], "gpt-audio")
        self.assertEqual(stage_two["model"], "gpt-audio")

    def test_blank_transcription_model_surfaces_config_error_before_network(self):
        # CONTRACT PIN: stage-1 model = `transcription_model or model` (plan
        # 3.1 step 2.2, literal `or`). Whitespace is truthy in Python, so a
        # whitespace-only value does NOT fall back - it reaches _require_model,
        # which strips and raises the existing user-safe config error BEFORE
        # any network call. Unreachable through the app (save_payload strips,
        # M8/M9 fill, main snapshots .strip()), so this pins fail-safe
        # behaviour for direct API callers; it does not crash or leak a request.
        with mock.patch("urllib.request.urlopen") as urlopen:
            with self.assertRaisesRegex(llm_api.LLMConfigError,
                                        "No model is selected"):
                llm_api.process_audio(
                    self.audio_path, "French", "sk-test",
                    "https://example.com/v1", model="gpt-audio",
                    dialect="openai", transcription_model="   ")
        urlopen.assert_not_called()

    # --- MiMo end-to-end wire shape -----------------------------------------
    def test_mimo_end_to_end_request_shape_through_urlopen(self):
        response = FakeResponse({"choices": [{"message": {"content": "\u0645\u0631\u062d\u0628\u0627"}}]})
        with mock.patch("urllib.request.urlopen",
                        return_value=response) as urlopen:
            text = llm_api.transcribe_audio(
                self.audio_path, "tp-key", "https://api.xiaomimimo.com/v1",
                "mimo-v2.5-asr", dialect="mimo-asr", target_language="Chinese")
        self.assertEqual(text, "\u0645\u0631\u062d\u0628\u0627")

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url,
                         "https://api.xiaomimimo.com/v1/chat/completions")
        headers = headers_of(request)
        self.assertEqual(headers["authorization"], "Bearer tp-key")
        self.assertEqual(headers["content-type"], "application/json")

        body = json.loads(request.data.decode("utf-8"))
        expected_b64 = base64.b64encode(b"RIFFfakeaudio").decode("ascii")
        self.assertEqual(body, {
            "model": "mimo-v2.5-asr",
            "messages": [{"role": "user", "content": [
                {"type": "input_audio",
                 "input_audio": {
                     "data": "data:audio/wav;base64," + expected_b64}},
            ]}],
            "asr_options": {"language": "zh"},
        })
        # strict docs contract: NO format key anywhere in the wire body
        self.assertNotIn(
            "format", body["messages"][0]["content"][0]["input_audio"])
        self.assertNotIn("text", body["messages"][0]["content"][0])
        timeout = urlopen.call_args.kwargs.get("timeout")
        self.assertEqual(timeout, llm_api.GENERATION_TIMEOUT)

    def test_generic_dialect_ignores_target_language(self):
        response = FakeResponse({"choices": [{"message": {"content": "ok"}}]})
        with mock.patch("urllib.request.urlopen",
                        return_value=response) as urlopen:
            llm_api.transcribe_audio(self.audio_path, "sk-test",
                                     "https://example.com/v1", "gpt-audio",
                                     dialect="openai", target_language="French")
        body = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertNotIn("asr_options", body)
        content = body["messages"][0]["content"]
        self.assertEqual(len(content), 2)          # text prompt + input_audio
        self.assertEqual(content[0]["type"], "text")
        self.assertNotIn("French", content[0]["text"])
        self.assertEqual(content[1]["input_audio"]["format"], "wav")


class PostProcessTextTests(unittest.TestCase):
    def call(self, target_language="French", response=None, **kwargs):
        if response is None:
            response = {"choices": [{"message": {"content": "  final text  "}}]}
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(response)) as urlopen:
            text = llm_api.post_process_text(
                kwargs.pop("raw_text", "دبلك... عندنا دي تتم ربعك"),
                target_language,
                kwargs.pop("api_key", "sk-test"),
                kwargs.pop("base_url", "https://example.com/v1"),
                kwargs.pop("model", "gpt-audio"),
                **kwargs,
            )
        return text, urlopen

    def prompt_of(self, urlopen):
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.com/v1/chat/completions")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["model"], "gpt-audio")
        content = body["messages"][0]["content"]
        self.assertIsInstance(content, str)
        self.assertNotIn("input_audio", content)
        return content

    def test_returns_cleaned_text(self):
        text, urlopen = self.call()
        self.assertEqual(text, "final text")
        self.assertEqual(urlopen.call_args.kwargs.get("timeout"), llm_api.GENERATION_TIMEOUT)
        self.assertEqual(headers_of(urlopen.call_args.args[0])["authorization"], "Bearer sk-test")

    def test_prompt_carries_raw_transcript_and_cleanup_rules(self):
        _, urlopen = self.call()
        prompt = self.prompt_of(urlopen)
        self.assertIn("دبلك... عندنا دي تتم ربعك", prompt)
        self.assertIn("WhisperLive", prompt)
        self.assertIn("بالإنجليزية", prompt)
        self.assertIn("نيّة المتحدث", prompt)

    def test_translation_instruction_included_when_language_selected(self):
        _, urlopen = self.call(target_language="French")
        prompt = self.prompt_of(urlopen)
        self.assertIn("French", prompt)
        self.assertIn("ترجم المخرج النهائي", prompt)

    def test_no_translation_for_original_language(self):
        _, urlopen = self.call(target_language=llm_api.NO_TRANSLATION)
        prompt = self.prompt_of(urlopen)
        self.assertIn("لا تترجم", prompt)
        self.assertNotIn("ترجم المخرج النهائي", prompt)

    def test_empty_target_language_skips_translation(self):
        _, urlopen = self.call(target_language="")
        prompt = self.prompt_of(urlopen)
        self.assertIn("لا تترجم", prompt)
        self.assertNotIn("ترجم المخرج النهائي", prompt)

    def test_missing_model_is_a_config_error(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            with self.assertRaisesRegex(llm_api.LLMConfigError, "No model is selected"):
                llm_api.post_process_text("raw", "French", "sk-test", "https://example.com/v1", "")
        urlopen.assert_not_called()

    def test_empty_completion_is_reported(self):
        with self.assertRaisesRegex(llm_api.LLMError, "empty completion"):
            self.call(response={"choices": [{"message": {"content": "   "}}]})

    def test_unexpected_response_is_reported(self):
        with self.assertRaisesRegex(llm_api.LLMError, "unexpected response"):
            self.call(response={"data": []})


class LlmErrorsReExportIdentityTests(unittest.TestCase):
    """dictation-modes Step 1.4 / A3 T17: llm_api re-exports the EXACT class
    objects from the pure ``llm_errors`` leaf (pinning from the llm_api side;
    ``test_llm_errors`` cross-checks the same contract from the leaf side)."""

    RE_EXPORTED = (
        "ConsentRequiredError", "DictationError", "EngineUnavailableError",
        "FocusLostError", "LLMAuthError", "LLMConfigError", "LLMError",
        "LLMNetworkError", "LLMQuotaError", "ModelNotDownloadedError",
    )

    def test_every_reexported_error_is_the_identical_object(self):
        import llm_errors
        for name in self.RE_EXPORTED:
            with self.subTest(name=name):
                self.assertIs(getattr(llm_api, name), getattr(llm_errors, name))

    def test_classic_except_boundaries_keep_their_exact_meaning(self):
        import llm_errors
        # LLMQuotaError instances still satisfy every classic boundary
        error = llm_errors.LLMQuotaError("paused", engine_id="groq-whisper")
        self.assertIsInstance(error, llm_api.LLMError)
        with self.assertRaises(llm_api.LLMError):
            raise error
        # the session umbrella stays a SIBLING (FocusLost never caught as LLM)
        self.assertNotIsInstance(llm_errors.FocusLostError("x"), llm_api.LLMError)
        self.assertIsInstance(llm_errors.FocusLostError("x"), llm_api.DictationError)

    def test_quota_error_constructor_kwargs_survive_the_reexport(self):
        error = llm_api.LLMQuotaError("paused", engine_id="groq-whisper",
                                      reset_hint="14:05")
        self.assertEqual((error.engine_id, error.reset_hint),
                         ("groq-whisper", "14:05"))
        self.assertEqual(llm_api.ModelNotDownloadedError("m",
                                                         model_id="fw").model_id, "fw")
        self.assertEqual(llm_api.EngineUnavailableError(
            "x", engine_id="local", reason="r").reason, "r")


if __name__ == "__main__":
    unittest.main()
