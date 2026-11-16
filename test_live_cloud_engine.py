"""LIVE cloud WS engine tests (plan 2.3; Phase 7 fake-transport contract).

Owner: qa-test-assurance.  A fake websocket double replaces the transport
(``websocket-client`` is absent in this dev venv — a premise this suite
asserts), so NO network and NO real socket is ever used.

Covers:
* Gemini: URL + API-key header auth, setup frame shape, verbatim 16 kHz
  audio, interim SKIP / final typed (U6), rollover reconnect WITH the
  resumption handle (no duplicate phrases), clean close;
* OpenAI Realtime: ``session.update`` shape, 16 kHz -> 24 kHz resample,
  item-matched terminal events, flush commit handshake;
* U2 consent gate at the session level: ConsentRequiredError with ZERO
  connection attempts;
* bounded backoff (<= MAX_RECONNECTS, jitter window) + D6 degraded sink;
* D9 ``ws_url_for_log`` redaction; transport-missing fail-closed (G6).
"""

import base64
import importlib.util
import json
import queue
import threading
import time
import unittest
from unittest import mock

import config_manager
from dictation_session import DictationSession
from dictation_types import build_session_config
from llm_errors import ConsentRequiredError, EngineUnavailableError
from live_cloud_engine import (BACKOFF_BASE_S, BACKOFF_MAX_S, GEMINI_MODEL,
                               GEMINI_URL, MAX_RECONNECTS, OPENAI_URL,
                               LiveCloudEngine, resample_pcm16, ws_url_for_log)

SENTINEL = object()
SPEECH = b"\x02\x00" * 320


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class FakeSocket:
    """Scripted socket double: recorded sends, queued inbound frames."""

    def __init__(self, label="s"):
        self.label = label
        self.sent = []
        self.closed = False
        self._q = queue.Queue()

    def send(self, payload):
        if self.closed:
            raise RuntimeError("socket closed")
        self.sent.append(payload)

    def recv(self):
        item = self._q.get()
        if item is SENTINEL:
            raise ConnectionResetError("peer closed")
        return item

    def push(self, message):
        self._q.put(json.dumps(message))

    def close(self):
        if not self.closed:
            self.closed = True
            self._q.put(SENTINEL)


def make_factory(sockets, calls, sleeps=None):
    def factory(url, headers, timeout):
        calls.append({"url": url, "headers": headers, "timeout": timeout})
        if not sockets:
            raise ConnectionRefusedError("scripted refusal")
        return sockets.pop(0)
    return factory


def live_config(**extra):
    base = {"dictation_mode": "live", "cleanup_mode": "off", "provider": "openai",
            "provider_base_url": "https://api.openai.com/v1", "model": "gpt",
            "transcription_model": "", "target_language": "English",
            "chunked_engine": "", "polish_mode": "insert", "model_size": "auto",
            "live_engine": "local", "gemini_consent": False,
            "gemini_paid_key_confirmed": False, "cleanup_model": "",
            "model_store_dir": "", "local_models": {}, "api_keys_protected": {},
            "quota_limits": {}, "first_run_guidance_seen": False}
    base.update(extra)
    return base


def make_cfg(**extra):
    api_keys = extra.pop("api_keys", {})
    return build_session_config(live_config(**extra), session_id="lc",
                                started_at_epoch=int(time.time()),
                                api_keys=api_keys)


