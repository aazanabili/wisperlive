"""Microphone capture with an explicit, bounded lifecycle.

State machine:
    IDLE --start_recording()--> RECORDING --stop_recording() ok--> IDLE
        (stop returns the temp path; the CALLER owns deletion)
    RECORDING --stop_recording() failure--> IDLE
        (raises RecorderError; the recorder already deleted the temp file)

Single-owner rule for the temp file: on success the path is returned and the
caller deletes it; on any failure the recorder deletes it before raising, so
the caller never receives the path. cancel_recording() deletes internally.

Threading contract: `is_recording` is a plain bool ("user-requested recording
active") written by the main thread and read by the recorder thread; both rely
on GIL-atomic bool access only.

Audio libraries resolve lazily on first use (`_audio_libs`): importing this
module never needs PortAudio/soundfile, and frozen Linux builds get the
bundled-PortAudio lookup patch active at first import. Tests patch the `sd` /
`sf` module attributes with fakes and never trigger a real import.
"""

import logging
import os
import queue
import sys
import tempfile
import threading

logger = logging.getLogger(__name__)

QUEUE_POLL_TIMEOUT_S = 0.25
THREAD_JOIN_TIMEOUT_S = 5.0

# Lazy seam: tests patch these module attributes with fakes; production fills
# them via _audio_libs() on first recording.
sd = None
sf = None


class RecorderError(RuntimeError):
    """Recording failed; str(error) is user-safe and may be shown in the UI."""


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


def _audio_libs():
    """Resolve audio libs on first use (frozen-Linux PortAudio patch active)."""
    global sd, sf
    if sd is None:
        sd = _import_sounddevice()
    if sf is None:
        import soundfile as _soundfile
        sf = _soundfile
    return sd, sf


class AudioRecorder:
    def __init__(self, samplerate=16000, channels=1):
        self.samplerate = samplerate
        self.channels = channels
        self.is_recording = False
        self._thread = None
        self._queue = None
        self._error = None
        self._blocks_written = 0
        self._current_path = None

    def start_recording(self) -> None:
        """Allocate a unique temp file and start capturing. Raises RecorderError if a
        previous run is still shutting down or the thread/file cannot be started."""
        if self.is_recording:
            return
        if self._thread and self._thread.is_alive():
            # Stale runs can never coexist with a new run, so per-instance
            # state (queue, counters, error, path) cannot cross-talk.
            raise RecorderError("The previous recording is still shutting down. Please try again.")
        fd, path = tempfile.mkstemp(prefix="whisperlive-", suffix=".wav")  # %TEMP%, OS-unique
        os.close(fd)
        self._current_path = path
        self._queue = queue.Queue()  # assigned before spawn; never reassigned while a thread is alive
        self._error = None
        self._blocks_written = 0
        self.is_recording = True
        self._thread = threading.Thread(
            target=self._record_thread, name="whisperlive-recorder", daemon=True
        )
        self._thread.start()
        logger.info("Recording started")

    def _record_thread(self):
        try:
            # First-use resolution stays INSIDE the try: an ImportError/OSError
            # becomes self._error -> RecorderError on stop, never a raw crash.
            _audio_libs()
            with sf.SoundFile(
                self._current_path,
                mode="w",
                samplerate=self.samplerate,
                channels=self.channels,
            ) as file:
                with sd.InputStream(
                    samplerate=self.samplerate,
                    channels=self.channels,
                    callback=self._callback,
                ):
                    while self.is_recording:  # bounded consumption - never a bare q.get()
                        try:
                            block = self._queue.get(timeout=QUEUE_POLL_TIMEOUT_S)
                        except queue.Empty:
                            continue  # re-check the flag; handles a stalled stream
                        file.write(block)
                        self._blocks_written += 1
                # InputStream.__exit__ stops+closes the stream and waits for the
                # callback to return (PortAudio stop semantics) => every put has
                # landed. Drain the tail before SoundFile closes:
                while True:
                    try:
                        file.write(self._queue.get_nowait())
                    except queue.Empty:
                        break
        except Exception as error:
            self._error = error
            logger.exception("Recording thread failed")

    def _callback(self, indata, frames, time_info, status):
        """Called from a separate PortAudio thread for each audio block."""
        if status:
            logger.warning("sounddevice stream status: %s", status)  # never raise into PortAudio
        try:
            self._queue.put(indata.copy())
        except Exception:
            logger.exception("Failed to buffer an audio block")

    def stop_recording(self) -> str | None:
        """Stop capturing and return the temp-file path on success (caller deletes it).
        Returns None if not currently recording. Raises RecorderError on any failure
        (recording-thread error, join timeout, or zero audio captured) after deleting
        the temp file."""
        if not self.is_recording:
            return None
        self.is_recording = False
        self._thread.join(timeout=THREAD_JOIN_TIMEOUT_S)  # bounded - can no longer hang forever
        path = self._current_path
        if self._error is not None:
            self._cleanup(path)
            raise RecorderError("Recording failed and no audio was saved. Please try again.")
        if self._thread.is_alive():
            self._cleanup(path)
            raise RecorderError("The recording did not stop cleanly. Please try again.")
        if self._blocks_written == 0:
            self._cleanup(path)
            raise RecorderError("No audio was captured. Check your microphone and try again.")
        logger.info("Recording stopped (%d blocks)", self._blocks_written)
        return path

    def cancel_recording(self) -> None:
        """Best-effort stop for app teardown: bounded join, delete temp file, never raises."""
        try:
            self.is_recording = False
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=THREAD_JOIN_TIMEOUT_S)
            self._cleanup(self._current_path)
        except Exception:
            logger.exception("Cleanup during cancel failed")  # never raise during teardown

    @staticmethod
    def _cleanup(path):
        if not path:
            return
        try:
            os.unlink(path)
        except OSError:
            # Basename only: a full %TEMP% path leaks the Windows username.
            logger.warning("Could not delete temp file %s", os.path.basename(path))
