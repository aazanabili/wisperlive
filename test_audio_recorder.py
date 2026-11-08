import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import audio_recorder


class BundledPortAudioTests(unittest.TestCase):
    def test_no_bundle_without_meipass(self):
        with mock.patch.object(audio_recorder.sys, "_MEIPASS", None, create=True):
            self.assertIsNone(audio_recorder._bundled_portaudio())

    def test_finds_bundle_at_meipass_root(self):
        with tempfile.TemporaryDirectory() as root:
            bundled = Path(root) / "libportaudio.so.2"
            bundled.write_bytes(b"fake")
            with mock.patch.object(audio_recorder.sys, "_MEIPASS", root, create=True):
                self.assertEqual(audio_recorder._bundled_portaudio(), str(bundled))

    def test_finds_bundle_in_internal_layout(self):
        with tempfile.TemporaryDirectory() as root:
            internal = Path(root) / "_internal"
            internal.mkdir()
            bundled = internal / "libportaudio.so.2"
            bundled.write_bytes(b"fake")
            with mock.patch.object(audio_recorder.sys, "_MEIPASS", root, create=True):
                self.assertEqual(audio_recorder._bundled_portaudio(), str(bundled))

    def test_returns_none_without_bundled_file(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(audio_recorder.sys, "_MEIPASS", root, create=True):
            self.assertIsNone(audio_recorder._bundled_portaudio())


class FindLibraryWrapperTests(unittest.TestCase):
    def test_no_wrapper_outside_frozen_build(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(audio_recorder.sys, "_MEIPASS", root, create=True):
            (Path(root) / "libportaudio.so.2").write_bytes(b"fake")
            self.assertIsNone(audio_recorder._find_library_with_bundled_portaudio())

    def test_no_wrapper_without_bundled_file(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(audio_recorder.sys, "_MEIPASS", root, create=True), \
                mock.patch.object(audio_recorder.sys, "frozen", True, create=True):
            self.assertIsNone(audio_recorder._find_library_with_bundled_portaudio())

    def test_falls_back_to_bundle_only_for_portaudio(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(audio_recorder.sys, "_MEIPASS", root, create=True), \
                mock.patch.object(audio_recorder.sys, "frozen", True, create=True), \
                mock.patch("ctypes.util.find_library", return_value=None):
            bundled = Path(root) / "libportaudio.so.2"
            bundled.write_bytes(b"fake")
            wrapper = audio_recorder._find_library_with_bundled_portaudio()

            self.assertEqual(wrapper("portaudio"), str(bundled))
            self.assertIsNone(wrapper("X11"))

    def test_system_lookup_still_wins_when_available(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(audio_recorder.sys, "_MEIPASS", root, create=True), \
                mock.patch.object(audio_recorder.sys, "frozen", True, create=True), \
                mock.patch("ctypes.util.find_library", return_value="libportaudio.so.2"):
            (Path(root) / "libportaudio.so.2").write_bytes(b"fake")
            wrapper = audio_recorder._find_library_with_bundled_portaudio()

            self.assertEqual(wrapper("portaudio"), "libportaudio.so.2")


if __name__ == "__main__":
    unittest.main()