class UrlRedactionTests(unittest.TestCase):
    def test_ws_url_for_log_strips_secrets(self):
        cases = [
            ("wss://host/ws?key=TOPSECRET&t=1", "wss://host/ws"),
            ("wss://user:pass@host/ws", "wss://host/ws"),
            ("wss://host:8443/ws?frag#x", "wss://host:8443/ws"),
            ("wss://api.openai.com/v1/realtime?intent=transcription",
             "wss://api.openai.com/v1/realtime"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                got = ws_url_for_log(raw)
                self.assertEqual(got, expected)
                for secret in ("TOPSECRET", "pass", "intent", "t=1", "#", "?"):
                    self.assertNotIn(secret, got)

    def test_engine_urls_carry_no_credentials(self):
        for url in (GEMINI_URL, OPENAI_URL):
            self.assertEqual(ws_url_for_log(url).find("?"), -1)   # no query


class ResampleTests(unittest.TestCase):
    def test_16k_to_24k_ratio_and_identity(self):
        self.assertEqual(live_cloud_rate(), (16000, 24000))
        payload = b"\x01\x00" * 320                        # 320 samples = 20 ms
        out = resample_pcm16(payload, 16000, 24000)
        self.assertEqual(len(out), 480 * 2)                # 1.5x exact (480 samples)
        self.assertEqual(resample_pcm16(payload, 16000, 16000), payload)
        self.assertEqual(resample_pcm16(b"\x01", 16000, 24000), b"\x01")  # too short

    def test_resample_preserves_a_constant_signal_level(self):
        constant = b"\x40\x10" * 400                       # constant s16 value
        out = resample_pcm16(constant, 16000, 24000)
        samples = [out[i] | (out[i + 1] << 8) for i in range(0, len(out), 2)]
        target = 0x1040
        for sample in samples:
            self.assertLessEqual(abs(sample - target), 2)   # linear interp stays level


def live_cloud_rate():
    import live_cloud_engine
    return (live_cloud_engine.CAPTURE_RATE, live_cloud_engine.OPENAI_RATE)


class GeminiLifecycleTests(unittest.TestCase):
    def test_setup_auth_audio_interim_skip_final_and_close(self):
        sock = FakeSocket("g1")
        calls, sleeps = [], []
        engine = LiveCloudEngine("gemini-live",
                                 ws_factory=make_factory([sock], calls),
                                 sleep=sleeps.append)
        got = []
        try:
            engine.start(make_cfg(live_engine="gemini-live", gemini_consent=True,
                                  api_keys={"google-gemini": "g-secret"}),
                         got.append)
            self.assertEqual(calls[0]["url"], GEMINI_URL)
            self.assertEqual(calls[0]["headers"], ["x-goog-api-key: g-secret"])
            self.assertEqual(calls[0]["timeout"], 8.0)      # bounded connect (fail-closed)
            setup = json.loads(sock.sent[0])["setup"]
            self.assertEqual(setup["model"], GEMINI_MODEL)
            self.assertIn("inputAudioTranscription", setup)
            self.assertNotIn("sessionResumption", setup)    # no handle on FIRST connect

            engine.push_audio(SPEECH)
            frame = json.loads(sock.sent[1])["realtimeInput"]["audio"]
            self.assertEqual(frame["mimeType"], "audio/pcm;rate=16000")
            self.assertEqual(base64.b64decode(frame["data"]), SPEECH)  # verbatim

            # U6: interim events NEVER emit; the final one types exactly once
            sock.push({"serverContent": {"interimInputTranscription":
                                         {"text": "hel-lo wor"}}})
            sock.push({"serverContent": {"inputTranscription":
                                         {"text": "hello world"}}})
            self.assertTrue(wait_until(lambda: len(got) == 1),
                            "final never typed")
            self.assertEqual(got[0].text, "hello world")
            self.assertTrue(got[0].is_final)
            self.assertNotIn("hel-lo", [e.text for e in got])
            thread = engine._thread
            engine.close()
            self.assertTrue(sock.closed)
            self.assertFalse(thread.is_alive())
        finally:
            engine.close()

    def test_rollover_reconnects_with_the_resumption_handle(self):
        s1, s2 = FakeSocket("g1"), FakeSocket("g2")
        calls, sleeps = [], []
        engine = LiveCloudEngine("gemini-live",
                                 ws_factory=make_factory([s1, s2], calls),
                                 sleep=sleeps.append)
        got = []
        try:
            engine.start(make_cfg(live_engine="gemini-live", gemini_consent=True,
                                  api_keys={"google-gemini": "g-secret"}),
                         got.append)
            s1.push({"sessionResumptionUpdate": {"newHandle": "h-1",
                                                 "resumable": True}})
            s1.push({"serverContent": {"inputTranscription": {"text": "one"}}})
            self.assertTrue(wait_until(lambda: len(got) == 1))
            s1.push({"goAway": {"timeLeft": "3s"}})        # vendor disconnect soon
            s1.close()                                     # recv dies -> reconnect
            self.assertTrue(wait_until(lambda: len(calls) == 2, 5.0),
                            "rollover reconnect never happened")
            self.assertTrue(sleeps, "backoff must run before the reconnect")
            setup2 = json.loads(s2.sent[0])["setup"]
            self.assertEqual(setup2.get("sessionResumption", {}).get("handle"),
                             "h-1", "resumed setup must carry the handle")
            s2.push({"serverContent": {"inputTranscription": {"text": "two"}}})
            self.assertTrue(wait_until(lambda: len(got) == 2))
            self.assertEqual([e.text for e in got], ["one", "two"])  # no dupes
            engine.close()
            self.assertTrue(s2.closed)
        finally:
            engine.close()


class OpenAIRealtimeTests(unittest.TestCase):
    def test_session_update_shape_and_resampled_audio(self):
        sock = FakeSocket("oa")
        calls, sleeps = [], []
        engine = LiveCloudEngine("openai-realtime",
                                 ws_factory=make_factory([sock], calls),
                                 sleep=sleeps.append)
        got = []
        try:
            engine.start(make_cfg(live_engine="openai-realtime",
                                  api_keys={"openai": "oa-secret"}), got.append)
            self.assertEqual(calls[0]["url"], OPENAI_URL)
            self.assertEqual(calls[0]["headers"],
                             ["Authorization: Bearer oa-secret"])
            setup = json.loads(sock.sent[0])
            session = setup["session"]
            self.assertEqual(setup["type"], "session.update")
            self.assertEqual(session["type"], "transcription")
            self.assertEqual(session["audio"]["input"]["format"],
                             {"type": "audio/pcm", "rate": 24000})
            self.assertEqual(session["audio"]["input"]["transcription"]["model"],
                             "gpt-live-transcribe")
            self.assertIsNone(session["audio"]["input"]["turn_detection"])

            engine.push_audio(SPEECH)                      # 320 samples @16 kHz
            frame = json.loads(sock.sent[1])
            self.assertEqual(frame["type"], "input_audio_buffer.append")
            self.assertEqual(len(base64.b64decode(frame["audio"])), 960)  # 480@24k
        finally:
            engine.close()

    def test_delta_never_types_completed_types_commit_is_tracked(self):
        sock = FakeSocket("oa")
        calls, sleeps = [], []
        engine = LiveCloudEngine("openai-realtime",
                                 ws_factory=make_factory([sock], calls),
                                 sleep=sleeps.append)
        got = []
        try:
            engine.start(make_cfg(live_engine="openai-realtime",
                                  api_keys={"openai": "k"}), got.append)
            sock.push({"type": "conversation.item.input_audio_transcription.delta",
                       "delta": "Hi"})
            sock.push({"type": "input_audio_buffer.committed",
                       "item_id": "item_1"})
            sock.push({"type": "conversation.item.input_audio_transcription.completed",
                       "item_id": "item_1", "transcript": "Hi there"})
            self.assertTrue(wait_until(lambda: len(got) == 1),
                            "completed never typed")
            self.assertEqual(got[0].text, "Hi there")
            self.assertTrue(got[0].is_final)
            self.assertNotIn("Hi ", [e.text for e in got])  # delta never typed alone
            self.assertEqual(len(got), 1)
            # failed terminal: turn tracked, but NO text emitted (D11 too)
            sock.push({"type": "conversation.item.input_audio_transcription.failed",
                       "item_id": "item_2"})
            time.sleep(0.05)
            self.assertEqual(len(got), 1)
        finally:
            engine.close()

    def test_flush_commits_the_turn_and_waits_for_the_item_matched_event(self):
        sock = FakeSocket("oa")
        calls, sleeps = [], []
        engine = LiveCloudEngine("openai-realtime",
                                 ws_factory=make_factory([sock], calls),
                                 sleep=sleeps.append)
        got = []
        try:
            engine.start(make_cfg(live_engine="openai-realtime",
                                  api_keys={"openai": "k"}), got.append)

            def deliver():
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if any(json.loads(s).get("type") == "input_audio_buffer.commit"
                           for s in sock.sent):
                        break
                    time.sleep(0.01)
                sock.push({"type": "input_audio_buffer.committed",
                           "item_id": "item_2"})
                sock.push({"type": "conversation.item.input_audio_transcription.completed",
                           "item_id": "item_2", "transcript": "second turn"})
            threading.Thread(target=deliver, daemon=True).start()
            engine.push_audio(SPEECH)                      # audio since last commit
            engine.flush()                                 # blocks until terminal
            self.assertTrue(any(json.loads(s).get("type") == "input_audio_buffer.commit"
                                for s in sock.sent),
                            "flush must commit the VAD turn")
            self.assertEqual([e.text for e in got], ["second turn"])
            thread = engine._thread
            engine.close()
            self.assertFalse(thread.is_alive())
        finally:
            engine.close()


class BoundedBackoffTests(unittest.TestCase):
    def test_reconnect_budget_is_bounded_and_degrades_loudly(self):
        sock = FakeSocket("b")
        calls, sleeps, notices = [], [], []
        engine = LiveCloudEngine("openai-realtime",
                                 ws_factory=make_factory([sock], calls),
                                 sleep=sleeps.append, clock=lambda: 1000.0)
        engine.degraded_sink = lambda kind, detail, **kw: notices.append(
            (kind, kw.get("engine_id")))
        try:
            engine.start(make_cfg(live_engine="openai-realtime",
                                  api_keys={"openai": "k"}), lambda _e: None)
            sock.close()                                   # recv dies -> reconnects
            self.assertTrue(wait_until(lambda: bool(notices), 5.0),
                            "exhaustion must degrade loudly (D6)")
            self.assertEqual(len(sleeps), MAX_RECONNECTS)   # bounded: never infinite
            for attempt, delay in enumerate(sleeps):
                floor = BACKOFF_BASE_S * (2 ** attempt) * 0.5     # exp base + jitter
                self.assertGreaterEqual(delay, floor - 1e-9)
                self.assertLessEqual(delay, BACKOFF_MAX_S)
            self.assertEqual(notices, [("unit_error", "openai-realtime")])
            # a DEAD engine swallows audio WITHOUT new connection attempts
            engine.push_audio(SPEECH)
            self.assertEqual(len(calls), 1 + MAX_RECONNECTS)
        finally:
            engine.close()

    def test_socket_timeout_is_not_a_disconnect(self):
        class _Timeoutish(FakeSocket):
            def recv(self):                                # idle, not dead
                raise type("WebSocketTimeoutException", (Exception,), {})()
        sock = _Timeoutish("idle")
        calls, sleeps = [], []
        engine = LiveCloudEngine("openai-realtime",
                                 ws_factory=make_factory([sock], calls),
                                 sleep=sleeps.append)
        try:
            engine.start(make_cfg(live_engine="openai-realtime",
                                  api_keys={"openai": "k"}), lambda _e: None)
            deadline = time.monotonic() + 1.0              # give the reader a tick
            while time.monotonic() < deadline and len(calls) == 1:
                time.sleep(0.01)
            self.assertEqual(len(calls), 1)                # NO reconnect on idle
            self.assertEqual(sleeps, [])
        finally:
            engine.close()


class FakeVad:
    def feed(self, _block):
        return []

    def flush(self):
        return None


class RecordingTyper:
    def __init__(self):
        self.submitted, self.typed_any, self.stopped = [], True, 0
        self.aborted = False

    def start(self):
        pass

    def submit(self, emission):
        self.submitted.append(emission.emission_seq)
        return True

    def stop(self):
        self.stopped += 1

    def abort(self):
        self.aborted = True


class Recorder:
    def __init__(self):
        self.sink, self.starts, self.stops = None, 0, 0

    def start_block_stream(self, sink):
        self.sink, self.starts = sink, self.starts + 1

    def stop_block_stream(self):
        self.stops += 1
        return None


class App:
    def __init__(self, config):
        self.config = dict(config)
        self.recorder = Recorder()
        self.indicator = type("I", (), {"show": lambda *a: None,
                                        "hide": lambda *a: None})()
        self.is_exiting = False
        self.is_processing = False
        self.statuses, self.beeps, self.finished = [], [], 0

    def set_status(self, message, state):
        self.statuses.append((message, state))

    def beep(self, freq, dur):
        self.beeps.append((freq, dur))

    def call_in_ui(self, callback):
        if not self.is_exiting:
            callback()

    def _on_processing_finished(self):
        self.finished += 1
        self.is_processing = False


class ConsentGateTests(unittest.TestCase):
    """U2: consent missing -> ConsentRequiredError with ZERO connects (G4/G6)."""

    def test_gemini_without_consent_never_opens_a_connection(self):
        connections = []

        def engine_factory(cfg, _store):
            return LiveCloudEngine(cfg.live_engine,
                                   ws_factory=make_factory(
                                       [FakeSocket("c")], connections),
                                   sleep=lambda _s: None)
        app = App(live_config(live_engine="gemini-live", gemini_consent=False,
                              gemini_paid_key_confirmed=False))
        session = DictationSession(app, engine_factory=engine_factory,
                                   vad_factory=lambda **_k: FakeVad(),
                                   typer_factory=lambda _a, _s: RecordingTyper())
        with self.assertRaises(ConsentRequiredError) as ctx:
            session.preflight(app.config)
        self.assertIn("data notice", str(ctx.exception))
        self.assertEqual(ctx.exception.engine_id, "gemini-live")
        self.assertTrue(session.maybe_start())             # rejected WITH status+beep
        self.assertEqual(connections, [])                  # ZERO connects
        self.assertEqual(app.recorder.starts, 0)
        self.assertEqual(session.state.value, "idle")
        self.assertTrue(any(state == "error" for _m, state in app.statuses))

    def test_paid_key_flag_opens_the_consent_gate_and_connects(self):
        connections = []

        def engine_factory(cfg, _store):
            return LiveCloudEngine(cfg.live_engine,
                                   ws_factory=make_factory(
                                       [FakeSocket("c")], connections),
                                   sleep=lambda _s: None)
        app = App(live_config(live_engine="gemini-live", gemini_consent=False,
                              gemini_paid_key_confirmed=True))
        config_manager.set_vendor_key(app.config, "google-gemini", "gk-paid")
        session = DictationSession(app, engine_factory=engine_factory,
                                   vad_factory=lambda **_k: FakeVad(),
                                   typer_factory=lambda _a, _s: RecordingTyper())
        with mock.patch("importlib.util.find_spec", return_value=object()):
            self.assertTrue(session.maybe_start())
        self.assertEqual(session.state.value, "recording")
        self.assertEqual(len(connections), 1)
        self.assertEqual(connections[0]["headers"],
                         ["x-goog-api-key: gk-paid"])
        session.abort_all("user_cancel")
        self.assertEqual(session.state.value, "idle")

    def test_quit_mid_stream_closes_the_socket_and_stops_emissions(self):
        sock = FakeSocket("q")
        connections = []
        app = App(live_config(live_engine="gemini-live", gemini_consent=True))
        config_manager.set_vendor_key(app.config, "google-gemini", "qk")
        typer = RecordingTyper()

        def engine_factory(cfg, _store):
            return LiveCloudEngine(cfg.live_engine,
                                   ws_factory=make_factory([sock], connections),
                                   sleep=lambda _s: None)
        session = DictationSession(app, engine_factory=engine_factory,
                                   vad_factory=lambda **_k: FakeVad(),
                                   typer_factory=lambda _a, _s: typer)
        with mock.patch("importlib.util.find_spec", return_value=object()):
            self.assertTrue(session.maybe_start())
        for _ in range(4):
            app.recorder.sink(SPEECH)
        self.assertTrue(wait_until(lambda: any("realtimeInput" in sent
                                               for sent in sock.sent)),
                        "live feed never pushed audio up")
        sock.push({"serverContent": {"inputTranscription": {"text": "before quit"}}})
        self.assertTrue(wait_until(lambda: len(session.emissions) == 1))
        self.assertEqual(typer.submitted, [0])
        # M2 quit: socket closes, zero emissions afterwards
        app.is_exiting = True
        session.abort_all("quit")
        self.assertTrue(sock.closed, "abort must close the socket")
        self.assertEqual(session.state.value, "idle")
        self.assertTrue(typer.aborted)
        self.assertEqual(session.emissions, ())
        self.assertEqual(typer.submitted, [0])             # no typing after abort
        time.sleep(0.2)
        self.assertEqual(session.emissions, ())            # late events stay silent


class TransportMissingTests(unittest.TestCase):
    def test_premise_websocket_client_is_absent_here(self):
        self.assertIsNone(importlib.util.find_spec("websocket"),
                          "premise: this dev venv ships NO websocket-client")

    def test_engine_fails_closed_before_any_socket_or_thread(self):
        engine = LiveCloudEngine("openai-realtime")
        try:
            with self.assertRaises(EngineUnavailableError) as ctx:
                engine.start(make_cfg(live_engine="openai-realtime",
                                      api_keys={"openai": "k"}),
                             lambda _e: None)
            self.assertEqual(ctx.exception.reason,
                             "websocket-client not installed")
            self.assertIn("requirements.txt", str(ctx.exception))
        finally:
            engine.close()
        self.assertIsNone(engine._thread)
        self.assertIsNone(engine._ws)

    def test_session_preflight_reports_transport_missing_before_recording(self):
        app = App(live_config(live_engine="openai-realtime"))
        config_manager.set_vendor_key(app.config, "openai", "k")
        session = DictationSession(app)
        with self.assertRaises(EngineUnavailableError) as ctx:
            session.preflight(app.config)
        self.assertEqual(ctx.exception.reason,
                         "websocket-client not installed")
        self.assertTrue(session.maybe_start())             # never crashes (G6)
        self.assertEqual(app.recorder.starts, 0)
        self.assertEqual(session.state.value, "idle")


if __name__ == "__main__":
    unittest.main()
