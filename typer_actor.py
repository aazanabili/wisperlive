"""Single-thread ordered emission typer (D4/U6; A6 s4.3 guarded emit).

WHY: engines may finish out of order, but text must be injected strictly by
``emission_seq`` (duplicates dropped idempotently, gaps NEVER typed out of
order), and injection must be GATED: aborted/quitting sessions inject nothing
(M2 — checked before the FIRST keystroke of every emission), and a focus change
pauses typing with the backlog RETAINED for resume/copy (D4).

Platform seams (lazy, mockable — no crash when a seam is missing):
* Windows: ``SendInput`` + ``KEYEVENTF_UNICODE`` — scan codes CANNOT emit
  Arabic, which is the whole reason this actor exists (D4).
* Linux/X11: per-phrase clipboard paste through the ONE session clipboard
  (``auto_typer.SessionClipboard``); per-phrase restore TIMERS are banned (D4).
"""

import enum
import logging
import os
import queue
import threading
import time

from auto_typer import (PasteError, SessionClipboard, send_unicode_text,
                        tap_shift_left)
from focus_guard import FocusGuard
from llm_errors import FocusLostError
from polish_engine import SpanRegistry

logger = logging.getLogger(__name__)

STOP = object()                    # queue sentinel (module-private by convention)
MAX_GAP_SUBMISSIONS = 32           # buffered out-of-order span before a loss is declared


class EmissionResult(str, enum.Enum):
    """A6 s4.3 outcome vocabulary for one guarded emission attempt."""

    TYPED = "typed"
    SUPPRESSED = "suppressed"      # aborted/quitting: zero injection (M2)
    FOCUS_CHANGED = "focus_changed"  # paused: emission retained, never typed blind


def default_sender(clipboard: SessionClipboard):
    """Platform text sender: Windows -> SendInput UNICODE; Linux/X11 -> the
    ONE session clipboard's per-phrase paste (D4; no per-phrase restore)."""
    if os.name == "nt":
        return send_unicode_text
    return clipboard.put_and_paste


