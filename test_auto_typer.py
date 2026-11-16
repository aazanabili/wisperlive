"""Isolated tests for auto_typer.py (core proposal section 8 + section 3.2).

pyperclip, `_send_paste_keystroke`, time.sleep and threading.Timer are patched
- no real keystrokes, no real clipboard access, no real delays. Timer instances
are captured and fired manually so restore timing is deterministic.

Reconciliation (core section 12.2): the merged module resolves the paste
keystroke LAZILY (`auto_typer.keyboard` starts as None and is only imported on
Windows), so the old `mock.patch.object(auto_typer.keyboard, "send")` seam is
retargeted to `mock.patch.object(auto_typer, "_send_paste_keystroke")` (the
platform dispatch seam itself). The exact `send("ctrl+v")` assertion survives
verbatim in `DispatchSeamTests` (core 12.2 ADDs) below.
"""

import unittest
from unittest import mock

import pyperclip

import auto_typer
from auto_typer import (
    CLIPBOARD_RESTORE_DELAY_S,
    CLIPBOARD_RETRY_ATTEMPTS,
    CLIPBOARD_RETRY_DELAY_S,
    PASTE_SETTLE_DELAY_S,
    PasteError,
    paste_text,
)


class FakeTimer:
    """Captures threading.Timer scheduling; fired manually by the test."""

    instances = []

    def __init__(self, interval, function):
        self.interval = interval
        self.function = function
        self.daemon = None
        self.started = False
        FakeTimer.instances.append(self)

    def start(self):
        self.started = True

    def fire(self):
        self.function()


