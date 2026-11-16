"""Quota ledger tests (plan 2.3 matrix; D6 gate evidence for G4).

Owner: qa-test-assurance.  Covers:
* Groq's verified 10-second billing floor math (``billable_units``),
* window-kind semantics (minute/requests count DISPATCHES, day counts units),
* pre-dispatch admission + atomic reserve (check then charge),
* exhaustion + ``"HH:MM"`` reset hint from an INJECTED clock,
* persistence round-trip (``to_state``/``from_state``) incl. corrupt input,
* the purity contract: this module never reads a clock itself.

Everything is deterministic: every "now" is an argument (no time.time in the
module under test).
"""

import ast
import unittest

import quota_ledger as ql


def _ledger(engine_id="groq-whisper", limits=None, overrides=None):
    quota = ql.default_engine_quota(engine_id, overrides)
    return ql.QuotaLedger(ql.SCHEMA_VERSION, {engine_id: quota})


class BillableUnitsFloorTests(unittest.TestCase):
    """V1 (D5): Groq bills max(10 s, ceil(duration)) — never below the floor."""

    def test_ten_second_floor_math(self):
        cases = [
            (0.0, 10, 10),       # floor applies even at zero duration
            (0.1, 10, 10),       # any positive tail hits the floor
            (9.2, 10, 10),
            (9.999, 10, 10),
            (10.0, 10, 10),
            (10.2, 10, 11),      # ceil above the floor
            (11.0, 10, 11),
            (59.5, 10, 60),
            (0.0, 0, 0),         # documented (0, 0) case bills nothing
        ]
        for duration, floor, expected in cases:
            with self.subTest(duration=duration):
                self.assertEqual(ql.billable_units(duration, floor), expected)

    def test_garbage_never_bills_negative(self):
        for garbage in (None, "x", object(), float("nan"), float("inf")):
            with self.subTest(value=garbage):
                result = ql.billable_units(garbage, 10)
                self.assertIsInstance(result, int)
                self.assertGreaterEqual(result, 0)
        self.assertEqual(ql.billable_units(100, None), 0)   # unusable floor -> 0 (N1)
        self.assertEqual(ql.billable_units(-3, 0), 0)       # negative billing never shrinks
        self.assertEqual(ql.billable_units(-3, 5), 5)       # floor still enforced

    def test_groq_profile_pins_the_floor(self):
        quota = ql.default_engine_quota("groq-whisper")
        self.assertEqual(quota.unit, "audio_seconds")
        self.assertEqual(quota.bill_floor, ql.GROQ_BILL_FLOOR_S)
        self.assertEqual(ql.GROQ_BILL_FLOOR_S, 10)


class WindowKindAndProfileTests(unittest.TestCase):
    def test_engine_profiles_match_the_a3_vocabulary(self):
        groq = ql.default_engine_quota("groq-whisper")
        self.assertEqual([w.window_id for w in groq.windows],
                         [ql.WINDOW_DAY, ql.WINDOW_REQUESTS])
        for engine in ("gemini-live", "openai-realtime"):
            quota = ql.default_engine_quota(engine)
            self.assertEqual(quota.unit, "audio_seconds")
            self.assertEqual([w.window_id for w in quota.windows],
                             [ql.WINDOW_MINUTE, ql.WINDOW_DAY])
        for engine in ("chat:openai", "cleanup:groq"):
            quota = ql.default_engine_quota(engine)
            self.assertEqual(quota.unit, "requests")
            self.assertEqual([w.window_id for w in quota.windows],
                             [ql.WINDOW_MINUTE, ql.WINDOW_DAY])
        local = ql.default_engine_quota("local")
        self.assertEqual([w.window_id for w in local.windows], [ql.WINDOW_DAY])
        self.assertEqual(local.unit, "requests")

    def test_unknown_limits_are_honest_minus_one(self):
        quota = ql.default_engine_quota("groq-whisper")
        self.assertTrue(all(w.limit == -1 for w in quota.windows))

    def test_quota_limits_overrides_apply_only_to_valid_positive_ints(self):
        quota = ql.default_engine_quota("groq-whisper", {
            "groq-whisper.day": 100,
            "groq-whisper.requests": True,        # bool is not a limit
            "groq-whisper.other": -5,             # not positive
            "other.day": 7,                       # other engine untouched
        })
        by_id = {w.window_id: w.limit for w in quota.windows}
        self.assertEqual(by_id, {ql.WINDOW_DAY: 100, ql.WINDOW_REQUESTS: -1})
        # garbage overrides mapping degrades to unknown limits (N1)
        self.assertTrue(all(w.limit == -1 for w in
                            ql.default_engine_quota("groq-whisper", "junk").windows))


