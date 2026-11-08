import os
import queue
import sys
import threading


def _bundled_portaudio():
    """Return a PortAudio library bundled by PyInstaller, or None."""
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return None
    for folder in (base, os.path.join(base, "_internal")):
        for name in ("libportaudio.so.2", "libportaudio.so"):
            candidate = os.path.join(folder, name)
            if os.path.exists(candidate):
                return candidate
    return None


def _find_library_with_bundled_portaudio():
    """Return a find_library wrapper that also resolves bundled PortAudio.

    Returns None when no bundled copy exists (for example outside a frozen
    build), in which case the system lookup is left alone.
    """
    bundled = _bundled_portaudio()
    if bundled is None or not getattr(sys, "frozen", False):
        return None
    import ctypes.util

    original = ctypes.util.find_library

    def find_library(name):
        found = original(name)
        if found is None and name == "portaudio":
            return bundled
        return found

    return find_library


def _import_sounddevice():
    """Import sounddevice, aware of a bundled PortAudio when frozen.

    sounddevice resolves PortAudio exclusively through
    ``ctypes.util.find_library()``, which consults the system ldconfig cache
    and therefore cannot see a copy bundled next to a frozen executable.
    While frozen, extend that lookup with the bundled copy before importing,
    and restore the original afterwards.
    """
    import ctypes.util

    original_find_library = ctypes.util.find_library
    wrapper = _find_library_with_bundled_portaudio()
    if wrapper is not None:
        ctypes.util.find_library = wrapper
    try:
        import sounddevice
    finally:
        ctypes.util.find_library = original_find_library
    return sounddevice


class AudioRecorder:
    def __init__(self, filename="temp_audio.wav", samplerate=16000, channels=1):
        self.filename = filename
        self.samplerate = samplerate
        self.channels = channels
        self.is_recording = False
        self.q = queue.Queue()
        self._thread = None

    def callback(self, indata, frames, time, status):
        """This is called (from a separate thread) for each audio block."""
        if status:
            print(status, flush=True)
        self.q.put(indata.copy())

    def _record_thread(self):
        try:
            sd = _import_sounddevice()
            import soundfile as sf
            with sf.SoundFile(self.filename, mode='w', samplerate=self.samplerate,
                              channels=self.channels) as file:
                with sd.InputStream(samplerate=self.samplerate, channels=self.channels,
                                    callback=self.callback):
                    while self.is_recording:
                        file.write(self.q.get())
        except Exception as e:
            print(f"Error during recording: {e}")

    def start_recording(self):
        if self.is_recording:
            return
        self.is_recording = True
        self.q = queue.Queue() # Clear queue
        self._thread = threading.Thread(target=self._record_thread)
        self._thread.start()
        print("Recording started...")

    def stop_recording(self):
        if not self.is_recording:
            return
        self.is_recording = False
        if self._thread:
            self._thread.join()
        print(f"Recording stopped. Saved to {self.filename}")
        return self.filename
