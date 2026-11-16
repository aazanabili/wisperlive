"""Error-taxonomy tests (plan 2.3 matrix): hierarchy + llm_api identity pins.

Owner: qa-test-assurance.  The dictation-modes feature MOVED the classic
``LLM*`` classes into the pure ``llm_errors`` leaf; ``llm_api`` must re-export
the exact SAME class objects (plan Step 1.4 / A3 T17 identity pins) so every
existing ``except llm_api.LLMError`` boundary keeps its meaning, and the
session umbrella ``DictationError`` must stay a SIBLING of ``LLMError`` (A3
Round-2 R3) so classic boundaries never silently swallow new-mode errors.
"""

import unittest

import llm_api
import llm_errors
from llm_errors import (ConsentRequiredError, DictationError,  # noqa: F401
                        EngineUnavailableError, FocusLostError, LLMAuthError,
                        LLMConfigError, LLMError, LLMNetworkError, LLMQuotaError,
                        ModelNotDownloadedError)

RE_EXPORTED = (
    "ConsentRequiredError", "DictationError", "EngineUnavailableError",
    "FocusLostError", "LLMAuthError", "LLMConfigError", "LLMError",
    "LLMNetworkError", "LLMQuotaError", "ModelNotDownloadedError",
)


class HierarchyTests(unittest.TestCase):
    def test_llm_family_is_under_llm_error_under_runtime_error(self):
        for cls in (LLMConfigError, LLMAuthError, LLMNetworkError, LLMQuotaError):
            with self.subTest(cls=cls.__name__):
                self.assertTrue(issubclass(cls, LLMError))
        self.assertTrue(issubclass(LLMError, RuntimeError))
        # derived config family
        self.assertTrue(issubclass(ModelNotDownloadedError, LLMConfigError))
        self.assertTrue(issubclass(EngineUnavailableError, LLMConfigError))
        self.assertTrue(issubclass(ConsentRequiredError, LLMConfigError))

    def test_dictation_error_is_a_sibling_never_a_parent_of_llm_error(self):
        # A3 R3: classic `except (LLMError, PasteError, ValueError)` boundaries
        # keep their EXACT meaning — FocusLostError must not be an LLMError.
        self.assertTrue(issubclass(DictationError, RuntimeError))
        self.assertFalse(issubclass(LLMError, DictationError))
        self.assertFalse(issubclass(DictationError, LLMError))
        self.assertTrue(issubclass(FocusLostError, DictationError))
        self.assertFalse(issubclass(FocusLostError, LLMError))

    def test_conservative_catch_boundaries(self):
        self.assertIsInstance(ConsentRequiredError("x"), LLMError)   # main's classic catch
        self.assertNotIsInstance(FocusLostError("x"), LLMError)
        self.assertIsInstance(FocusLostError("x"), DictationError)


class KeywordPayloadTests(unittest.TestCase):
    """Every typed constructor carries its machine-readable context (D6/D7/U2)."""

    def test_llm_quota_error_kwargs(self):
        error = LLMQuotaError("paused", engine_id="groq-whisper", reset_hint="14:05")
        self.assertEqual(error.engine_id, "groq-whisper")
        self.assertEqual(error.reset_hint, "14:05")
        self.assertEqual(str(error), "paused")
        # defaults are honest blanks, never fabricated
        plain = LLMQuotaError("paused")
        self.assertEqual((plain.engine_id, plain.reset_hint), ("", ""))

    def test_model_not_downloaded_error_kwargs(self):
        error = ModelNotDownloadedError("get it", model_id="faster-whisper-base")
        self.assertEqual(error.model_id, "faster-whisper-base")
        self.assertIsInstance(error, LLMConfigError)
        self.assertEqual(ModelNotDownloadedError("m").model_id, "")

    def test_engine_unavailable_error_kwargs(self):
        error = EngineUnavailableError("install extras", engine_id="local",
                                       reason="faster-whisper not installed")
        self.assertEqual(error.engine_id, "local")
        self.assertEqual(error.reason, "faster-whisper not installed")
        self.assertEqual(EngineUnavailableError("x").reason, "")

    def test_consent_required_error_kwargs(self):
        error = ConsentRequiredError("accept the notice", engine_id="gemini-live")
        self.assertEqual(error.engine_id, "gemini-live")
        self.assertEqual(ConsentRequiredError("x").engine_id, "")

    def test_focus_lost_error_is_plain_dictation_error(self):
        error = FocusLostError("window changed")
        self.assertEqual(str(error), "window changed")
        self.assertFalse(hasattr(error, "engine_id"))   # no LLM baggage


class LlmApiReExportIdentityTests(unittest.TestCase):
    def test_every_reexported_name_is_the_identical_class_object(self):
        for name in RE_EXPORTED:
            with self.subTest(name=name):
                self.assertIs(getattr(llm_api, name), getattr(llm_errors, name))

    def test_classic_llm_error_boundary_preserved_by_identity(self):
        # The pinned pre-existing behaviour: llm_api.raise sites still produce
        # instances catchable as llm_api.LLMError AND llm_errors.LLMError.
        self.assertIsInstance(llm_api.LLMQuotaError("x", engine_id="e"),
                              llm_errors.LLMError)
        with self.assertRaises(llm_api.LLMError):
            raise llm_errors.LLMNetworkError("boom")

    def test_llm_errors_is_a_pure_leaf(self):
        # D11/A3: zero imports, zero I/O, zero logging in the taxonomy module.
        with open(llm_errors.__file__, encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("import logging", "import urllib", "import os",
                          "open(", "print("):
            self.assertNotIn(forbidden, source,
                             f"llm_errors.py must stay pure (found {forbidden!r})")


if __name__ == "__main__":
    unittest.main()
