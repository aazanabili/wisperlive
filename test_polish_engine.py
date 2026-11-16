"""Polish engine tests (plan 2.3; gate G2 span validity + U3 safe insert).

Owner: qa-test-assurance.  Covers:
* ``apply_polish`` decision matrix: insert default, replace ONLY on a PROVEN
  valid span (mode + Windows + focus proof + fresh TTL), auto-fallback on any
  doubt (never a half-replace),
* the per-session ``SpanRegistry`` (UTF-16 char accounting, invalidation
  triggers, TTL), and
* ``polish_last`` cleanup routing: off verbatim / cloud classic stage 2 /
  local-ollama behind ``require_ollama`` with NO silent cloud fallback (D6).
"""

import time
import unittest
from unittest import mock

import polish_engine
from dictation_types import SPAN_REGISTRY_CAPACITY, SPAN_TTL_S
from llm_errors import EngineUnavailableError, LLMError
from polish_engine import (INSERT_MODE, REPLACE_EXPERIMENTAL, SpanRegistry,
                           apply_polish, polish_last)


class ClipboardDouble:
    def __init__(self):
        self.pasted = []

    def put_and_paste(self, text):
        self.pasted.append(text)


class ActorDouble:
    """Minimal TyperActor duck-type for polish_last (registry/guard/clipboard)."""

    def __init__(self, *, focus_unchanged=True):
        self.registry = SpanRegistry()
        self.focus_guard = mock.MagicMock()
        self.focus_guard.unchanged.return_value = focus_unchanged
        self.clipboard = ClipboardDouble()
        self.tapped = []
        self.typed = []

    def tap_back(self, count):
        self.tapped.append(count)

    def type_text(self, text):
        self.typed.append(text)


def _record(actor, text="dictated text", seq=0, now=None):
    return actor.registry.record_typed(
        seq, text, INSERT_MODE, int(time.time()) if now is None else now)


class ApplyPolishMatrixTests(unittest.TestCase):
    def _apply(self, cleaned="cleaned", *, mode=INSERT_MODE, focus=True,
               windows=True, now=1000, registry=None, tap=None, typed=None):
        registry = registry or SpanRegistry()
        clipboard = ClipboardDouble()
        result = apply_polish(
            cleaned, registry=registry, mode=mode, focus_unchanged=focus,
            tap_back=tap if tap is not None else mock.MagicMock(),
            type_text=typed if typed is not None else mock.MagicMock(),
            clipboard=clipboard, now_epoch=now, on_windows=windows)
        return result, clipboard, registry

    def test_empty_or_non_string_text_is_none(self):
        for cleaned in ("", None, 42, []):
            with self.subTest(cleaned=cleaned):
                result, clipboard, _ = self._apply(cleaned)
                self.assertEqual(result, "none")
                self.assertEqual(clipboard.pasted, [])

    def test_default_mode_always_inserts_with_clipboard(self):
        registry = SpanRegistry()
        result, clipboard, reg = self._apply(mode=INSERT_MODE, registry=registry)
        self.assertEqual(result, "insert")
        self.assertEqual(clipboard.pasted, ["cleaned"])
        # the insert itself invalidates spans (the caret moved)
        registry.record_typed(0, "prior", INSERT_MODE, 1000)
        result2, clipboard2, _ = self._apply(mode=INSERT_MODE, registry=registry)
        self.assertEqual(result2, "insert")

    def test_replace_only_on_proven_valid_span(self):
        tap, typed = mock.MagicMock(), mock.MagicMock()
        registry = SpanRegistry()
        span = registry.record_typed(3, "just dictated", INSERT_MODE, 1000)
        result, clipboard, _ = self._apply(
            mode=REPLACE_EXPERIMENTAL, focus=True, windows=True, now=1000 + 5,
            registry=registry, tap=tap, typed=typed)
        self.assertEqual(result, "replace")
        tap.assert_called_once_with(span.length)         # SHIFT+VK_LEFT x length (U.4)
        typed.assert_called_once_with("cleaned")
        self.assertEqual(clipboard.pasted, [])           # replace never pastes
        # post-replace: spans invalidated (target changed under us)
        self.assertTrue(all(not s.valid for s in registry.spans))

    def test_replace_falls_back_to_insert_on_every_doubt(self):
        cases = [
            {"mode": INSERT_MODE},                       # not the replace mode
            {"focus": False},                            # focus not PROVEN unchanged
            {"windows": False},                          # not Windows
        ]
        for overrides in cases:
            with self.subTest(**overrides):
                registry = SpanRegistry()
                registry.record_typed(1, "text", INSERT_MODE, 1000)
                kwargs = dict(mode=REPLACE_EXPERIMENTAL, focus=True, windows=True,
                              now=1001, registry=registry)
                kwargs.update(overrides)
                tap, typed = mock.MagicMock(), mock.MagicMock()
                result, clipboard, _ = self._apply(tap=tap, typed=typed, **kwargs)
                self.assertEqual(result, "insert")
                tap.assert_not_called()
                typed.assert_not_called()
                self.assertEqual(clipboard.pasted, ["cleaned"])

    def test_replace_without_any_valid_span_falls_back_to_insert(self):
        tap, typed = mock.MagicMock(), mock.MagicMock()
        result, clipboard, _ = self._apply(
            mode=REPLACE_EXPERIMENTAL, tap=tap, typed=typed)   # empty registry
        self.assertEqual(result, "insert")
        tap.assert_not_called()
        typed.assert_not_called()
        self.assertEqual(clipboard.pasted, ["cleaned"])

    def test_replace_with_expired_span_falls_back_to_insert(self):
        registry = SpanRegistry()
        registry.record_typed(1, "old", INSERT_MODE, 1000)
        tap, typed = mock.MagicMock(), mock.MagicMock()
        result, clipboard, _ = self._apply(
            mode=REPLACE_EXPERIMENTAL, now=1000 + SPAN_TTL_S + 1,
            registry=registry, tap=tap, typed=typed)
        self.assertEqual(result, "insert")
        tap.assert_not_called()
        self.assertEqual(clipboard.pasted, ["cleaned"])

    def test_failed_tap_back_never_leaves_a_half_replace(self):
        registry = SpanRegistry()
        registry.record_typed(1, "text to replace", INSERT_MODE, 1000)
        typed = mock.MagicMock()
        result, clipboard, _ = self._apply(
            mode=REPLACE_EXPERIMENTAL, now=1001, registry=registry,
            tap=mock.MagicMock(side_effect=RuntimeError("SendInput failed")),
            typed=typed)
        self.assertEqual(result, "insert")               # doubt -> safe insert
        typed.assert_not_called()                        # never typed the swap
        self.assertEqual(clipboard.pasted, ["cleaned"])  # user still gets the result


