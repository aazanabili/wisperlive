"""Focus guard tests (plan 2.3; gate G2 focus + hook self-filter).

Owner: qa-test-assurance.  Seams are INJECTED: ``foreground_seam`` is a plain
callable double (no real GetForegroundWindow), and the WH_KEYBOARD_LL hook is
NEVER actually installed here — its filter contract is pinned through the
``notify_key_event`` seam plus an AST check that the hook proc derives the
``LLKHF_INJECTED`` flag.

Covers: focus change -> ``FocusLostError`` (target baseline on FIRST emission),
honest degradation when focus is unobservable, the PROOF semantics of
``unchanged()`` for replace-experimental, and the injected-vs-real key filter
(our own SendInput output must never invalidate spans).
"""

import ast
import os
import unittest
from unittest import mock

import focus_guard
from llm_errors import FocusLostError
from focus_guard import FocusGuard


class SeamDouble:
    """Programmable foreground-window seam (hwnd ints, 0 = none)."""

    def __init__(self, *values):
        self.values = list(values)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if not self.values:
            raise AssertionError("seam exhausted")
        value = self.values[0]
        if len(self.values) > 1:
            self.values.pop(0)
        return value


class FocusIdentityTests(unittest.TestCase):
    def test_first_emission_baselines_the_target_window(self):
        seam = SeamDouble(111)
        guard = FocusGuard(foreground_seam=seam)
        guard.check()                                  # first check captures target
        self.assertEqual(guard._target, 111)
        guard.check()                                  # same window: no raise
        guard.reset_target()
        self.assertIsNone(guard._target)

    def test_focus_change_raises_focus_lost_error(self):
        seam = SeamDouble(111, 222)
        guard = FocusGuard(foreground_seam=seam)
        guard.check()                                  # baseline 111
        with self.assertRaises(FocusLostError) as ctx:
            guard.check()                              # 222: another window
        message = str(ctx.exception)
        self.assertIn("window", message.lower())
        self.assertIn("nothing was lost", message)     # user-safe copy (D4)

    def test_focus_flapping_back_to_target_does_not_raise(self):
        seam = SeamDouble(111, 222, 111)
        guard = FocusGuard(foreground_seam=seam)
        guard.check()
        with self.assertRaises(FocusLostError):
            guard.check()
        guard.reset_target()                           # user hit Resume (D4)
        guard.check()                                  # re-baselines on the target
        self.assertEqual(guard._target, 111)

    def test_unobservable_focus_never_blocks_typing(self):
        # seam returns 0 (no window) or raises -> degrade honestly (PLATFORM_REQUIRED)
        seam = SeamDouble(0, 111, 0)
        guard = FocusGuard(foreground_seam=seam)
        guard.check()                                  # 0: unknown, no baseline
        guard.check()                                  # 111: baseline now
        guard.check()                                  # 0: unknown -> never blocks
        self.assertEqual(guard._target, 111)
        # a raising seam logs ONCE and degrades to "never blocks"
        failing = mock.Mock(side_effect=RuntimeError("no display"))
        guard2 = FocusGuard(foreground_seam=failing)
        with mock.patch.object(focus_guard, "logger") as logger, \
                mock.patch.object(focus_guard, "_seam_note_logged", False):
            guard2.check()
            guard2.check()
            failing.assert_called()                    # keeps trying (honest)
            logger.info.assert_called()                # NOTE logged exactly once
            self.assertEqual(logger.info.call_count, 1)

    def test_no_seam_platform_degrades_completely(self):
        # A platform WITHOUT any foreground seam (Wayland-only, unknown OS):
        # default_foreground_seam() -> None.  Injected here by patching the
        # platform probe (passing None to the ctor means "resolve default").
        with mock.patch.object(focus_guard, "default_foreground_seam",
                               return_value=None):
            guard = FocusGuard()
        guard.check()                                  # never raises
        self.assertIsNone(guard._target)
        self.assertFalse(guard.unchanged())            # NOT proof -> polish falls back

    def test_seam_none_means_default_platform_seam(self):
        expected = focus_guard.default_foreground_seam()
        guard = FocusGuard()
        if os.name == "nt":
            self.assertIs(guard._seam, focus_guard._windows_foreground)
        elif os.name == "posix":
            self.assertIs(guard._seam, expected)


class ProofUnchangedTests(unittest.TestCase):
    """G2: replace-experimental needs PROOF; unknown focus is never proof."""

    def test_equal_foreground_is_proof(self):
        seam = SeamDouble(111, 111)
        guard = FocusGuard(foreground_seam=seam)
        self.assertTrue(guard.unchanged())             # first call baselines + proof
        self.assertTrue(guard.unchanged())

    def test_changed_foreground_is_disproof(self):
        seam = SeamDouble(111, 222)
        guard = FocusGuard(foreground_seam=seam)
        self.assertTrue(guard.unchanged())
        self.assertFalse(guard.unchanged())

    def test_unknown_foreground_is_never_proof(self):
        seam = SeamDouble(0)
        guard = FocusGuard(foreground_seam=seam)
        self.assertFalse(guard.unchanged())
        # a raising seam is also not proof
        guard2 = FocusGuard(foreground_seam=mock.Mock(side_effect=OSError))
        with mock.patch.object(focus_guard, "_seam_note_logged", True):
            self.assertFalse(guard2.unchanged())