class TyperActor:
    """Session-scoped actor: FIFO queue + strict ``emission_seq`` pipeline on
    ONE thread, guarded injection (abort -> focus -> inject -> span record),
    pause/resume with retained backlog, and a ONE-shot clipboard lifecycle.
    """

    def __init__(self, *, focus_guard, clipboard, registry, sender,
                 app=None, insertion_mode="insert", clock=time.time,
                 max_gap=MAX_GAP_SUBMISSIONS):
        self.focus_guard = focus_guard
        self.clipboard = clipboard
        self.registry = registry
        self._sender = sender
        self._app = app
        self._insertion_mode = insertion_mode
        self._clock = clock
        self._max_gap = max(1, int(max_gap))
        self._queue: queue.Queue = queue.Queue()
        self._buffer: dict = {}                     # seq -> emission (gap hold)
        self._retained: list = []                   # focus-retained backlog (resume/copy)
        self._expected = 0                          # contract: session seqs start at 0
        self._gap_submissions = 0
        self._paused = False
        self._aborted = False
        self._stopped = False
        self._thread = None
        self._typed_count = 0
        self._dropped_duplicates = 0
        self._dropped_lost = 0

    # ---------------- lifecycle --------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self.focus_guard.start_key_hook()
        self.clipboard.open()                       # D4: save ONCE at session start
        self._thread = threading.Thread(target=self._run, name="dictation-typer", daemon=True)
        self._thread.start()

    def submit(self, emission) -> bool:
        """Enqueue from the session emitter thread (any arrival order — the
        pipeline re-orders).  False once stopped (never typed after stop)."""
        if self._stopped:
            return False
        self._queue.put(emission)
        return True

    def stop(self) -> None:
        """ONE session teardown: join the thread (bounded), close the session
        clipboard EXACTLY ONCE (D4), preserve any retained backlog by copying
        it (no keystrokes), and uninstall the key hook.  Idempotent."""
        if self._stopped:
            return
        self._stopped = True
        self._queue.put(STOP)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        self._thread = None
        exiting = bool(self._app is not None and getattr(self._app, "is_exiting", False))
        retained = self.retained_texts()
        if not exiting:
            self.clipboard.close()                  # exactly one restore per session (D4)
            if retained:
                try:
                    self.clipboard.copy_only(" ".join(retained))
                    logger.warning("Preserved %d undelivered phrase(s) on the clipboard",
                                   len(retained))
                except PasteError:
                    logger.warning("Could not preserve undelivered phrases", exc_info=True)
        self.focus_guard.stop_key_hook()

    # ---------------- observability ----------------------------------------
    @property
    def typed_count(self) -> int:
        return self._typed_count

    @property
    def typed_any(self) -> bool:
        """True once ANY phrase was injected (the session skips its whole-text
        fallback paste to avoid double insertion)."""
        return self._typed_count > 0

    @property
    def retained_count(self) -> int:
        """Backlog held for resume/copy (focus pause + queue + gap buffer)."""
        return len(self._retained) + self._queue.qsize() + len(self._buffer)

    def retained_texts(self) -> list:
        """Deterministic oldest-first view of the undelivered backlog."""
        items = list(self._retained) + list(self._buffer.values())
        items.sort(key=lambda emission: getattr(emission, "emission_seq", -1))
        return [emission.text for emission in items if getattr(emission, "text", "")]

    # ---------------- guarded injection (A6 s4.3) ---------------------------
    def guarded_emit(self, emission) -> EmissionResult:
        """Abort gate -> focus check -> platform inject -> span record (D3/D4).

        Raises ``FocusLostError`` AFTER retaining the emission: the caller
        (actor thread or a test) sees the typed error; nothing was injected.
        """
        if self._aborted or (self._app is not None
                             and getattr(self._app, "is_exiting", False)):
            return EmissionResult.SUPPRESSED        # M2: zero injection when quitting
        try:
            self.focus_guard.check()                # raises FocusLostError on change (D4)
        except FocusLostError:
            self._retained.append(emission)         # backlog RETAINED for resume/copy
            self._paused = True
            self._notify_status("Typing paused: the target window changed. "
                                "Re-focus it to resume; your text is kept.")
            raise
        # Re-check AFTER the wait (quit may have raced a pause — M2).
        if self._aborted or (self._app is not None
                             and getattr(self._app, "is_exiting", False)):
            return EmissionResult.SUPPRESSED
        try:
            self._sender(emission.text)
        except PasteError as error:
            self._notify_status(str(error))
            logger.warning("Injection failed for emission %d", emission.emission_seq)
            return EmissionResult.SUPPRESSED
        # D3: spans are created ONLY after a successful injection.
        self.registry.record_typed(
            emission.emission_seq, emission.text, self._insertion_mode,
            int(self._clock()))
        self._typed_count += 1
        return EmissionResult.TYPED

    def type_text(self, text: str) -> None:
        """Direct synchronous typing (polish replace path — idle-only by U.4)."""
        if self._aborted or (self._app is not None
                             and getattr(self._app, "is_exiting", False)):
            return                                   # M2: no injection while exiting
        self._sender(text)

    def tap_back(self, count: int) -> None:
        """Select-back seam for polish_engine (Windows SHIFT+VK_LEFT)."""
        tap_shift_left(count)

    def abort(self) -> None:
        """Hard stop used by session abort: nothing types after this (M2)."""
        self._aborted = True

    # ---------------- pipeline ---------------------------------------------
    def _run(self) -> None:
        while not (self._stopped and self._queue.empty()):
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                self._try_resume()
                continue
            if item is STOP:
                break
            if self._paused:
                self._retained.append(item)         # keep order; typed on resume
                continue
            self._hold(item)
            self._flush_ready()
            self._try_resume()

    def _hold(self, emission) -> None:
        seq = getattr(emission, "emission_seq", -1)
        if isinstance(seq, int) and seq < self._expected:
            # Idempotent duplicate replay (A3 s2.4): dropped WITH a counter/log.
            self._dropped_duplicates += 1
            logger.debug("Duplicate emission_seq %d dropped", seq)
            return
        if seq in self._buffer:
            return
        self._buffer[seq] = emission
        if self._expected not in self._buffer and self._buffer:
            self._gap_submissions += 1
            if self._gap_submissions > self._max_gap:
                # Lost-unit skip: WARN — never silently lose order or stall.
                self._dropped_lost += 1
                next_seq = min(self._buffer)
                logger.warning("Emission gap at seq %d declared lost; resuming at %d",
                               self._expected, next_seq)
                self._expected = next_seq
                self._gap_submissions = 0

    def _flush_ready(self) -> None:
        while not self._paused:
            emission = self._buffer.pop(self._expected, None)
            if emission is None:
                return
            try:
                result = self.guarded_emit(emission)
            except FocusLostError:
                return                               # emission already retained; paused
            if result is EmissionResult.TYPED:
                self._expected += 1
                self._gap_submissions = 0
            else:                                    # SUPPRESSED: drop without typing
                self._expected += 1

    def _try_resume(self) -> None:
        """Focus restored -> replay the retained backlog through the pipeline
        in its original order (nothing typed twice: expected never advanced)."""
        if not self._paused:
            return
        try:
            self.focus_guard.check()
        except FocusLostError:
            return                                   # still away: keep pausing
        for emission in self._retained:
            self._buffer.setdefault(emission.emission_seq, emission)
        self._retained.clear()
        self._paused = False
        self._flush_ready()

    def _notify_status(self, message: str) -> None:
        app = self._app
        if app is None:
            logger.info(message)
            return
        try:
            app.call_in_ui(lambda: app.set_status(message, "error"))
        except Exception:
            logger.debug("Status notification failed", exc_info=True)


def build_typer(app, snapshot) -> TyperActor:
    """Per-session composition root (adapter wiring): focus guard + ONE session
    clipboard + span registry + platform sender.  Snapshot fixes the insertion
    mode recorded with every typed span (D10: session-start settings)."""
    clipboard = SessionClipboard()
    registry = SpanRegistry()
    guard = FocusGuard(on_key=registry.invalidate_all)
    return TyperActor(
        focus_guard=guard,
        clipboard=clipboard,
        registry=registry,
        sender=default_sender(clipboard),
        app=app,
        insertion_mode=getattr(snapshot, "polish_mode", "insert") or "insert",
    )
