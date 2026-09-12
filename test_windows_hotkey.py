import unittest

from windows_hotkey import HotkeyError, MOD_CONTROL, MOD_SHIFT, parse_shortcut


class ParseShortcutTests(unittest.TestCase):
    def test_parses_default_shortcut(self):
        self.assertEqual(parse_shortcut("ctrl+space"), (MOD_CONTROL, 0x20))

    def test_parses_function_key_and_multiple_modifiers(self):
        self.assertEqual(parse_shortcut("Ctrl + Shift + F12"), (MOD_CONTROL | MOD_SHIFT, 0x7B))

    def test_parses_letter_case_insensitively(self):
        self.assertEqual(parse_shortcut("ctrl+k"), (MOD_CONTROL, ord("K")))

    def test_parses_tkinter_punctuation_name(self):
        self.assertEqual(parse_shortcut("ctrl+slash"), (MOD_CONTROL, 0xBF))

    def test_rejects_modifier_without_main_key(self):
        with self.assertRaisesRegex(HotkeyError, "non-modifier"):
            parse_shortcut("ctrl+shift")

    def test_rejects_multiple_main_keys(self):
        with self.assertRaisesRegex(HotkeyError, "only one"):
            parse_shortcut("ctrl+k+m")

    def test_rejects_unknown_key(self):
        with self.assertRaisesRegex(HotkeyError, "Unsupported"):
            parse_shortcut("ctrl+not-a-key")

    def test_parses_windows_modifier_alias(self):
        self.assertEqual(parse_shortcut("win+space"), (0x0008, 0x20))


if __name__ == "__main__":
    unittest.main()
