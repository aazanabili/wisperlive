"""Wire-format contract tests for llm_payloads.py (plan 2.2).

Owner: qa-test-assurance.  Pins the generic payload byte-for-byte to the
pre-preset shape (text prompt part + ``input_audio {data, format:"wav"}``),
the MiMo ASR documented shape EXACTLY (Data-URL ``input_audio`` with NO
``format`` key + top-level ``asr_options.language``), the language mapping
table, the per-dialect audio size limits, and the dispatcher's fail-safe
fallback.  Pure module tests: no network, no files, no Tk.
"""

import unittest

import llm_payloads
from llm_payloads import (
    DIALECT_MIMO_ASR,
    DIALECT_OPENAI,
    MIMO_ASR_LANGUAGE_AUTO,
    MIMO_ASR_LANGUAGE_MAP,
    MIMO_MAX_AUDIO_BYTES,
    _audio_size_limit,
    _build_transcription_payload,
    _generic_transcription_payload,
    _mimo_asr_language,
    _mimo_transcription_payload,
)

B64 = "UklGRiBmaWtlYXVkaW8="


class GenericPayloadTests(unittest.TestCase):
    def test_generic_payload_is_byte_identical_to_today_shape(self):
        payload = _generic_transcription_payload("gpt-audio", B64)
        self.assertEqual(set(payload), {"model", "messages"})
        message = payload["messages"][0]
        self.assertEqual(message["role"], "user")
        content = message["content"]
        self.assertEqual(len(content), 2)
        # part 0: the literal-transcription text prompt (stage 1, untranslated)
        self.assertEqual(content[0]["type"], "text")
        self.assertTrue(content[0]["text"].strip())
        self.assertIn("\u0627\u0633\u062a\u0645\u0639 \u0625\u0644\u0649 \u0647\u0630\u0627 "
                      "\u0627\u0644\u0645\u0642\u0637\u0639 \u0627\u0644\u0635\u0648\u062a\u064a",
                      content[0]["text"])
        # part 1: raw-base64 input_audio WITH the format key (today's shape)
        self.assertEqual(content[1], {"type": "input_audio",
                                      "input_audio": {"data": B64, "format": "wav"}})

    def test_generic_payload_ignores_target_language(self):
        # The generic dialect has no asr_options; language only feeds stage 2.
        payload = _generic_transcription_payload("gpt-audio", B64)
        self.assertNotIn("asr_options", payload)


class MiMoPayloadTests(unittest.TestCase):
    def payload(self, language="Chinese"):
        return _mimo_transcription_payload("mimo-v2.5-asr", B64, language)

    def test_mimo_payload_exact_documented_shape(self):
        payload = self.payload()
        self.assertEqual(set(payload), {"model", "messages", "asr_options"})
        self.assertEqual(payload["model"], "mimo-v2.5-asr")
        # exactly ONE message with exactly ONE content part - no text part
        self.assertEqual(len(payload["messages"]), 1)
        content = payload["messages"][0]["content"]
        self.assertEqual(len(content), 1)
        self.assertEqual(content[0]["type"], "input_audio")
        self.assertNotIn("text", content[0])
        # ruling R4: NO format key inside input_audio (strict docs contract)
        input_audio = content[0]["input_audio"]
        self.assertNotIn("format", input_audio)
        self.assertEqual(set(input_audio), {"data"})
        # data is a Data URL, not raw base64
        self.assertTrue(input_audio["data"].startswith("data:audio/wav;base64,"))
        self.assertEqual(input_audio["data"],
                         "data:audio/wav;base64," + B64)
        # top-level asr_options carries the language
        self.assertEqual(payload["asr_options"], {"language": "zh"})

    def test_mimo_prompt_part_is_deliberately_absent(self):
        payload = self.payload()
        types = [part["type"] for part in payload["messages"][0]["content"]]
        self.assertEqual(types, ["input_audio"])  # no text/prompt part on the wire


