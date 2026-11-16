"""Pure session state machine for the three dictation modes (A6 s2.1, D10).

WHY: the orchestrator (``dictation_session``, later phase) and the UI must
agree on ONE legal-transition table — including "abort is legal from EVERY
state" — without importing threads, I/O, or the UI toolkit.  Everything here
is data + pure predicates: no timers, no side effects, and the clock never
appears (purity contract), so QA can drive the whole machine headlessly.

The degraded-notice path (D6) is intentionally NOT a state: a notice may be
raised from recording/draining/typing while processing continues (unit
disposition) or while aborting — modelling it as a state would invent
transitions the A6 diagram does not define.  ``DegradedNotice`` carries the
kind vocabulary instead; ``kind``/``detail`` hold approved user-safe copy
only, NEVER transcript text (D11).
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple


class SessionState(str, Enum):
    """The five session states (A6 s2.1).  ``str`` mixin so states compare and
    format as their plain values in logs/status copy."""

    IDLE = "idle"
    RECORDING = "recording"
    DRAINING = "draining"
    TYPING = "typing"
    ABORTING = "aborting"


# Exact trigger names from the A6 s2.1 transition table.
TRIGGERS = ("start", "stop_requested", "all_finals_emitted", "done", "abort", "abort_done")

# A6 s2.1 abort causes (a)-(d); a focus change is a DEGRADED notice, not an
# abort (it only suppresses injection while the session itself may continue).
ABORT_REASONS = ("user_cancel", "quit", "fatal_error", "backlog_overflow")

# A6 s2.3 degraded-notice cases (never silent, never failover — D6).
DEGRADED_KINDS = ("quota_exhausted", "unit_error", "focus_changed",
                  "queue_overflow", "extras_missing")

_TRANSITIONS = {
    (SessionState.IDLE, "start"): SessionState.RECORDING,
    (SessionState.RECORDING, "stop_requested"): SessionState.DRAINING,
    (SessionState.DRAINING, "all_finals_emitted"): SessionState.TYPING,
    (SessionState.TYPING, "done"): SessionState.IDLE,
    (SessionState.ABORTING, "abort_done"): SessionState.IDLE,
}
# ABORT is legal from EVERY state (A6 s2.1 "* -> ABORTING"); repeating it while
# already aborting is an idempotent no-op, never an error.
for _state in SessionState:
    _TRANSITIONS[(_state, "abort")] = SessionState.ABORTING


@dataclass(frozen=True)
class DegradedNotice:
    """Non-fatal notice for the status line (D6/D10): ``kind`` is one of
    ``DEGRADED_KINDS``; ``detail`` must be approved user-safe copy only —
    this class carries NO transcript (D11)."""

    kind: str
    engine_id: str = ""
    detail: str = ""


def _coerce(state) -> Optional[SessionState]:
    """Value or enum -> ``SessionState``; anything else -> None (never raises)."""
    if isinstance(state, SessionState):
        return state
    if isinstance(state, str):
        try:
            return SessionState(state)
        except ValueError:
            return None
    return None


def next_state(state, trigger) -> Optional[SessionState]:
    """Next state for ``(state, trigger)``, or ``None`` when illegal/unknown —
    a pure table lookup so callers can both validate and transition in one
    place.  Never raises on garbage input (N1)."""
    key = (_coerce(state), trigger if isinstance(trigger, str) else None)
    return _TRANSITIONS.get(key)


def can_transition(state, trigger) -> bool:
    """Predicate twin of ``next_state``: is this transition legal right now?"""
    return next_state(state, trigger) is not None


def is_processing(state) -> bool:
    """The existing UI guard semantics (A6 s2.1): ``state != IDLE``.  Unknown
    states count as NOT processing (fail-safe: never brick the start action)."""
    current = _coerce(state)
    return current is not None and current is not SessionState.IDLE


def legal_triggers(state) -> Tuple[str, ...]:
    """All triggers legal from ``state`` (deterministic order) — used by tests
    and debug surfaces; ``()`` for an unknown state.  Never raises (N1)."""
    current = _coerce(state)
    if current is None:
        return ()
    return tuple(trigger for trigger in TRIGGERS
                 if (current, trigger) in _TRANSITIONS)
