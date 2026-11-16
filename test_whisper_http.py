"""Groq transcription transport tests (plan 2.3; G4 quota + H4 redaction).

Owner: qa-test-assurance.  All network access goes through the INJECTED
``session`` opener (or a patched urlopen) — this suite never touches the wire.

Covers: the exact multipart shape (V1 field set), 429 -> ``LLMQuotaError``
with a ``HH:MM`` reset hint vs the legacy header-less copy, H4 endpoint/log
redaction, auth/5xx/network classification, bounded reads, D11 repr hygiene,
and the H6 NO-RETRY pin (one send per failure).
"""

import json
import urllib.error
import unittest
from unittest import mock

import whisper_http
from llm_errors import (LLMAuthError, LLMConfigError, LLMError, LLMNetworkError,
                        LLMQuotaError)

WAV = b"RIFF\x00\x00\x00\x00WAVEfake"


class _FakeResponse:
    def __init__(self, payload):
        self._payload = bytes(payload) if isinstance(payload, bytes) else \
            json.dumps(payload).encode("utf-8")
        self._pos = 0

    def read(self, limit=-1):
        if limit is None or limit < 0:
            chunk = self._payload[self._pos:]
            self._pos = len(self._payload)
            return chunk
        chunk = self._payload[self._pos:self._pos + limit]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class MultipartShapeTests(unittest.TestCase):
    def test_request_shape_matches_the_verified_v1_contract(self):
        captured = {}

        def opener(request, timeout=None):
            captured["url"] = request.full_url
            captured["data"] = request.data
            captured["timeout"] = timeout
            captured["method"] = request.get_method()
            captured["headers"] = {k.lower(): v
                                   for k, v in request.header_items()}
            return _FakeResponse({"text": "ok", "words": []})

        result = whisper_http.transcribe_wav(
            WAV, api_key="gsk-1", base_url="https://api.groq.com/openai/v1",
            model="whisper-large-v3-turbo", language="ar", session=opener)

        self.assertEqual(result.text, "ok")
        self.assertEqual(captured["url"],
                         "https://api.groq.com/openai/v1/audio/transcriptions")
        self.assertEqual(captured["timeout"], whisper_http.TRANSCRIPTION_TIMEOUT)
        self.assertEqual(captured["headers"]["authorization"], "Bearer gsk-1")
        content_type = captured["headers"]["content-type"]
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        boundary = content_type.split("boundary=", 1)[1]
        body = captured["data"]
        text = body.decode("utf-8", "replace")
        self.assertTrue(text.startswith(f"--{boundary}\r\n"))
        self.assertIn('name="model"', text)
        self.assertIn("whisper-large-v3-turbo", text)
        self.assertIn('name="response_format"', text)
        self.assertIn("verbose_json", text)
        self.assertIn('name="timestamp_granularities[]"', text)
        self.assertIn('name="language"', text)
        self.assertIn("ar", text)
        self.assertIn('name="file"; filename="segment.wav"', text)
        self.assertIn("Content-Type: audio/wav", text)
        self.assertIn(WAV, body)
        self.assertTrue(body.endswith(f"--{boundary}--\r\n".encode()))
        self.assertEqual(captured["method"], "POST")
        self.assertIn("accept", captured["headers"])

    def test_defaults_apply_when_model_and_language_are_blank(self):
        captured = {}

        def opener(request, timeout=None):
            captured["data"] = request.data
            captured["headers"] = {k.lower(): v
                                   for k, v in request.header_items()}
            return _FakeResponse({"text": "ok"})

        whisper_http.transcribe_wav(WAV, api_key="", language="", session=opener)
        text = captured["data"].decode("utf-8", "replace")
        self.assertIn(whisper_http.DEFAULT_MODEL, text)
        self.assertNotIn('name="language"', text)      # auto-detect omits language
        self.assertNotIn("authorization", captured["headers"])  # keyless allowed (D2)

    def test_empty_wav_fails_before_any_open(self):
        def opener(request, timeout=None):            # pragma: no cover
            raise AssertionError("must not open a request")

        for bad in (b"", None, "not-bytes"):
            with self.subTest(wav=bad):
                with self.assertRaises(LLMError):
                    whisper_http.transcribe_wav(bad, session=opener)


