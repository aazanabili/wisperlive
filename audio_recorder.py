import sounddevice as sd
import soundfile as sf
import threading
import queue

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