class AdmissionTests(unittest.TestCase):
    def test_unknown_engine_or_unlimited_never_blocks_locally(self):
        self.assertTrue(ql.check_admission(ql.EMPTY_LEDGER, "anything", 10, 0).ok)
        self.assertTrue(ql.check_admission(_ledger(), "local", 999, 0).ok)
        self.assertTrue(ql.check_admission(None, None, None, None).ok)

    def test_dispatch_windows_count_one_per_request_not_units(self):
        ledger = _ledger("chat:openai", overrides={"chat:openai.minute": 2})
        now = 1_000
        self.assertTrue(ql.check_admission(ledger, "chat:openai", 999, now).ok)
        ledger = ql.charge(ledger, "chat:openai", 999, now)   # minute window +1 only
        minute = next(w for w in ledger.engines["chat:openai"].windows
                      if w.window_id == ql.WINDOW_MINUTE)
        day = next(w for w in ledger.engines["chat:openai"].windows
                   if w.window_id == ql.WINDOW_DAY)
        self.assertEqual(minute.used, 1)          # dispatch counted once...
        self.assertEqual(day.used, 999)           # ...day window counted the units

    def test_budgeted_window_blocks_at_and_over_limit(self):
        ledger = _ledger("chat:openai", overrides={"chat:openai.minute": 1})
        now = 5_000
        self.assertTrue(ql.check_admission(ledger, "chat:openai", 1, now).ok)
        ledger = ql.charge(ledger, "chat:openai", 1, now)
        refused = ql.check_admission(ledger, "chat:openai", 1, now)
        self.assertFalse(refused.ok)
        # day window: request WOULD exceed
        ledger = _ledger("chat:openai", overrides={"chat:openai.day": 10})
        self.assertTrue(ql.check_admission(ledger, "chat:openai", 10, now).ok)
        self.assertFalse(ql.check_admission(ledger, "chat:openai", 11, now).ok)
        ledger = ql.charge(ledger, "chat:openai", 10, now)
        self.assertFalse(ql.check_admission(ledger, "chat:openai", 1, now).ok)  # used>=limit

    def test_reserve_is_atomic_against_racing_admissions(self):
        # The session's TOCTOU rule (A6 s6.2): admission+charge happen under one
        # lock; two sequential admissions under budget-of-one must NOT both pass
        # once the first one charges.
        ledger = _ledger("groq-whisper", overrides={"groq-whisper.requests": 1})
        first = ql.check_admission(ledger, "groq-whisper", 10, 1_000)
        self.assertTrue(first.ok)
        ledger = ql.charge(ledger, "groq-whisper", 10, 1_000)      # reserve
        second = ql.check_admission(ledger, "groq-whisper", 10, 1_000)
        self.assertFalse(second.ok)

    def test_expired_window_resets_from_the_injected_clock(self):
        ledger = _ledger("chat:openai", overrides={"chat:openai.minute": 1})
        ledger = ql.charge(ledger, "chat:openai", 1, 1_000)
        self.assertFalse(ql.check_admission(ledger, "chat:openai", 1, 1_500).ok)
        # reset_at unknown (0): stays blocked (honest, vendor must teach us)
        self.assertFalse(ql.check_admission(ledger, "chat:openai", 1, 9_999_999).ok)

    def test_known_reset_time_expires_the_window(self):
        quota = ql.EngineQuota("chat:openai", "requests", 0, (
            ql.QuotaWindow(ql.WINDOW_MINUTE, 1, 1, 2_000),))
        ledger = ql.QuotaLedger(1, {"chat:openai": quota})
        self.assertFalse(ql.check_admission(ledger, "chat:openai", 1, 1_999).ok)
        self.assertTrue(ql.check_admission(ledger, "chat:openai", 1, 2_000).ok)  # >= reset