class KeyHookFilterTests(unittest.TestCase):
    """G2 self-filter: NON-injected (real user) keys invalidate spans; our own
    injected SendInput output (LLKHF_INJECTED) must be ignored."""

    def test_injected_keys_never_invalidate(self):
        on_key = mock.MagicMock()
        guard = FocusGuard(foreground_seam=None, on_key=on_key)
        self.assertFalse(guard.notify_key_event(True))   # injected -> filtered
        on_key.assert_not_called()

    def test_real_user_keys_invalidate(self):
        on_key = mock.MagicMock()
        guard = FocusGuard(foreground_seam=None, on_key=on_key)
        self.assertTrue(guard.notify_key_event(False))
        on_key.assert_called_once_with()

    def test_callback_failure_never_raises_into_the_hook_chain(self):
        on_key = mock.Mock(side_effect=ValueError("callback bug"))
        guard = FocusGuard(foreground_seam=None, on_key=on_key)
        with mock.patch.object(focus_guard, "logger") as logger:
            self.assertTrue(guard.notify_key_event(False))   # still reports dispatch
            logger.exception.assert_called_once()
        self.assertFalse(guard.notify_key_event(True))       # filter first anyway

    def test_missing_callback_is_a_safe_noop(self):
        guard = FocusGuard(foreground_seam=None, on_key=None)
        self.assertFalse(guard.notify_key_event(True))
        self.assertTrue(guard.notify_key_event(False))       # returns filter verdict

    def test_hook_proc_derives_the_injected_flag_from_llkhf_injected(self):
        # AST pin: the WH_KEYBOARD_LL callback must dispatch through
        # notify_key_event(bool(info.flags & LLKHF_INJECTED)) — the exact
        # self-filter expression (a bare bool(flags) would mute real keys).
        with open(focus_guard.__file__, encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute)
                    and func.attr == "notify_key_event"):
                continue
            arg = node.args[0] if node.args else None
            text = ast.unparse(arg) if arg is not None else ""
            self.assertIn("LLKHF_INJECTED", text,
                          "hook must filter on the LLKHF_INJECTED flag")
            self.assertIn("flags", text)
            found = True
        self.assertTrue(found, "no notify_key_event dispatch inside the hook loop")
        # flag/constants are the shipped Win32 values
        self.assertEqual(focus_guard.LLKHF_INJECTED, 0x10)
        self.assertEqual(focus_guard.WH_KEYBOARD_LL, 13)

    @unittest.skipUnless(os.name == "nt", "hook is a Windows-only seam")
    def test_start_stop_hook_is_idempotent_and_never_raises(self):
        # NOTE: to avoid installing a REAL WH_KEYBOARD_LL chain in the test
        # process, the Win32 seam itself is mocked — only the lifecycle logic
        # (single thread, stop unhook path) is exercised.
        guard = FocusGuard(foreground_seam=None, on_key=lambda: None)
        fake_thread = mock.MagicMock()
        fake_thread.is_alive.return_value = True
        with mock.patch.object(focus_guard.threading, "Thread",
                               return_value=fake_thread) as thread_cls:
            guard.start_key_hook()
            guard.start_key_hook()                     # idempotent
            thread_cls.assert_called_once()
            self.assertIsNotNone(guard._hook_thread)
            guard.stop_key_hook()
            guard.stop_key_hook()                      # idempotent teardown
            self.assertTrue(guard._hook_stop.is_set())  # stop event signalled
            fake_thread.join.assert_called()
            self.assertIsNone(guard._hook_thread)       # reference dropped

    def test_non_windows_hook_is_a_logged_noop(self):
        guard = FocusGuard(foreground_seam=None)
        fake_os = mock.Mock()
        fake_os.name = "posix"
        with mock.patch.object(focus_guard, "os", fake_os), \
                mock.patch.object(focus_guard, "logger") as logger, \
                mock.patch.object(focus_guard, "_hook_note_logged", False):
            guard.start_key_hook()                     # documented no-op
            self.assertIsNone(guard._hook_thread)
            logger.info.assert_called_once()
            self.assertIn("PLATFORM_REQUIRED", logger.info.call_args.args[0])
        guard.stop_key_hook()                          # never raises when idle


class HookMessageLayoutTests(unittest.TestCase):
    def test_msg_structure_is_the_full_win32_layout(self):
        # An undersized MSG buffer passed to GetMessageW is a memory-safety bug;
        # pin the shipped field set (hwnd/message/wParam/lParam/time/pt/private).
        fields = [name for name, _type in focus_guard._MSG._fields_]
        self.assertEqual(fields, ["hwnd", "message", "wParam", "lParam",
                                  "time", "pt", "lPrivate"])
        point_fields = [name for name, _type in focus_guard._MSG._POINT._fields_]
        self.assertEqual(point_fields, ["x", "y"])


if __name__ == "__main__":
    unittest.main()
