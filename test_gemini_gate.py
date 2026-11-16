"""Focused pins for the ONE Gemini consent rule — `gemini_gate` (F-2 Option A,
T-2026-11-16-dictation-modes; user-approved scope extension).

Owner: qa-test-assurance.  Pure policy leaf: no I/O, no Tk, no network.
Covers:
* ``routes_to_gemini`` endpoint rule — preset id, display label, Gemini host,
  cloned Custom Gemini URL -> True; OpenAI/Groq/localhost -> False;
* ``require_gemini_consent`` fail-closed typed error — raises
  ``ConsentRequiredError`` WITHOUT consent/paid flag (M20 literal ``True``,
  coercion-proof), passes with EITHER flag;
* ``require_gemini_route`` — raises only for Gemini-bound destinations;
* ``save_route`` — save-blocker decision tokens ``"live"`` / ``"provider"``
  / ``""`` (never raises).
"""

import unittest

import gemini_gate
from gemini_gate import (
    GEMINI_ENGINE_ID,
    GEMINI_PROVIDER_ID,
    require_gemini_consent,
    require_gemini_route,
    routes_to_gemini,
    save_route,
)
from llm_errors import ConsentRequiredError

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"


class GateContractTests(unittest.TestCase):
    def test_constants_and_pure_leaf_import(self):
        # the ONE rule's identifiers are pinned so callers cannot drift
        self.assertEqual(GEMINI_PROVIDER_ID, "google-gemini")
        self.assertEqual(GEMINI_ENGINE_ID, "gemini-live")
        # pure policy leaf: no transport/Tk/print surface, only typed helpers
        for name in ("gemini_consent_ok", "routes_to_gemini",
                     "require_gemini_consent", "require_gemini_route",
                     "save_route"):
            self.assertTrue(callable(getattr(gemini_gate, name)), name)
        with open(gemini_gate.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("import tkinter", source)
        self.assertNotIn("urllib", source)
        self.assertNotIn("requests", source)


class RoutesToGeminiTests(unittest.TestCase):
    def test_preset_id_and_display_label_route_to_gemini(self):
        self.assertTrue(routes_to_gemini("google-gemini"))
        self.assertTrue(routes_to_gemini("Google Gemini"))     # registry label
        self.assertTrue(routes_to_gemini(provider_id=GEMINI_PROVIDER_ID))

    def test_gemini_host_base_url_routes_to_gemini(self):
        # registry endpoint (no provider id needed) and a hand-edited Custom
        # URL cloned onto the SAME Gemini host are both Gemini-bound traffic
        self.assertTrue(routes_to_gemini(base_url=GEMINI_URL))
        self.assertTrue(routes_to_gemini("custom", GEMINI_URL))
        self.assertTrue(routes_to_gemini("custom",
                                         "https://generativelanguage.googleapis.com/"))
        # subdomain + port/scheme variants of the sanctioned host
        self.assertTrue(routes_to_gemini(base_url=
                                         "HTTPS://v1beta.generativelanguage.googleapis.com:443/x"))

    def test_non_gemini_routes_do_not_route_to_gemini(self):
        self.assertFalse(routes_to_gemini("openai"))
        self.assertFalse(routes_to_gemini("groq"))
        self.assertFalse(routes_to_gemini("ollama-local"))
        self.assertFalse(routes_to_gemini(base_url="http://localhost:11434/v1"))
        self.assertFalse(routes_to_gemini(base_url="https://api.openai.com/v1"))
        self.assertFalse(routes_to_gemini("custom", "https://api.groq.com/openai/v1"))

    def test_lookalike_and_malformed_hosts_never_route_to_gemini(self):
        # suffix/prefix spoofing of the Gemini host must NOT match
        self.assertFalse(routes_to_gemini(
            base_url="https://generativelanguage.googleapis.com.evil.example/v1"))
        self.assertFalse(routes_to_gemini(
            base_url="https://evil.example/generativelanguage.googleapis.com"))
        self.assertFalse(routes_to_gemini(
            base_url="https://notgenerativelanguage.googleapis.com/v1"))
        # non-string / empty inputs fail closed to False (never raise)
        for bad in (None, 42, "", "   ", b"https://generativelanguage.googleapis.com"):
            self.assertFalse(routes_to_gemini(base_url=bad), repr(bad))
            self.assertFalse(routes_to_gemini(provider_id=bad), repr(bad))


class RequireGeminiConsentTests(unittest.TestCase):
    def test_raises_without_consent_or_paid_flag(self):
        for source in ({"gemini_consent": False,
                        "gemini_paid_key_confirmed": False},
                       {},
                       {"gemini_consent": False},
                       {"gemini_paid_key_confirmed": False},
                       # M20 coercion-proof: only literal True counts
                       {"gemini_consent": "True", "gemini_paid_key_confirmed": 1},
                       None):
            with self.subTest(source=source):
                with self.assertRaises(ConsentRequiredError) as ctx:
                    require_gemini_consent(source)
                self.assertEqual(ctx.exception.engine_id, GEMINI_PROVIDER_ID)
                self.assertIn("data notice", str(ctx.exception))

    def test_passes_with_consent_flag(self):
        require_gemini_consent({"gemini_consent": True})          # never raises
        require_gemini_consent({"gemini_consent": True,
                                "gemini_paid_key_confirmed": False})

    def test_passes_with_paid_key_flag(self):
        require_gemini_consent({"gemini_paid_key_confirmed": True})

    def test_d10_snapshot_object_is_honoured(self):
        # SessionConfig snapshot exposes `gemini_enabled` instead of the mapping
        class Snapshot:
            def __init__(self, enabled):
                self.gemini_enabled = enabled

        with self.assertRaises(ConsentRequiredError):
            require_gemini_consent(Snapshot(False))
        require_gemini_consent(Snapshot(True))                     # never raises


class RequireGeminiRouteTests(unittest.TestCase):
    def test_gemini_bound_route_requires_consent(self):
        for route in ({"provider": "google-gemini"},
                      {"provider": "custom", "provider_base_url": GEMINI_URL}):
            with self.subTest(route=route):
                with self.assertRaises(ConsentRequiredError):
                    require_gemini_route(route)
        require_gemini_route({"provider": "google-gemini", "gemini_consent": True})
        require_gemini_route({"provider": "custom",
                              "provider_base_url": GEMINI_URL,
                              "gemini_paid_key_confirmed": True})

    def test_non_gemini_route_passes_without_consent(self):
        # OpenAI/localhost destinations are NOT gated by the Gemini rule
        require_gemini_route({"provider": "openai"})
        require_gemini_route({"provider": "ollama-local",
                              "provider_base_url": "http://localhost:11434/v1"})
        require_gemini_route({})                                   # fail-closed ''


class SaveRouteTests(unittest.TestCase):
    def test_blocks_gemini_live_engine_without_consent(self):
        self.assertEqual(save_route(GEMINI_ENGINE_ID, {}, {}), "live")
        self.assertEqual(save_route("gemini-live", {},
                                    {"gemini_consent": False,
                                     "gemini_paid_key_confirmed": False}), "live")

    def test_blocks_gemini_bound_provider_route_without_consent(self):
        self.assertEqual(save_route("local",
                                    {"provider": "google-gemini"}, {}), "provider")
        self.assertEqual(save_route("local",
                                    {"provider": "custom",
                                     "provider_base_url": GEMINI_URL}, {}), "provider")

    def test_permits_without_consent_flag_either(self):
        # consent OR paid-key (M20) unlocks every route — save may run
        self.assertEqual(save_route("gemini-live", {},
                                    {"gemini_consent": True}), "")
        self.assertEqual(save_route("gemini-live", {},
                                    {"gemini_paid_key_confirmed": True}), "")
        self.assertEqual(save_route("local", {"provider": "google-gemini"},
                                    {"gemini_consent": True}), "")

    def test_non_gemini_routes_and_live_engines_are_not_blocked(self):
        self.assertEqual(save_route("local", {"provider": "openai"},
                                    {"gemini_consent": False}), "")
        self.assertEqual(save_route("local",
                                    {"provider": "ollama-local",
                                     "provider_base_url": "http://localhost:11434/v1"},
                                    {}), "")
        self.assertEqual(save_route("local", {}, {"gemini_consent": False}), "")

    def test_save_route_never_raises_on_malformed_inputs(self):
        # decision token only: malformed sections degrade to "save may run"
        self.assertEqual(save_route(None, None, None), "")
        self.assertEqual(save_route(GEMINI_ENGINE_ID, None, None), "live")


if __name__ == "__main__":
    unittest.main()
