import os
import unittest

from linux_hotkey import (
    ALT_MASK,
    CONTROL_MASK,
    SHIFT_MASK,
    SUPER_MASK,
    HotkeyError,
    parse_shortcut,
)


class ParseShortcutTests(unittest.TestCase):
    def test_parses_default_shortcut(self):
        self.assertEqual(parse_shortcut("ctrl+space"), (CONTROL_MASK, 0x0020))

    def test_parses_function_key_and_multiple_modifiers(self):
        self.assertEqual(parse_shortcut("Ctrl + Shift + F12"), (CONTROL_MASK | SHIFT_MASK, 0xFFC9))

    def test_parses_letter_case_insensitively(self):
        self.assertEqual(parse_shortcut("ctrl+k"), (CONTROL_MASK, ord("k")))

    def test_parses_tkinter_punctuation_name(self):
        self.assertEqual(parse_shortcut("ctrl+slash"), (CONTROL_MASK, 0x002F))

    def test_parses_punctuation_character(self):
        self.assertEqual(parse_shortcut("ctrl+;"), (CONTROL_MASK, 0x003B))

    def test_maps_windows_modifier_to_super(self):
        self.assertEqual(parse_shortcut("win+space"), (SUPER_MASK, 0x0020))

    def test_parses_alt_modifier(self):
        self.assertEqual(parse_shortcut("alt+tab"), (ALT_MASK, 0xFF09))

    def test_rejects_modifier_without_main_key(self):
        with self.assertRaisesRegex(HotkeyError, "non-modifier"):
            parse_shortcut("ctrl+shift")

    def test_rejects_multiple_main_keys(self):
        with self.assertRaisesRegex(HotkeyError, "only one"):
            parse_shortcut("ctrl+k+m")

    def test_rejects_unknown_key(self):
        with self.assertRaisesRegex(HotkeyError, "Unsupported"):
            parse_shortcut("ctrl+not-a-key")


@unittest.skipUnless(
    os.environ.get("WHISPERLIVE_X11_TESTS"),
    "set WHISPERLIVE_X11_TESTS=1 with an X11 display to run the X11 smoke tests",
)
class X11SmokeTests(unittest.TestCase):
    """Exercise the real X11 code paths against a live display (e.g. Xvfb)."""

    def test_registers_and_stops_hotkey(self):
        from linux_hotkey import LinuxGlobalHotkey

        pressed = []
        hotkey = LinuxGlobalHotkey("ctrl+space", lambda: pressed.append(1))
        hotkey.start()
        try:
            self.assertFalse(hotkey.is_main_key_pressed())
        finally:
            hotkey.stop()

    def test_conflicting_registration_is_reported(self):
        from linux_hotkey import LinuxGlobalHotkey

        first = LinuxGlobalHotkey("ctrl+alt+k", lambda: None)
        first.start()
        second = LinuxGlobalHotkey("ctrl+alt+k", lambda: None)
        try:
            with self.assertRaisesRegex(HotkeyError, "already in use"):
                second.start()
        finally:
            first.stop()
            second.stop()

    def test_send_paste_runs(self):
        from linux_hotkey import send_paste

        send_paste()


if __name__ == "__main__":
    unittest.main()