class ExhaustionAndHintTests(unittest.TestCase):
    def test_mark_exhausted_zeroes_remaining_until_the_reset(self):
        ledger = _ledger("groq-whisper", overrides={"groq-whisper.day": 30})
        ledger = ql.mark_exhausted(ledger, "groq-whisper", until=7_000, now_epoch=1_000)
        self.assertEqual(ql.remaining(ledger, "groq-whisper", 1_000), 0)
        self.assertTrue(ql.engine_paused(ledger, "groq-whisper", 1_000))
        refused = ql.check_admission(ledger, "groq-whisper", 1, 1_000)
        self.assertFalse(refused.ok)
        from datetime import datetime                       # local tz of THIS host
        self.assertEqual(refused.reset_hint,
                         datetime.fromtimestamp(7_000).strftime("%H:%M"))
        # after the KNOWN reset the window auto-expires (injected clock)
        self.assertFalse(ql.engine_paused(ledger, "groq-whisper", 7_000))
        self.assertTrue(ql.check_admission(ledger, "groq-whisper", 1, 7_000).ok)

    def test_unknown_reset_hint_is_honest_blank(self):
        ledger = ql.mark_exhausted(
            _ledger("groq-whisper", overrides={"groq-whisper.day": 10}),
            "groq-whisper", until=0, now_epoch=1_000)
        refused = ql.check_admission(ledger, "groq-whisper", 1, 1_000)
        self.assertFalse(refused.ok)
        self.assertEqual(refused.reset_hint, "")                 # never a fabricated time
        # An engine with ONLY unknown windows stays unlimited even after a 429:
        # vendor truth is unknown, so we never invent a local budget (honest).
        unknown_only = ql.mark_exhausted(_ledger("local"), "local",
                                         until=999, now_epoch=1)
        self.assertTrue(ql.check_admission(unknown_only, "local", 1, 1).ok)

    def test_remaining_and_paused_for_unknown_engine(self):
        self.assertEqual(ql.remaining(ql.EMPTY_LEDGER, "nope", 0), -1)
        self.assertFalse(ql.engine_paused(ql.EMPTY_LEDGER, "nope", 0))
        # unknown budget renders -1 (UI shows an honest blank, never a percent)
        self.assertEqual(ql.remaining(_ledger("local"), "local", 0), -1)

    def test_record_vendor_usage_never_lowers_local_counts(self):
        ledger = _ledger("groq-whisper", overrides={"groq-whisper.day": 100})
        ledger = ql.charge(ledger, "groq-whisper", 40, 1_000)
        lower = ql.record_vendor_usage(ledger, "groq-whisper", ql.WINDOW_DAY, 5, 0)
        self.assertEqual(next(w for w in lower.engines["groq-whisper"].windows
                              if w.window_id == ql.WINDOW_DAY).used, 40)
        higher = ql.record_vendor_usage(ledger, "groq-whisper", ql.WINDOW_DAY, 80, 6_000)
        day = next(w for w in higher.engines["groq-whisper"].windows
                   if w.window_id == ql.WINDOW_DAY)
        self.assertEqual(day.used, 80)
        self.assertEqual(day.reset_at, 6_000)
        # unknown engine/window => unchanged (N1)
        self.assertIs(ql.record_vendor_usage(ledger, "nope", ql.WINDOW_DAY, 1, 1), ledger)


class PersistenceRoundTripTests(unittest.TestCase):
    def test_round_trip_preserves_counters_and_pins_schema(self):
        ledger = _ledger("groq-whisper", overrides={"groq-whisper.day": 500})
        ledger = ql.charge(ledger, "groq-whisper", 120, 1_000)
        state = ql.to_state(ledger)
        self.assertEqual(state["version"], ql.SCHEMA_VERSION)
        restored = ql.from_state(state)
        self.assertEqual(restored, ledger)
        # serialization carries counters ONLY — D11-clean (no transcript/secrets)
        flat = repr(state).lower()
        for forbidden in ("text", "transcript", "api_key", "secret", "key"):
            self.assertNotIn(f"{forbidden}=", flat)

    def test_corrupt_or_garbage_state_degrades_to_empty_never_crashes(self):
        for garbage in (None, "junk", 42, [], {"version": "x"},
                        {"version": 1, "engines": "nope"},
                        {"version": 1, "engines": {"e": {"windows": "bad"}}},
                        {"version": 1, "engines": {"e": {"unit": "requests",
                                                         "bill_floor": 1,
                                                         "windows": [{"window_id": "day"}]}}}):
            with self.subTest(data=garbage):
                restored = ql.from_state(garbage)
                self.assertIsInstance(restored, ql.QuotaLedger)
        self.assertEqual(ql.from_state({}), ql.EMPTY_LEDGER)
        self.assertEqual(ql.to_state("junk"),
                         {"version": ql.SCHEMA_VERSION, "engines": {}})

    def test_one_malformed_engine_row_is_dropped_not_fatal(self):
        state = {"version": 1, "engines": {
            "chat:openai": ql.to_state(ql.QuotaLedger(
                1, {"chat:openai": ql.default_engine_quota("chat:openai")}
            ))["engines"]["chat:openai"],
            "broken": 5,                       # corrupt row (e.g. truncated file)
            "worse": {"unit": "requests"},     # missing required fields
        }}
        restored = ql.from_state(state)
        self.assertIn("chat:openai", restored.engines)
        self.assertNotIn("broken", restored.engines)
        self.assertNotIn("worse", restored.engines)

    def test_ledger_engines_mapping_is_frozen(self):
        ledger = _ledger()
        with self.assertRaises(TypeError):
            ledger.engines["x"] = ql.default_engine_quota("x")


class PurityContractTests(unittest.TestCase):
    def test_module_never_reads_a_clock_itself(self):
        # Every expiry test is deterministic because "now" is always an argument:
        # the module imports no `time` at all (docstrings excluded — AST only).
        with open(ql.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported = set()
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
            elif isinstance(node, ast.Name):
                names.add(node.id)
        self.assertNotIn("time", imported, "quota_ledger must never import time")
        self.assertNotIn("time", names)
        self.assertNotIn("monotonic", names)


if __name__ == "__main__":
    unittest.main()
