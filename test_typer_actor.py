"""Typing actor tests (plan 2.3; gates G1 ordering + G2 injection).

Owner: qa-test-assurance.  All platform seams are INJECTED doubles (focus
guard / clipboard / sender), so no real keystroke, clipboard access, or
WH_KEYBOARD_LL hook ever runs here.

Covers:
* ordered emission: out-of-order arrival re-ordered by ``emission_seq``,
  idempotent duplicate drops, bounded gap-loss skip (G1),
* the Arabic UNICODE SendInput path (mocked wide-char payload — G2),
* ONE clipboard restore per session + non-clobber of a user copy (G2/D4),
* the M2 ``is_exiting``/abort freeze: ZERO injection after exit,
* focus loss: emission retained (never typed blind) + resume replay.
"""

import enum
import unittest
from unittest import mock

import auto_typer
import typer_actor
from llm_errors import FocusLostError
from typer_actor import EmissionResult, TyperActor


def _emission(text, seq, *, kind="phrase", final=True):
    from dictation_types import EmissionOut
    return EmissionOut(text=text, segment_seq=seq, emission_seq=seq,
                       kind=kind, is_final=final)


class FakeClipboard:
    def __init__(self):
        self.opened = 0
        self.closed = 0
        self.pasted = []
        self.copied = []
        self.paste_calls = 0

    def open(self):
        self.opened += 1

    def put_and_paste(self, text):
        self.paste_calls += 1
        self.pasted.append(text)

    def copy_only(self, text):
        self.copied.append(text)

    def close(self):
        self.closed += 1


class FakeFocusGuard:
    def __init__(self):
        self.target = None
        self.hook_starts = 0
        self.hook_stops = 0
        self.away = False
        self.resets = 0

    def start_key_hook(self):
        self.hook_starts += 1

    def stop_key_hook(self):
        self.hook_stops += 1

    def check(self):
        if self.away:
            raise FocusLostError("Typing moved to another window.")

    def reset_target(self):
        self.resets += 1


def make_actor(*, sender=None, focus=None, clipboard=None, app=None,
               insertion_mode="insert", clock=lambda: 1000.0, max_gap=32):
    clipboard = clipboard or FakeClipboard()
    focus = focus or FakeFocusGuard()
    actor = TyperActor(focus_guard=focus, clipboard=clipboard,
                       registry=mock.MagicMock(), sender=sender or mock.MagicMock(),
                       app=app, insertion_mode=insertion_mode, clock=clock,
                       max_gap=max_gap)
    return actor, focus, clipboard