class QuotaClassificationTests(unittest.TestCase):
    """G4: 429 with a parseable reset -> typed LLMQuotaError (engine + hint);
    header-less 429 keeps the legacy generic copy (never a wrong quota claim)."""

    @staticmethod
    def _http_error(code, headers=None, url="https://api.groq.com/openai/v1/audio/transcriptions"):
        return urllib.error.HTTPError(url, code, "err", headers, None)

    def test_429_with_retry_after_yields_quota_error_and_hhmm_hint(self):
        def opener(request, timeout=None):
            raise self._http_error(429, {"Retry-After": "120"})

        with self.assertRaises(LLMQuotaError) as ctx:
            whisper_http.transcribe_wav(WAV, session=opener)
        error = ctx.exception
        self.assertEqual(error.engine_id, "groq-whisper")
        self.assertRegex(error.reset_hint, r"^\d{2}:\d{2}$")
        self.assertIn("rate-limiting", str(error))

    def test_429_with_x_ratelimit_reset_epoch_yields_hint(self):
        import time as real_time
        headers = {"x-ratelimit-reset-requests": str(int(real_time.time()) + 300)}

        def opener(request, timeout=None):
            raise self._http_error(429, headers)

        with self.assertRaises(LLMQuotaError) as ctx:
            whisper_http.transcribe_wav(WAV, session=opener)
        self.assertRegex(ctx.exception.reset_hint, r"^\d{2}:\d{2}$")

    def test_header_less_429_keeps_the_legacy_generic_copy(self):
        def opener(request, timeout=None):
            raise self._http_error(429, {})

        with self.assertRaises(LLMError) as ctx:
            whisper_http.transcribe_wav(WAV, session=opener)
        self.assertNotIsInstance(ctx.exception, LLMQuotaError)  # honest: no hint known
        self.assertEqual(str(ctx.exception), whisper_http.RATE_LIMIT_COPY)
        self.assertIn("rate-limiting", str(ctx.exception))
        self.assertIn("try again", str(ctx.exception))

    def test_401_and_403_are_auth_errors(self):
        for code in (401, 403):
            def opener(request, timeout=None, code=code):
                raise self._http_error(code)

            with self.subTest(code=code):
                with self.assertRaises(LLMAuthError) as ctx:
                    whisper_http.transcribe_wav(WAV, api_key="k", session=opener)
                self.assertIn(str(code), str(ctx.exception))
                self.assertIn("Settings", str(ctx.exception))

    def test_5xx_and_other_http_codes_are_generic_llm_errors(self):
        def server(request, timeout=None):
            raise self._http_error(503)

        with self.assertRaises(LLMError) as ctx:
            whisper_http.transcribe_wav(WAV, session=server)
        self.assertIn("HTTP 503", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, (LLMAuthError, LLMQuotaError,
                                                 LLMNetworkError))

        def client(request, timeout=None):
            raise self._http_error(400)

        with self.assertRaises(LLMError) as ctx:
            whisper_http.transcribe_wav(WAV, session=client)
        self.assertIn("HTTP 400", str(ctx.exception))
        self.assertNotIn("TOP", str(ctx.exception))     # no body echo

    def test_network_failures_are_typed_network_errors(self):
        def refused(request, timeout=None):
            raise urllib.error.URLError("connection refused")

        with self.assertRaises(LLMNetworkError) as ctx:
            whisper_http.transcribe_wav(WAV, session=refused)
        self.assertIn("Could not reach", str(ctx.exception))

        def timed_out(request, timeout=None):
            raise urllib.error.URLError(TimeoutError("timed out"))

        with self.assertRaises(LLMNetworkError) as ctx:
            whisper_http.transcribe_wav(WAV, session=timed_out)
        self.assertIn("timed out", str(ctx.exception))

        def os_error(request, timeout=None):
            raise OSError("network is unreachable")

        with self.assertRaises(LLMNetworkError):
            whisper_http.transcribe_wav(WAV, session=os_error)

    def test_no_retry_on_failure_is_exactly_one_send(self):
        attempts = []

        def failing_429(request, timeout=None):
            attempts.append(request.full_url)
            raise self._http_error(429, {"Retry-After": "60"})

        with self.assertRaises(LLMQuotaError):
            whisper_http.transcribe_wav(WAV, session=failing_429)
        self.assertEqual(len(attempts), 1)              # H6: UI is the retry control

        attempts.clear()

        def failing_network(request, timeout=None):
            attempts.append(request.full_url)
            raise urllib.error.URLError("down")

        with self.assertRaises(LLMNetworkError):
            whisper_http.transcribe_wav(WAV, session=failing_network)
        self.assertEqual(len(attempts), 1)              # no reconnect loop either


