"""Session state-machine tests (plan 2.3 matrix; A6 s2.1 full table).

Owner: qa-test-assurance.  Drives the COMPLETE transition table headlessly:
every (state, trigger) pair, abort-from-everywhere (incl. abort while already
aborting as an idempotent no-op), the illegal-trigger matrix, ``is_processing``
guard semantics, and the vocab pins (ABORT_REASONS / DEGRADED_KINDS /
DegradedNotice never carrying transcript).
"""

import dataclasses
import unittest

import session_state as machine
from session_state import DegradedNotice, SessionState

STATES = (SessionState.IDLE, SessionState.RECORDING, SessionState.DRAINING,
          SessionState.TYPING, SessionState.ABORTING)

# The A6 s2.1 table as documented, re-derived INDEPENDENTLY of _TRANSITIONS.
HAPPY_PATH = {
    (SessionState.IDLE, "start"): SessionState.RECORDING,
    (SessionState.RECORDING, "stop_requested"): SessionState.DRAINING,
    (SessionState.DRAINING, "all_finals_emitted"): SessionState.TYPING,
    (SessionState.TYPING, "done"): SessionState.IDLE,
    (SessionState.ABORTING, "abort_done"): SessionState.IDLE,
}


class FullTransitionTableTests(unittest.TestCase):
    def test_every_state_trigger_pair_matches_the_documented_table(self):
        for state in STATES:
            for trigger in machine.TRIGGERS:
                expected = HAPPY_PATH.get((state, trigger))
                if expected is None and trigger == "abort":
                    expected = SessionState.ABORTING
                with self.subTest(state=state.value, trigger=trigger):
                    result = machine.next_state(state, trigger)
                    self.assertEqual(result, expected)
                    self.assertEqual(machine.can_transition(state, trigger),
                                     expected is not None)

    def test_abort_is_legal_from_every_state_including_abort_itself(self):
        for state in STATES:
            with self.subTest(state=state.value):
                self.assertTrue(machine.can_transition(state, "abort"))
                self.assertIs(machine.next_state(state, "abort"),
                              SessionState.ABORTING)
        # idempotent: abort while aborting is a no-op transition, never an error
        self.assertIs(machine.next_state(SessionState.ABORTING, "abort"),
                      SessionState.ABORTING)

    def test_illegal_transitions_return_none_not_raise(self):
        illegal = [
            (SessionState.IDLE, "stop_requested"),
            (SessionState.IDLE, "done"),
            (SessionState.IDLE, "abort_done"),
            (SessionState.IDLE, "all_finals_emitted"),
            (SessionState.RECORDING, "start"),
            (SessionState.RECORDING, "done"),
            (SessionState.RECORDING, "abort_done"),
            (SessionState.DRAINING, "start"),
            (SessionState.DRAINING, "stop_requested"),
            (SessionState.TYPING, "start"),
            (SessionState.TYPING, "all_finals_emitted"),
            (SessionState.ABORTING, "start"),
            (SessionState.ABORTING, "stop_requested"),
            (SessionState.ABORTING, "done"),
        ]
        for state, trigger in illegal:
            with self.subTest(state=state.value, trigger=trigger):
                self.assertIsNone(machine.next_state(state, trigger))
                self.assertFalse(machine.can_transition(state, trigger))

    def test_garbage_inputs_never_raise(self):
        for state in (None, 42, "bogus", object()):
            for trigger in (None, 42, object(), "start"):
                with self.subTest(state=state, trigger=trigger):
                    self.assertIsNone(machine.next_state(state, trigger))
                    self.assertFalse(machine.can_transition(state, trigger))
                    self.assertEqual(machine.legal_triggers(state), ())

    def test_string_states_coerce_like_the_enum(self):
        self.assertIs(machine.next_state("idle", "start"), SessionState.RECORDING)
        self.assertIs(machine.next_state("aborting", "abort_done"),
                      SessionState.IDLE)

    def test_legal_triggers_per_state_are_exact_and_ordered(self):
        expected = {
            SessionState.IDLE: ("start", "abort"),
            SessionState.RECORDING: ("stop_requested", "abort"),
            SessionState.DRAINING: ("all_finals_emitted", "abort"),
            SessionState.TYPING: ("done", "abort"),
            SessionState.ABORTING: ("abort", "abort_done"),
        }
        for state, triggers in expected.items():
            with self.subTest(state=state.value):
                self.assertEqual(machine.legal_triggers(state), triggers)

    def test_trigger_vocabulary_is_pinned(self):
        self.assertEqual(machine.TRIGGERS,
                         ("start", "stop_requested", "all_finals_emitted",
                          "done", "abort", "abort_done"))


class GuardSemanticsTests(unittest.TestCase):
    def test_is_processing_is_idle_equals_not_processing(self):
        self.assertFalse(machine.is_processing(SessionState.IDLE))
        for state in STATES[1:]:
            self.assertTrue(machine.is_processing(state.value))
        # unknown states count as NOT processing (fail-safe: never brick start)
        self.assertFalse(machine.is_processing(None))
        self.assertFalse(machine.is_processing("bogus"))
        self.assertFalse(machine.is_processing(42))

    def test_states_compare_as_plain_strings(self):
        self.assertEqual(SessionState.RECORDING.value, "recording")
        self.assertEqual(SessionState.IDLE, "idle")          # str mixin equality
        self.assertTrue(SessionState.IDLE == "idle")
        self.assertIn(SessionState.ABORTING, tuple(STATES))
        # production status copy formats via the explicit .value (session_state
        # itself never relies on str() formatting of the enum)
        self.assertEqual(f"{SessionState.DRAINING.value}", "draining")


class DegradedNoticeTests(unittest.TestCase):
    def test_notice_vocabulary_is_pinned(self):
        self.assertEqual(machine.DEGRADED_KINDS,
                         ("quota_exhausted", "unit_error", "focus_changed",
                          "queue_overflow", "extras_missing"))
        self.assertEqual(machine.ABORT_REASONS,
                         ("user_cancel", "quit", "fatal_error", "backlog_overflow"))

    def test_notice_is_frozen_pure_data_without_transcript_field(self):
        notice = DegradedNotice(kind="quota_exhausted", engine_id="groq-whisper",
                                detail="Paused until 14:05.")
        self.assertEqual(notice.kind, "quota_exhausted")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            notice.kind = "unit_error"
        fields = {field.name for field in dataclasses.fields(DegradedNotice)}
        self.assertEqual(fields, {"kind", "engine_id", "detail"})
        self.assertNotIn("text", fields)                     # D11: no transcript slot

    def test_degraded_notices_are_never_states(self):
        # D6: a notice may fire while recording/draining/typing AND while
        # aborting — modelling it as a state would invent transitions.  Pin
        # that the enum holds EXACTLY five states.
        self.assertEqual(len(SessionState), 5)
        self.assertEqual({state.value for state in SessionState},
                         {"idle", "recording", "draining", "typing", "aborting"})


if __name__ == "__main__":
    unittest.main()
