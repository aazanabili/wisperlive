"""Pure per-engine quota accounting for D6 — no silent failover (A3 R1 union).

WHY: Groq bills audio with a 10-second minimum plus daily audio/request
budgets, and Gemini limits RPM/RPD — those windows span app restarts, so the
counters are pure accounting here while ``to_state``/``from_state`` is the
port ``quota_store`` (I/O tier, later phase) persists to
``<config dir>/quota_state.json``.  The clock is ALWAYS injected
(``now_epoch``): this module never calls ``time.time()``, so expiry tests are
deterministic (purity contract).

D6 contract: ``check_admission`` runs BEFORE dispatch; refusal surfaces the
engine + reset time and NO code path may substitute another vendor.  The
per-engine "paused" state is DERIVED from counters (never persisted — a
vendor-side early reset must not strand the user).  Vendor headers, when
parseable, win via ``record_vendor_usage`` with ``max(local, vendor)``.

Window-kind rule (documented seam): ``minute``/``requests`` windows count
DISPATCHES (+1 per charge/admission) while ``day`` windows count the engine's
billed units — this keeps Gemini RPM admission correct even when a live-cloud
caller reserves minutes of audio (A6 s6.2), and keeps Groq's audio-seconds
window separate from its request-count window.
"""

import math
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import Mapping, Tuple

GROQ_BILL_FLOOR_S = 10                     # D5/V1 (verified): Groq's minimum billed seconds

WINDOW_MINUTE = "minute"                   # requests per rolling minute (RPM)
WINDOW_DAY = "day"                         # per-day budget in the engine's unit
WINDOW_REQUESTS = "requests"               # daily request count

SCHEMA_VERSION = 1                         # quota_state.json layout version


@dataclass(frozen=True)
class QuotaWindow:
    """One counter window (A3 s2.5): ``limit == -1`` = unknown/unlimited (honest
    until vendor headers teach us — V1b), ``reset_at == 0`` = unknown reset."""

    window_id: str
    limit: int
    used: int
    reset_at: int                           # epoch seconds


@dataclass(frozen=True)
class EngineQuota:
    """Quota profile for one ``engine_id`` (A3 s2.5): ``unit`` is the ledger's
    counting unit, ``bill_floor`` the per-request minimum (Groq audio = 10 s)."""

    engine_id: str
    unit: str                               # audio_seconds | requests | messages | tokens
    bill_floor: int
    windows: Tuple[QuotaWindow, ...]


@dataclass(frozen=True)
class QuotaLedger:
    """Immutable engine -> quota map (A3 s2.5); ``charge``/``mark_exhausted``
    return NEW ledgers, so UI reads are race-free against worker commits."""

    version: int
    engines: Mapping[str, EngineQuota]

    def __post_init__(self) -> None:
        # Same immutability mechanics as SessionConfig: copy + freeze once.
        object.__setattr__(self, "engines", MappingProxyType(dict(self.engines)))


@dataclass(frozen=True)
class Admission:
    """Pre-dispatch verdict: ``ok`` + optional ``"HH:MM"`` reset hint."""

    ok: bool
    reset_hint: str = ""


EMPTY_LEDGER = QuotaLedger(SCHEMA_VERSION, {})


def billable_units(duration_s, bill_floor) -> int:
    """Groq's floor rule (V1, D5): ``max(bill_floor, ceil(duration_s))`` —
    9.2 s bills 10, 10.2 s bills 11, (0, 0) bills 0.  Never raises: garbage
    or negative input bills 0 (N1 — negative billing would SHRINK counters)."""
    try:
        seconds = math.ceil(float(duration_s))
        floor = int(bill_floor)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, floor, seconds)


def _dispatch_window(window_id: str) -> bool:
    """Minute/request windows count dispatches; day windows count billed units."""
    return window_id in (WINDOW_MINUTE, WINDOW_REQUESTS)


def _expire(window: QuotaWindow, now_epoch: int) -> QuotaWindow:
    """Auto-reset a window whose KNOWN reset time has passed (A3 s2.5)."""
    if window.reset_at and now_epoch >= window.reset_at:
        return replace(window, used=0, reset_at=0)
    return window


def _hhmm(reset_at: int) -> str:
    """Local ``"HH:MM"`` reset hint for the countdown; unknown -> ``""`` (the UI
    renders an honest blank, never a fabricated time)."""
    if not isinstance(reset_at, int) or reset_at <= 0:
        return ""
    try:
        return datetime.fromtimestamp(reset_at).strftime("%H:%M")
    except (OSError, OverflowError, ValueError):
        return ""


def _engine(ledger, engine_id):
    """Engine row lookup that tolerates garbage inputs without raising (N1)."""
    if isinstance(ledger, QuotaLedger) and isinstance(engine_id, str):
        return ledger.engines.get(engine_id)
    return None