class ResponseParsingTests(unittest.TestCase):
    def _open_ok(self, payload):
        return mock.Mock(side_effect=lambda request, timeout=None:
                         _FakeResponse(payload))

    def test_verbose_json_parses_text_and_word_milliseconds(self):
        opener = self._open_ok({"text": "  hello world  ",
                                "words": [{"word": "hello", "start": 0.0, "end": 0.25},
                                          {"word": "world", "start": 0.3, "end": 0.6},
                                          {"word": 42, "start": 0, "end": 1},
                                          "junk"]})
        result = whisper_http.transcribe_wav(WAV, session=opener)
        self.assertEqual(result.text, "hello world")
        self.assertEqual(len(result.words), 2)
        self.assertEqual(result.words[0].start_ms, 0)
        self.assertEqual(result.words[0].end_ms, 250)     # seconds -> ms contract
        self.assertEqual(result.words[1].text, "world")

    def test_repr_hides_the_transcript(self):
        opener = self._open_ok({"text": "SECRET-TRANSCRIPT"})
        result = whisper_http.transcribe_wav(WAV, session=opener)
        self.assertNotIn("SECRET-TRANSCRIPT", repr(result))
        import dataclasses
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.text = "changed"                     # parsed result is immutable (D10)

    def test_non_json_and_wrong_shape_are_user_safe_errors(self):
        for payload in (b"<html>gateway</html>", json.dumps(["list"]),
                        json.dumps({"words": []}), json.dumps({"text": 5})):
            with self.subTest(payload=payload[:30]):
                opener = mock.Mock(side_effect=lambda request, timeout=None:
                                   _FakeResponse(payload))
                with self.assertRaises(LLMError) as ctx:
                    whisper_http.transcribe_wav(WAV, session=opener)
                message = str(ctx.exception)
                self.assertNotIn("<html>", message)       # no echo (D11)

    def test_oversized_response_is_rejected(self):
        big = b"x" * (whisper_http.MAX_RESPONSE_BYTES + 1)

        class _BigResponse(_FakeResponse):
            def __init__(self):
                self._payload = big
                self._pos = 0

        opener = mock.Mock(side_effect=lambda request, timeout=None: _BigResponse())
        with self.assertRaises(LLMError) as ctx:
            whisper_http.transcribe_wav(WAV, session=opener)
        self.assertIn("too large", str(ctx.exception))


class EndpointAndRedactionTests(unittest.TestCase):
    """H4: bad endpoints fail BEFORE any request; log lines carry scheme+host+path."""

    def test_rejected_base_urls_never_open_a_request(self):
        def opener(request, timeout=None):            # pragma: no cover
            raise AssertionError("must not open")

        for base in ("http://insecure.example/v1",
                     "https://user:pass@example.com/v1",
                     "https://example.com/v1?token=TOP",
                     "https://example.com/v1#frag",
                     "ftp://example.com/v1",
                     "api.groq.com/v1"):
            with self.subTest(base=base):
                with self.assertRaises(LLMConfigError):
                    whisper_http.transcribe_wav(WAV, base_url=base, session=opener)

    def test_safe_url_for_log_strips_credentials_query_and_fragment(self):
        cases = [
            ("https://user:pass@example.com/v1/audio?token=TOP#frag",
             "https://example.com/v1/audio"),
            ("https://api.groq.com/openai/v1/audio/transcriptions",
             "https://api.groq.com/openai/v1/audio/transcriptions"),
            ("https://example.com:8443/v1", "https://example.com:8443/v1"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(whisper_http._safe_url_for_log(raw), expected)
                for secret in ("user", "pass", "TOP", "token", "#", "?"):
                    self.assertNotIn(secret, whisper_http._safe_url_for_log(raw))

    def test_success_log_line_carries_size_and_redacted_host_only(self):
        import logging
        captured = []

        def opener(request, timeout=None):
            return _FakeResponse({"text": "ok"})

        logger = logging.getLogger("whisper_http")
        handler = logging.Handler()
        handler.emit = lambda record: captured.append(record.getMessage())
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            whisper_http.transcribe_wav(
                WAV, api_key="gsk-TOPSECRET",
                base_url="https://api.groq.com/openai/v1", session=opener)
        finally:
            logger.removeHandler(handler)
        self.assertTrue(captured)
        for message in captured:
            self.assertNotIn("gsk-TOPSECRET", message)   # G5: keys never logged
            self.assertNotIn('"ok"', message)            # transcript never logged
            self.assertNotIn("RIFF", message)            # audio never logged


if __name__ == "__main__":
    unittest.main()