class AutoTyperTests(unittest.TestCase):
    def setUp(self):
        FakeTimer.instances = []
        self.clipboard_content = "OLD"
        self.copy_calls = []

        self.paste_patch = mock.patch.object(auto_typer.pyperclip, "paste")
        self.copy_patch = mock.patch.object(auto_typer.pyperclip, "copy")
        # core 12.2 seam retarget: the merged dispatch function replaces the old
        # eager `auto_typer.keyboard.send` patch (keyboard is now lazy/None).
        self.send_patch = mock.patch.object(auto_typer, "_send_paste_keystroke")
        self.sleep_patch = mock.patch.object(auto_typer.time, "sleep")
        self.timer_patch = mock.patch.object(auto_typer.threading, "Timer")
        self.paste_mock = self.paste_patch.start()
        self.copy_mock = self.copy_patch.start()
        self.send_mock = self.send_patch.start()
        self.sleep_mock = self.sleep_patch.start()
        self.timer_mock = self.timer_patch.start()
        for patcher in (self.timer_patch, self.sleep_patch, self.send_patch,
                        self.copy_patch, self.paste_patch):
            self.addCleanup(patcher.stop)

        self.paste_mock.side_effect = lambda: self.clipboard_content

        def fake_copy(text):
            self.copy_calls.append(text)
            self.clipboard_content = text

        self.copy_mock.side_effect = fake_copy
        self.timer_mock.side_effect = lambda interval, function: FakeTimer(interval, function)

    # 1. save -> paste -> delayed restore when nothing newer was copied
    def test_clipboard_restored_after_delay_when_unchanged(self):
        paste_text("NEW")

        self.send_mock.assert_called_once()  # the "ctrl+v" dispatch is asserted by DispatchSeamTests
        self.sleep_mock.assert_any_call(PASTE_SETTLE_DELAY_S)
        self.assertEqual(self.clipboard_content, "NEW")  # restore is deferred, not immediate
        self.assertEqual(len(FakeTimer.instances), 1)
        timer = FakeTimer.instances[0]
        self.assertTrue(timer.started)
        self.assertTrue(timer.daemon, "restore timer must never block teardown")
        self.assertEqual(timer.interval, CLIPBOARD_RESTORE_DELAY_S)
        self.assertEqual(CLIPBOARD_RESTORE_DELAY_S, 1.0)  # shipped contract (plan revision log)

        timer.fire()
        self.assertEqual(self.clipboard_content, "OLD")
        self.assertEqual(self.copy_calls, ["NEW", "OLD"])

    # 2. equality guard: a user copy made during the delay window is never clobbered
    def test_restore_skipped_when_user_copied_something_new(self):
        paste_text("NEW")
        timer = FakeTimer.instances[0]

        self.clipboard_content = "USER-COPIED-AFTER-PASTE"
        timer.fire()

        self.assertEqual(self.clipboard_content, "USER-COPIED-AFTER-PASTE")
        self.assertEqual(self.copy_calls, ["NEW"], "no restore copy may be issued")

    # 3. clipboard busy -> bounded retry then PasteError BEFORE any keystroke
    def test_copy_failure_raises_paste_error_and_never_sends_keystroke(self):
        self.copy_mock.side_effect = pyperclip.PyperclipException("clipboard busy")

        with self.assertRaises(PasteError) as ctx:
            paste_text("NEW")

        self.assertEqual(str(ctx.exception), "The clipboard is busy. Please try again.")
        self.assertIsInstance(ctx.exception.__cause__, pyperclip.PyperclipException)
        self.send_mock.assert_not_called()  # stale clipboard content is never pasted
        self.assertEqual(self.copy_mock.call_count, CLIPBOARD_RETRY_ATTEMPTS)  # bounded
        self.assertEqual(self.clipboard_content, "OLD")  # clipboard untouched
        self.assertEqual(FakeTimer.instances, [])  # raised before the paste try/finally
        sleep_calls = [call.args[0] for call in self.sleep_mock.call_args_list if call.args]
        self.assertIn(CLIPBOARD_RETRY_DELAY_S, sleep_calls)  # bounded inter-retry delay
        self.assertNotIn(PASTE_SETTLE_DELAY_S, sleep_calls)  # never reached the paste step

    # 4. keystroke send failure -> PasteError, restore still scheduled (finally)
    def test_send_failure_raises_paste_error_and_schedules_restore(self):
        self.send_mock.side_effect = RuntimeError("keyboard hook failed")

        with self.assertRaises(PasteError) as ctx:
            paste_text("NEW")

        self.assertEqual(str(ctx.exception), "Could not send the paste keystroke to the active window.")
        self.assertIsInstance(ctx.exception.__cause__, RuntimeError)
        self.assertEqual(len(FakeTimer.instances), 1, "finally must schedule the restore")
        FakeTimer.instances[0].fire()
        self.assertEqual(self.clipboard_content, "OLD")

    # 5. unreadable clipboard -> proceed with paste, skip restore entirely
    def test_paste_read_failure_proceeds_without_restore(self):
        self.paste_mock.side_effect = pyperclip.PyperclipException("clipboard locked")

        paste_text("NEW")  # must not raise

        self.copy_mock.assert_called_once_with("NEW")
        self.send_mock.assert_called_once()
        self.assertEqual(FakeTimer.instances, [], "no saved clipboard -> no restore")
        self.assertEqual(self.clipboard_content, "NEW")

    # 6. empty text is a strict no-op (section 8 case 6)
    def test_empty_text_is_noop(self):
        paste_text("")
        paste_text(None)

        self.paste_mock.assert_not_called()
        self.copy_mock.assert_not_called()
        self.send_mock.assert_not_called()
        self.sleep_mock.assert_not_called()
        self.assertEqual(FakeTimer.instances, [])


class DispatchSeamTests(unittest.TestCase):
    """core 12.2 ADDs: the merged `_IS_WINDOWS` paste dispatch (upstream XTEST
    branch vs Windows keyboard branch) must be covered explicitly."""

    # ADD: Windows dispatch sends the real `keyboard.send("ctrl+v")` call
    def test_windows_dispatch_uses_keyboard_send(self):
        fake_keyboard = mock.Mock()
        with mock.patch.object(auto_typer, "_IS_WINDOWS", True), \
                mock.patch.object(auto_typer, "keyboard", fake_keyboard):
            auto_typer._send_paste_keystroke()

        fake_keyboard.send.assert_called_once_with("ctrl+v")

    # ADD: non-Windows dispatch routes through linux_hotkey.send_paste (XTEST)
    def test_linux_dispatch_uses_xtest_paste(self):
        with mock.patch.object(auto_typer, "_IS_WINDOWS", False), \
                mock.patch("linux_hotkey.send_paste") as send_paste:
            auto_typer._send_paste_keystroke()

        send_paste.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
