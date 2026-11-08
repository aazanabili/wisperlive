"""Tests for the OpenAI-compatible LLM provider client.

These tests mock ``urllib.request.urlopen`` so no network access happens and
exercise the request shape, error classification, and the guarantee that
every request is bounded by an explicit timeout (the regression guard for
the app-freezing hangs).
"""

import base64
import json
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

    def read(self, limit=-1):
        if limit is None or limit < 0:
            return self.body
        return self.body[:limit]

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


if __name__ == "__main__":
    unittest.main()