def _units(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def check_admission(ledger, engine_id, requested_units, now_epoch) -> Admission:
    """D6 pre-flight gate (A3 s2.5), run BEFORE any bytes leave.

    Unknown engine/limits never block locally (``limit = -1`` — the vendor
    still governs); a budgeted window blocks when the engine is exhausted or
    the request would exceed it, returning the first blocking window's
    ``"HH:MM"`` reset hint.  Never raises (N1).
    """
    engine = _engine(ledger, engine_id)
    if engine is None:
        return Admission(True)
    requested = _units(requested_units)
    for window in engine.windows:
        window = _expire(window, now_epoch)
        if window.limit < 0:
            continue                          # unknown/unlimited: never blocks
        used = window.used + (1 if _dispatch_window(window.window_id) else requested)
        if window.used >= window.limit or used > window.limit:
            return Admission(False, _hhmm(window.reset_at))
    return Admission(True)


def _apply_charge(window: QuotaWindow, amount: int, now_epoch: int) -> QuotaWindow:
    window = _expire(window, now_epoch)
    return replace(window, used=window.used + amount)


def charge(ledger, engine_id, units, now_epoch) -> QuotaLedger:
    """Commit ``units`` (A6 s6.2): on success AND on an unknown post-send
    outcome — billing reality is unknown, so it is conservatively counted as
    consumed, never pretended free.  Dispatch windows +1, day windows +units;
    unknown engine/garbage leaves the ledger unchanged.  Never raises (N1)."""
    engine = _engine(ledger, engine_id)
    if engine is None:
        return ledger
    amount = _units(units)
    windows = tuple(_apply_charge(window, 1 if _dispatch_window(window.window_id) else amount,
                                  now_epoch)
                    for window in engine.windows)
    engines = dict(ledger.engines)
    engines[engine_id] = replace(engine, windows=windows)
    return QuotaLedger(ledger.version, engines)


def record_vendor_usage(ledger, engine_id, window_id, used, reset_at) -> QuotaLedger:
    """Vendor headers are AUTHORITATIVE but never LOWER than our local count
    (``max(local, vendor)``, A3 s2.5).  Unknown engine/window => unchanged;
    a ``reset_at <= 0`` keeps the locally known reset.  Never raises (N1)."""
    engine = _engine(ledger, engine_id)
    if engine is None:
        return ledger
    authoritative = _units(used)
    vendor_reset = reset_at if isinstance(reset_at, int) and reset_at > 0 else 0
    updated = tuple(
        replace(window, used=max(window.used, authoritative),
                reset_at=vendor_reset or window.reset_at)
        if window.window_id == window_id else window
        for window in engine.windows
    )
    engines = dict(ledger.engines)
    engines[engine_id] = replace(engine, windows=updated)
    return QuotaLedger(ledger.version, engines)


def mark_exhausted(ledger, engine_id, until, now_epoch) -> QuotaLedger:
    """A6 s6.2: on HTTP 429/vendor limit — remaining = 0 until ``until`` for
    every budgeted window (unknown-limit windows stay untouched, honest).
    ``until <= 0`` leaves each reset time unchanged.  Never raises (N1)."""
    engine = _engine(ledger, engine_id)
    if engine is None:
        return ledger
    until_epoch = until if isinstance(until, int) and until > 0 else 0
    updated = []
    for window in engine.windows:
        expired = _expire(window, now_epoch)
        if expired.limit < 0:
            updated.append(expired)
            continue
        updated.append(replace(expired, used=max(expired.used, expired.limit),
                               reset_at=until_epoch or expired.reset_at))
    engines = dict(ledger.engines)
    engines[engine_id] = replace(engine, windows=tuple(updated))
    return QuotaLedger(ledger.version, engines)


def remaining(ledger, engine_id, now_epoch) -> int:
    """Visible remaining budget (D6 countdown): minimum of ``limit - used``
    across budgeted windows after expiry reset; ``-1`` = unknown/unlimited
    (UI renders an honest blank, never a fabricated percentage)."""
    engine = _engine(ledger, engine_id)
    if engine is None:
        return -1
    budgets = []
    for window in engine.windows:
        window = _expire(window, now_epoch)
        if window.limit >= 0:
            budgets.append(window.limit - window.used)
    return max(0, min(budgets)) if budgets else -1


def engine_paused(ledger, engine_id, now_epoch) -> bool:
    """DERIVED pause state — never persisted (A3 s2.5): a budgeted window sits
    at/over its limit and its reset is unknown (0) or still in the future.
    An unknown reset pauses (consistent with ``check_admission``, which blocks
    too) until ``record_vendor_usage``/``mark_exhausted`` teaches the real one."""
    engine = _engine(ledger, engine_id)
    if engine is None:
        return False
    for window in engine.windows:
        window = _expire(window, now_epoch)
        if window.limit >= 0 and window.used >= window.limit:
            if window.reset_at == 0 or window.reset_at > now_epoch:
                return True
    return False


def default_engine_quota(engine_id, limit_overrides=None) -> EngineQuota:
    """Construction point for dispatch wiring (engine vocabulary per A3 R1):

    * ``groq-whisper`` — audio_seconds with the verified 10 s floor, a daily
      audio window plus a daily request-count window;
    * ``gemini-live``/``openai-realtime`` — RPM + daily windows (audio units,
      A6 s6.2 live per-minute accounting);
    * ``chat:<provider>`` / ``cleanup:<provider>`` — RPM + daily request
      windows (RPD);
    * local/unknown ids — a single unknown observability window.

    Limits are ``-1`` (unknown until the first vendor 429 — V1b: exact vendor
    numbers are volatile) unless ``quota_limits`` overrides with positive ints
    under ``"<engine_id>.<window_id>"`` keys (M23).
    """
    engine_id = engine_id if isinstance(engine_id, str) else ""
    if engine_id == "groq-whisper":
        unit, floor, window_ids = "audio_seconds", GROQ_BILL_FLOOR_S, (WINDOW_DAY, WINDOW_REQUESTS)
    elif engine_id in ("gemini-live", "openai-realtime"):
        unit, floor, window_ids = "audio_seconds", 0, (WINDOW_MINUTE, WINDOW_DAY)
    elif engine_id.startswith(("chat:", "cleanup:")):
        unit, floor, window_ids = "requests", 0, (WINDOW_MINUTE, WINDOW_DAY)
    else:                                    # local, preset:*, unknown
        unit, floor, window_ids = "requests", 0, (WINDOW_DAY,)
    overrides = limit_overrides if isinstance(limit_overrides, Mapping) else {}
    windows = tuple(QuotaWindow(window_id, _limit_override(overrides, engine_id, window_id), 0, 0)
                    for window_id in window_ids)
    return EngineQuota(engine_id, unit, floor, windows)


def _limit_override(overrides: Mapping, engine_id: str, window_id: str) -> int:
    value = overrides.get(f"{engine_id}.{window_id}")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return -1                                 # unknown until vendor headers say otherwise


def to_state(ledger) -> dict:
    """Serialization port for the ``quota_store`` adapter (A3 s2.5): counters
    + timestamps ONLY — no transcript, no secrets (D11-clean). Never raises."""
    if not isinstance(ledger, QuotaLedger):
        return {"version": SCHEMA_VERSION, "engines": {}}
    engines = {}
    for engine_id, engine in ledger.engines.items():
        engines[engine_id] = {
            "engine_id": engine.engine_id,
            "unit": engine.unit,
            "bill_floor": engine.bill_floor,
            "windows": [{"window_id": window.window_id, "limit": window.limit,
                         "used": window.used, "reset_at": window.reset_at}
                        for window in engine.windows],
        }
    return {"version": ledger.version, "engines": engines}


def from_state(data) -> QuotaLedger:
    """NEVER raises: garbage or partially corrupt persisted state degrades to
    an empty ledger (N1), so a bad ``quota_state.json`` means 'unknown budget'
    — never a startup crash and never fabricated headroom."""
    if not isinstance(data, dict):
        return EMPTY_LEDGER
    version = data.get("version")
    if not isinstance(version, int) or version < 1:
        version = SCHEMA_VERSION
    engines = {}
    raw_engines = data.get("engines")
    if isinstance(raw_engines, dict):
        for engine_id, raw in raw_engines.items():
            engine = _engine_from_state(engine_id, raw)
            if engine is not None:
                engines[engine_id] = engine
    return QuotaLedger(version, engines)


def _engine_from_state(engine_id, raw):
    """One persisted engine row -> ``EngineQuota``; any malformed row is
    dropped (N1) instead of corrupting the whole ledger."""
    if not isinstance(engine_id, str) or not isinstance(raw, dict):
        return None
    unit = raw.get("unit")
    bill_floor = raw.get("bill_floor")
    windows = raw.get("windows")
    if not isinstance(unit, str) or not isinstance(bill_floor, int) or not isinstance(windows, list):
        return None
    parsed = []
    for window in windows:
        if not isinstance(window, dict):
            return None
        window_id = window.get("window_id")
        limit = window.get("limit")
        used = window.get("used")
        reset_at = window.get("reset_at")
        if not isinstance(window_id, str):
            return None
        if not all(isinstance(value, int) for value in (limit, used, reset_at)):
            return None
        parsed.append(QuotaWindow(window_id, limit, max(0, used), max(0, reset_at)))
    return EngineQuota(engine_id, unit, max(0, bill_floor), tuple(parsed))