class LanguageMappingTests(unittest.TestCase):
    def test_map_constants_match_documented_values(self):
        self.assertEqual(MIMO_ASR_LANGUAGE_MAP, {"chinese": "zh", "english": "en"})
        self.assertEqual(MIMO_ASR_LANGUAGE_AUTO, "auto")

    def test_chinese_and_english_map_case_insensitively(self):
        for value, expected in (("Chinese", "zh"), ("CHINESE", "zh"),
                                ("chinese", "zh"), ("  Chinese  ", "zh"),
                                ("English", "en"), ("ENGLISH", "en"),
                                (" english", "en")):
            with self.subTest(value=value):
                self.assertEqual(_mimo_asr_language(value), expected)

    def test_everything_else_maps_to_auto(self):
        for value in ("Original language", "", "   ", "French", "العربية",
                      "zh", "en", None, 42, [], {}, b"x"):
            with self.subTest(value=value):
                self.assertEqual(_mimo_asr_language(value), "auto")


class AudioSizeLimitTests(unittest.TestCase):
    def test_per_dialect_limits(self):
        limit, label = _audio_size_limit(DIALECT_OPENAI)
        self.assertEqual(limit, 20 * 1024 * 1024)
        self.assertEqual(label, "20 MB")
        limit, label = _audio_size_limit(DIALECT_MIMO_ASR)
        self.assertEqual(limit, MIMO_MAX_AUDIO_BYTES)
        self.assertEqual(limit, 7 * 1024 * 1024)
        self.assertEqual(label, "7 MB")

    def test_mimo_bound_keeps_base64_under_vendor_10mb_rule(self):
        # 7 MiB raw -> 4*ceil(n/3) chars + 22-char Data-URL prefix < 10,000,000.
        import base64
        worst = MIMO_MAX_AUDIO_BYTES
        encoded_chars = 4 * ((worst + 2) // 3)
        self.assertLessEqual(encoded_chars + len("data:audio/wav;base64,"), 10_000_000)

    def test_unknown_dialect_falls_back_to_generic_limit(self):
        for dialect in ("bogus", None, 42, "", DIALECT_OPENAI.upper()):
            with self.subTest(dialect=dialect):
                limit, label = _audio_size_limit(dialect)
                self.assertEqual((limit, label), (20 * 1024 * 1024, "20 MB"))

    def test_size_gate_and_payload_dispatcher_never_disagree(self):
        # Same fail-safe default for both lookups: an unknown dialect must get
        # the generic bound AND the generic payload shape.
        for dialect in ("bogus", None, 42):
            with self.subTest(dialect=dialect):
                payload = _build_transcription_payload("m", B64, dialect, "Chinese")
                self.assertNotIn("asr_options", payload)
                self.assertEqual(_audio_size_limit(dialect)[1], "20 MB")


class DispatcherTests(unittest.TestCase):
    def test_mimo_dialect_dispatches_to_mimo_shape(self):
        payload = _build_transcription_payload("mimo-v2.5-asr", B64,
                                               DIALECT_MIMO_ASR, "English")
        self.assertIn("asr_options", payload)
        self.assertEqual(payload["asr_options"], {"language": "en"})
        self.assertNotIn("format", payload["messages"][0]["content"][0]["input_audio"])

    def test_openai_dialect_dispatches_to_generic_shape(self):
        payload = _build_transcription_payload("gpt-audio", B64,
                                               DIALECT_OPENAI, "French")
        self.assertEqual(payload, _generic_transcription_payload("gpt-audio", B64))

    def test_unknown_or_missing_dialect_falls_back_to_generic(self):
        for dialect in ("future-dialect", None, 42, "", DIALECT_OPENAI):
            with self.subTest(dialect=dialect):
                payload = _build_transcription_payload("gpt-audio", B64, dialect, "Chinese")
                self.assertEqual(payload, _generic_transcription_payload("gpt-audio", B64))


if __name__ == "__main__":
    unittest.main()