def wait_until(predicate, timeout=5.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class LifecycleTests(unittest.TestCase):
    def test_start_stop_opens_and_closes_the_session_clipboard_once(self):
        sender = mock.MagicMock()
        actor, focus, clipboard = make_actor(sender=sender)
        actor.start()
        actor.start()                                  # idempotent: one thread only
        self.assertEqual(focus.hook_starts, 1)
        self.assertEqual(clipboard.opened, 1)
        actor.stop()
        actor.stop()                                   # idempotent teardown
        self.assertEqual(clipboard.closed, 1)          # ONE restore per session (D4)
        self.assertEqual(focus.hook_stops, 1)

    def test_submit_after_stop_is_refused(self):
        actor, _focus, _clip = make_actor()
        actor.start()
        self.assertTrue(actor.submit(_emission("a", 0)))
        actor.stop()
        self.assertFalse(actor.submit(_emission("b", 1)))  # never typed after stop


class OrderedEmissionTests(unittest.TestCase):
    def _drain(self, actor, emissions):
        actor.start()
        for emission in emissions:
            actor.submit(emission)
        ok = wait_until(lambda: actor.typed_count + actor._dropped_duplicates +
                         actor._dropped_lost >= len(set(
                             e.emission_seq for e in emissions))
                        and actor._queue.empty())
        self.assertTrue(ok, "actor never drained the backlog")
        return ok

    def test_out_of_order_arrival_is_typed_strictly_in_order(self):
        sent = []
        actor, _focus, _clip = make_actor(sender=sent.append)
        # engines finish out of order: 2, 0, 1 (G1 ordered emission)
        self._drain(actor, [_emission("c", 2), _emission("a", 0),
                            _emission("b", 1)])
        self.assertEqual(sent, ["a", "b", "c"])
        self.assertEqual(actor.typed_count, 3)
        actor.stop()

    def test_duplicate_replay_is_dropped_idempotently(self):
        sent = []
        actor, _focus, _clip = make_actor(sender=sent.append)
        self._drain(actor, [_emission("a", 0), _emission("b", 1),
                            _emission("a", 0),                 # replay of 0
                            _emission("b", 1),                 # replay of 1
                            _emission("c", 2)])
        self.assertEqual(sent, ["a", "b", "c"])
        self.assertGreaterEqual(actor._dropped_duplicates, 2)
        actor.stop()

    def test_gap_is_buffered_until_the_missing_unit_arrives(self):
        sent = []
        actor, _focus, _clip = make_actor(sender=sent.append, max_gap=32)
        self._drain(actor, [_emission("a", 0), _emission("c", 2),
                            _emission("b", 1)])
        self.assertEqual(sent, ["a", "b", "c"])        # gap 1 arrived late: full order
        self.assertEqual(actor._dropped_lost, 0)
        actor.stop()

    def test_persistent_gap_is_declared_lost_after_max_gap_submissions(self):
        sent = []
        actor, _focus, _clip = make_actor(sender=sent.append, max_gap=2)
        actor.start()
        for seq in (0, 5, 7, 9):                       # missing 1 forever
            actor.submit(_emission(f"u{seq}", seq))
        self.assertTrue(wait_until(lambda: actor.typed_count >= 2
                                   and actor._queue.empty()))
        self.assertEqual(sent, ["u0", "u5"])           # skip to the oldest BUFFERED unit
        self.assertEqual(actor._dropped_lost, 1)       # declared lost WITH a warning
        self.assertNotIn("u1", sent)
        actor.stop()

    def test_suppressed_emission_still_advances_the_sequence(self):
        actor, _focus, _clip = make_actor(sender=mock.MagicMock())
        actor.abort()                                  # M2 freeze before any typing
        actor.start()
        actor.submit(_emission("a", 0))
        self.assertTrue(wait_until(lambda: actor._queue.empty()))
        self.assertEqual(actor.typed_count, 0)
        actor.stop()

    def test_retained_backlog_view_is_oldest_first(self):
        actor, _focus, _clip = make_actor()
        actor._retained = [_emission("second", 1), _emission("first", 0)]
        self.assertEqual(actor.retained_texts(), ["first", "second"])
        self.assertEqual(actor.retained_count, 2)


class GuardedInjectionTests(unittest.TestCase):
    def test_focus_change_retains_the_emission_and_never_types_it(self):
        sent = []
        focus = FakeFocusGuard()
        focus.away = True
        app = mock.MagicMock()
        app.is_exiting = False
        actor, _focus2, _clip = make_actor(sender=sent.append, focus=focus,
                                           app=app)
        with self.assertRaises(FocusLostError):
            actor.guarded_emit(_emission("kept", 0))
        self.assertEqual(sent, [])                      # nothing injected
        self.assertEqual(actor.retained_texts(), ["kept"])
        self.assertTrue(actor._paused)
        app.call_in_ui.assert_called()                  # status surfaced (D4 copy)

    def test_resume_replays_the_retained_backlog_once(self):
        sent = []
        focus = FakeFocusGuard()
        focus.away = True
        actor, _focus2, _clip = make_actor(sender=sent.append, focus=focus)
        actor.start()
        actor.submit(_emission("one", 0))
        self.assertTrue(wait_until(lambda: actor._paused and
                                   actor.retained_count >= 1))
        focus.away = False                             # user re-focused the target
        self.assertTrue(wait_until(lambda: sent == ["one"]))
        self.assertEqual(actor.retained_texts(), [])
        actor.stop()

    def test_abort_freezes_all_injection(self):
        sent = []
        actor, _focus, _clip = make_actor(sender=sent.append)
        actor.abort()
        self.assertEqual(actor.guarded_emit(_emission("x", 0)),
                         EmissionResult.SUPPRESSED)
        actor.type_text("direct")                      # polish path frozen too
        self.assertEqual(sent, [])

    def test_is_exiting_app_freezes_all_injection(self):
        # M2: the app flag alone must gate keystrokes (checked BEFORE the
        # first keystroke of every emission).
        sent = []
        app = mock.MagicMock()
        app.is_exiting = True
        actor, _focus, _clip = make_actor(sender=sent.append, app=app)
        self.assertEqual(actor.guarded_emit(_emission("x", 0)),
                         EmissionResult.SUPPRESSED)
        actor.type_text("direct")
        self.assertEqual(sent, [])
        self.assertEqual(actor.typed_count, 0)

    def test_paste_error_is_suppressed_and_noted_without_a_span(self):
        def broken(text):
            raise auto_typer.PasteError("Could not send the paste keystroke.")

        app = mock.MagicMock()
        app.is_exiting = False
        actor, _focus, _clip = make_actor(sender=broken, app=app)
        result = actor.guarded_emit(_emission("x", 0))
        self.assertEqual(result, EmissionResult.SUPPRESSED)
        self.assertEqual(actor.typed_count, 0)
        actor.registry.record_typed.assert_not_called()  # spans only post-success (D3)
        app.call_in_ui.assert_called()

    def test_successful_emit_records_the_span_after_injection(self):
        actor, _focus, _clip = make_actor(sender=mock.MagicMock(),
                                          insertion_mode="replace-experimental",
                                          clock=lambda: 1234.0)
        result = actor.guarded_emit(_emission("text", 9))
        self.assertEqual(result, EmissionResult.TYPED)
        actor.registry.record_typed.assert_called_once_with(
            9, "text", "replace-experimental", 1234)
        self.assertEqual(actor.typed_count, 1)
        self.assertTrue(actor.typed_any)

    def test_abort_between_focus_wait_and_injection_still_suppresses(self):
        # Re-check AFTER the focus wait (quit may have raced a pause — M2).
        def check_then_quit():
            actor.abort()

        focus = mock.MagicMock()
        focus.check.side_effect = check_then_quit
        actor, _f2, _clip = make_actor(sender=mock.MagicMock(), focus=focus)
        result = actor.guarded_emit(_emission("x", 0))
        self.assertEqual(result, EmissionResult.SUPPRESSED)


class UnicodeInjectionTests(unittest.TestCase):
    """G2: Arabic cannot come from scan codes — KEYEVENTF_UNICODE pairs per
    UTF-16 code unit are the WHOLE reason this actor exists (D4)."""

    def test_arabic_text_emits_unicode_down_up_pairs(self):
        text = "مرحبا بالعالم"                       # Arabic + space
        events = []
        with mock.patch.object(auto_typer, "_send_input_batch",
                               side_effect=lambda evts: events.extend(evts)
                               or len(evts)):
            auto_typer.send_unicode_text(text)
        units = [text.encode("utf-16-le")[i:i + 2]
                 for i in range(0, len(text.encode("utf-16-le")), 2)]
        self.assertEqual(len(events), 2 * len(units))       # down + up per unit
        self.assertEqual(len(events), 2 * len(text))        # BMP chars: 1 unit each
        for index, unit in enumerate(units):
            down = events[2 * index]
            up = events[2 * index + 1]
            expected = unit[0] | (unit[1] << 8)             # s16le -> wScan
            self.assertEqual(down, (0, expected, auto_typer.KEYEVENTF_UNICODE))
            self.assertEqual(up, (0, expected,
                                  auto_typer.KEYEVENTF_UNICODE | auto_typer.KEYEVENTF_KEYUP))
            self.assertEqual(down[0], 0)                    # wVk 0: no scan-code mapping

    def test_surrogate_pair_emoji_emits_two_units(self):
        text = "\U0001F600"                             # astral plane -> surrogate pair
        events = []
        with mock.patch.object(auto_typer, "_send_input_batch",
                               side_effect=lambda evts: events.extend(evts)
                               or len(evts)):
            auto_typer.send_unicode_text(text)
        self.assertEqual(len(events), 4)                      # 2 units x (down+up)
        encoded = text.encode("utf-16-le")
        first = encoded[0] | (encoded[1] << 8)
        self.assertEqual(events[0], (0, first, auto_typer.KEYEVENTF_UNICODE))

    def test_empty_text_sends_nothing(self):
        with mock.patch.object(auto_typer, "_send_input_batch") as batch:
            auto_typer.send_unicode_text("")
        batch.assert_not_called()

    @unittest.skipUnless(auto_typer.os.name == "nt",
                         "SendInput is a Windows-only seam")
    def test_half_sent_batch_raises_paste_error(self):
        # accepted < sent must surface as PasteError (never pretend success)
        fake_user32 = mock.MagicMock()
        fake_user32.SendInput.return_value = 1        # 1 of 3 accepted -> half-sent
        with mock.patch.object(auto_typer.ctypes, "WinDLL",
                               return_value=fake_user32):
            with self.assertRaises(auto_typer.PasteError):
                auto_typer._send_input_batch([(0, 0x645, auto_typer.KEYEVENTF_UNICODE)] * 3)

    @unittest.skipUnless(auto_typer.os.name == "nt",
                         "SendInput is a Windows-only seam")
    def test_batch_is_chunked_to_the_bounded_input_size(self):
        fake_user32 = mock.MagicMock()
        fake_user32.SendInput.side_effect = lambda count, *_a: count
        events = [(0, 0x645, auto_typer.KEYEVENTF_UNICODE)] * (
            auto_typer.MAX_INPUT_EVENTS + 44)
        with mock.patch.object(auto_typer.ctypes, "WinDLL",
                               return_value=fake_user32):
            sent = auto_typer._send_input_batch(events)
        self.assertEqual(sent, len(events))
        calls = [call.args[0] for call in fake_user32.SendInput.call_args_list]
        self.assertEqual(calls, [auto_typer.MAX_INPUT_EVENTS, 44])  # bounded batches

    def test_typer_actor_arabic_path_routes_through_the_unicode_sender(self):
        # D4: default_sender on Windows is send_unicode_text (NOT clipboard paste)
        from typer_actor import default_sender
        clipboard = FakeClipboard()
        if auto_typer.os.name == "nt":
            self.assertIs(default_sender(clipboard), auto_typer.send_unicode_text)
        else:
            self.assertIs(default_sender(clipboard), clipboard.put_and_paste)


class SessionClipboardRestoreTests(unittest.TestCase):
    """G2/D4: ONE restore per session; a user copy made in the meantime wins."""

    def test_close_restores_exactly_once_and_idempotently(self):
        clipboard = auto_typer.SessionClipboard()
        with mock.patch.object(auto_typer, "_read_clipboard_safe",
                               return_value="user-before"), \
                mock.patch.object(auto_typer, "_copy_with_retry") as copy, \
                mock.patch.object(auto_typer.pyperclip, "paste",
                                  return_value="our-text"), \
                mock.patch.object(auto_typer.pyperclip, "copy") as pcopy:
            clipboard.open()
            clipboard.copy_only("our-text")            # session copies its phrase
            clipboard.close()
            clipboard.close()                          # repeated close: no-op
        copy.assert_called_once_with("our-text")       # exactly ONE session copy
        pcopy.assert_called_once_with("user-before")   # exactly ONE restore (D4)
        # NO restore timer exists in the streaming path (per-phrase timers banned)
        with mock.patch.object(auto_typer.threading, "Timer") as timer, \
                mock.patch.object(auto_typer, "_read_clipboard_safe",
                                  return_value="before"), \
                mock.patch.object(auto_typer, "_copy_with_retry"), \
                mock.patch.object(auto_typer.pyperclip, "paste",
                                  return_value="our-phrase"), \
                mock.patch.object(auto_typer.pyperclip, "copy"):
            clipboard2 = auto_typer.SessionClipboard()
            clipboard2.open()
            clipboard2.copy_only("phrase")
            clipboard2.close()
        timer.assert_not_called()

    def test_user_copy_during_the_session_is_never_clobbered(self):
        clipboard = auto_typer.SessionClipboard()
        with mock.patch.object(auto_typer, "_read_clipboard_safe",
                               return_value="user-before"), \
                mock.patch.object(auto_typer, "_copy_with_retry"), \
                mock.patch.object(auto_typer.pyperclip, "paste",
                                  return_value="USER-COPIED-LATER"), \
                mock.patch.object(auto_typer.pyperclip, "copy") as pcopy:
            clipboard.open()
            clipboard.copy_only("our-phrase")
            clipboard.close()                          # equality guard: mismatch -> skip
        pcopy.assert_not_called()                      # user's later copy wins (G2)
        # proof: restore happens only when the clipboard still holds OUR text
        clipboard3 = auto_typer.SessionClipboard()
        with mock.patch.object(auto_typer, "_read_clipboard_safe",
                               return_value="user-before"), \
                mock.patch.object(auto_typer, "_copy_with_retry"), \
                mock.patch.object(auto_typer.pyperclip, "paste",
                                  return_value="our-phrase"), \
                mock.patch.object(auto_typer.pyperclip, "copy") as pcopy3:
            clipboard3.open()
            clipboard3.copy_only("our-phrase")
            clipboard3.close()
        pcopy3.assert_called_once_with("user-before")

    def test_unreadable_clipboard_never_restores_garbage(self):
        clipboard = auto_typer.SessionClipboard()
        with mock.patch.object(auto_typer, "_read_clipboard_safe",
                               return_value=None), \
                mock.patch.object(auto_typer, "_copy_with_retry"), \
                mock.patch.object(auto_typer.pyperclip, "paste") as paste:
            clipboard.open()
            clipboard.copy_only("phrase")
            clipboard.close()
        paste.assert_not_called()                      # saved is None -> skip restore

    def test_stop_closes_clipboard_unless_app_is_exiting(self):
        app = mock.MagicMock()
        app.is_exiting = False
        actor, _focus, clipboard = make_actor(app=app)
        actor.start()
        actor.stop()
        self.assertEqual(clipboard.closed, 1)

        app2 = mock.MagicMock()
        app2.is_exiting = True
        actor2, _f2, clipboard2 = make_actor(app=app2)
        actor2.start()
        actor2.stop()                                  # exiting: no restore (M2)
        self.assertEqual(clipboard2.closed, 0)

    def test_undelivered_backlog_is_preserved_by_copy_only(self):
        app = mock.MagicMock()
        app.is_exiting = False
        focus = FakeFocusGuard()
        focus.away = True                              # everything stays retained
        clipboard = FakeClipboard()
        actor = TyperActor(focus_guard=focus, clipboard=clipboard,
                           registry=mock.MagicMock(),
                           sender=mock.MagicMock(), app=app, clock=lambda: 0.0)
        actor.start()
        actor.submit(_emission("kept-1", 0))
        actor.submit(_emission("kept-2", 1))
        self.assertTrue(wait_until(lambda: actor.retained_count >= 2))
        actor.stop()
        # nothing typed (still paused), the backlog is copied instead of lost
        self.assertEqual(actor.typed_count, 0)
        self.assertEqual(clipboard.closed, 1)           # exactly one restore...
        self.assertEqual(clipboard.copied, ["kept-1 kept-2"])  # ...then preservation


class BuildRootTests(unittest.TestCase):
    def test_build_typer_wires_snapshot_insertion_mode_without_side_effects(self):
        snapshot = mock.MagicMock()
        snapshot.polish_mode = "replace-experimental"
        with mock.patch.object(typer_actor, "SessionClipboard") as clip_cls, \
                mock.patch.object(typer_actor, "FocusGuard") as guard_cls, \
                mock.patch.object(typer_actor, "SpanRegistry") as reg_cls:
            actor = typer_actor.build_typer(object(), snapshot)
        self.assertIsInstance(actor, TyperActor)
        self.assertEqual(actor._insertion_mode, "replace-experimental")
        clip_cls.assert_called_once()
        guard_cls.assert_called_once()
        reg_cls.assert_called_once()
        clip_cls.return_value.open.assert_not_called()   # no side effects at build

    def test_default_insertion_mode_falls_back_to_insert(self):
        with mock.patch.object(typer_actor, "SessionClipboard"), \
                mock.patch.object(typer_actor, "FocusGuard"), \
                mock.patch.object(typer_actor, "SpanRegistry"):
            actor = typer_actor.build_typer(object(), object())   # no polish_mode attr
        self.assertEqual(actor._insertion_mode, "insert")

    def test_emission_result_vocabulary_is_pinned(self):
        self.assertEqual([member.value for member in EmissionResult],
                         ["typed", "suppressed", "focus_changed"])
        self.assertIsInstance(EmissionResult.TYPED, str)
        self.assertIsInstance(EmissionResult.TYPED, enum.Enum)


if __name__ == "__main__":
    unittest.main()