class SpanRegistryTests(unittest.TestCase):
    def test_record_typed_accounts_utf16_units_and_words(self):
        registry = SpanRegistry()
        span = registry.record_typed(0, "hello world", INSERT_MODE, 1000)
        self.assertEqual(span.char_count, 11)            # UTF-16 units
        self.assertEqual(span.word_count, 2)
        self.assertEqual(span.length, 11)                # characters for select-back
        self.assertEqual(span.insertion_mode, INSERT_MODE)
        self.assertTrue(span.valid)
        self.assertEqual(span.span_id, 1)
        # Arabic: char_count counts UTF-16 units, not code points blindly
        arabic = registry.record_typed(1, "مرحبا", INSERT_MODE, 1001)
        self.assertEqual(arabic.char_count, len("مرحبا".encode("utf-16-le")) // 2)
        self.assertEqual(arabic.word_count, 1)
        self.assertEqual(arabic.span_id, 2)               # ids strictly increase

    def test_newer_record_invalidates_older_spans(self):
        registry = SpanRegistry()
        registry.record_typed(0, "first", INSERT_MODE, 1000)
        registry.record_typed(1, "second", INSERT_MODE, 1001)
        self.assertTrue(registry.spans[0].valid)          # newest valid
        self.assertFalse(registry.spans[1].valid)         # caret moved (rule a)

    def test_invalidate_all_and_by_id(self):
        registry = SpanRegistry()
        registry.record_typed(0, "one", INSERT_MODE, 1000)
        registry.record_typed(1, "two", INSERT_MODE, 1001)
        registry.invalidate(registry.spans[0].span_id)
        self.assertFalse(registry.spans[0].valid)
        registry.invalidate_all()
        self.assertTrue(all(not s.valid for s in registry.spans))

    def test_latest_valid_ttl_window(self):
        registry = SpanRegistry()
        registry.record_typed(0, "fresh", INSERT_MODE, 1000)
        self.assertIsNotNone(registry.latest_valid(1000 + SPAN_TTL_S))
        self.assertIsNone(registry.latest_valid(1000 + SPAN_TTL_S + 1))

    def test_registry_capacity_is_bounded(self):
        registry = SpanRegistry()
        for seq in range(10):
            registry.record_typed(seq, f"u{seq}", INSERT_MODE, 1000)
        self.assertEqual(len(registry.spans), SPAN_REGISTRY_CAPACITY)  # drop-oldest

    def test_span_carries_no_text(self):
        import dataclasses
        fields = {f.name for f in dataclasses.fields(
            polish_engine.TypedSpan)}
        self.assertNotIn("text", fields)


class PolishLastRoutingTests(unittest.TestCase):
    def test_off_returns_the_raw_text_without_any_network(self):
        actor = ActorDouble()
        with mock.patch.object(polish_engine, "llm_api") as api:
            result = polish_last("raw text", actor, {"cleanup_mode": "off",
                                                     "polish_mode": INSERT_MODE})
        self.assertEqual(result, "insert")
        api.post_process_text.assert_not_called()
        self.assertEqual(actor.clipboard.pasted, ["raw text"])

    def test_cloud_routes_through_classic_stage_two_with_config_values(self):
        actor = ActorDouble()
        config = {"cleanup_mode": "cloud", "target_language": "French",
                  "api_key": "sk-1", "provider_base_url": "https://api.x/v1",
                  "model": "gpt-audio", "polish_mode": INSERT_MODE}
        with mock.patch.object(polish_engine, "llm_api") as api:
            api.post_process_text.return_value = "CLEANED"
            result = polish_last("raw", actor, config)
        api.post_process_text.assert_called_once_with(
            "raw", "French", "sk-1", "https://api.x/v1", "gpt-audio")
        self.assertEqual(result, "insert")
        self.assertEqual(actor.clipboard.pasted, ["CLEANED"])

    def test_local_ollama_gates_through_presence_keyless(self):
        actor = ActorDouble()
        config = {"cleanup_mode": "local-ollama", "target_language": "",
                  "cleanup_model": "qwen", "model": "other",
                  "polish_mode": INSERT_MODE}
        with mock.patch.object(polish_engine, "llm_api") as api, \
                mock.patch.object(polish_engine, "require_ollama",
                                  return_value="http://localhost:11434/v1") as gate:
            api.post_process_text.return_value = "LOCAL CLEAN"
            result = polish_last("raw", actor, config)
        gate.assert_called_once_with()
        api.post_process_text.assert_called_once_with(
            "raw", "", "", "http://localhost:11434/v1", "qwen")
        self.assertEqual(result, "insert")

    def test_absent_ollama_propagates_and_never_touches_clipboard(self):
        actor = ActorDouble()
        config = {"cleanup_mode": "local-ollama", "polish_mode": INSERT_MODE}
        with mock.patch.object(polish_engine, "require_ollama",
                               side_effect=EngineUnavailableError(
                                   "Ollama is not running.", engine_id="cleanup",
                                   reason="ollama unreachable")), \
                mock.patch.object(polish_engine, "llm_api") as api:
            with self.assertRaises(EngineUnavailableError):
                polish_last("raw", actor, config)
            api.post_process_text.assert_not_called()   # NEVER a silent cloud fallback
        self.assertEqual(actor.clipboard.pasted, [])
        self.assertEqual(actor.typed, [])

    def test_cleanup_failure_propagates_without_polishing_the_raw_text(self):
        actor = ActorDouble()
        config = {"cleanup_mode": "cloud", "target_language": "",
                  "api_key": "k", "provider_base_url": "https://x/v1",
                  "model": "m", "polish_mode": INSERT_MODE}
        with mock.patch.object(polish_engine, "llm_api") as api:
            api.post_process_text.side_effect = LLMError("cleanup exploded")
            with self.assertRaises(LLMError):
                polish_last("raw", actor, config)
        self.assertEqual(actor.clipboard.pasted, [])     # raw NEVER polished (D6)
        self.assertEqual(actor.typed, [])


class RequireOllamaTests(unittest.TestCase):
    def _call(self, side_effect):
        with mock.patch.object(polish_engine.urllib.request, "urlopen",
                               side_effect=side_effect):
            return polish_engine.require_ollama()

    def test_any_http_answer_proves_presence_even_404(self):
        import urllib.error
        response_404 = urllib.error.HTTPError("http://localhost:11434/v1", 404,
                                              "Not Found", {}, None)
        self.assertEqual(self._call(response_404),
                         "http://localhost:11434/v1")

    def test_connection_failure_raises_typed_actionable_error(self):
        import urllib.error
        for failure in (urllib.error.URLError("refused"),
                        OSError("network unreachable")):
            with self.subTest(failure=type(failure).__name__):
                with self.assertRaises(EngineUnavailableError) as ctx:
                    self._call(failure)
                self.assertEqual(ctx.exception.engine_id, "cleanup")
                self.assertIn("ollama serve", str(ctx.exception))
                self.assertIn("Settings", str(ctx.exception))

    def test_timeout_counts_as_absent(self):
        with self.assertRaises(EngineUnavailableError):
            self._call(TimeoutError("timed out"))

    def test_log_line_carries_the_failure_type_only(self):
        import urllib.error
        with mock.patch.object(polish_engine, "logger") as logger, \
                mock.patch.object(polish_engine.urllib.request, "urlopen",
                                  side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(EngineUnavailableError):
                polish_engine.require_ollama()
        logger.info.assert_called_once()
        logged = logger.info.call_args.args[0] % logger.info.call_args.args[1:]
        self.assertIn("URLError", logged)               # failure TYPE only (D11)
        self.assertNotIn("refused", logged)             # never the raw reason text
        self.assertNotIn("http://", logged)             # never an endpoint/key carrier


if __name__ == "__main__":
    unittest.main()
