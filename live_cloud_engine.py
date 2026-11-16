"""Live cloud WS engines (A6 s3d Round-2): Gemini Live + OpenAI Realtime — one shared
connection/rollover/backoff skeleton behind ``LiveStreamEngine``; only the frame codecs differ.
Transport = ``websocket-client`` 1.9.2 (R-U4) LAZY-imported (absent in this dev venv: missing
package -> ``EngineUnavailableError`` before any connection; tests inject seams instead).  Shapes
re-VERIFIED 2026-11-16 vs ai.google.dev/api/live + developers.openai.com guides (UNVERIFIED noted
in code); consent/key/vocabulary gates live in ``dictation_session.preflight`` BEFORE this factory.
D9 ``ws_url_for_log`` strips userinfo/query/fragment before any log; D11 logs = event types/codes/
durations only (never transcript/keys); D10 audio stays in memory (base64); D6 reconnects bounded
(<= 3/session, backoff+jitter, exhaustion degrades loudly — never silently).
"""

import base64
import json
import logging
import random
import threading
import time
import urllib.parse
from array import array
from typing import Callable, Optional

from dictation_types import EmissionOut, SessionConfig
from llm_errors import EngineUnavailableError, LLMNetworkError

logger = logging.getLogger(__name__)

VENDOR_OF = {"gemini-live": "google-gemini", "openai-realtime": "openai"}
GEMINI_URL = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
OPENAI_URL = "wss://api.openai.com/v1/realtime?intent=transcription"
GEMINI_MODEL = "models/gemini-3.5-transcribe-live"    # annex: dedicated streaming-STT model
OPENAI_MODEL = "gpt-live-transcribe"                 # docs VERIFIED
CAPTURE_RATE = 16000                                 # pcm_stream capture rate
OPENAI_RATE = 24000                                  # docs VERIFIED transcription PCM rate
MAX_RECONNECTS = 3                                   # annex: max 3 reconnects per session
BACKOFF_BASE_S = 0.5                                 # annex: exponential backoff + jitter
BACKOFF_MAX_S = 2.0                                  # every sleep <= this (bounded)
FLUSH_WAIT_S = 1.5                                   # bounded vendor-finality wait (s3c)
CONNECT_TIMEOUT_S = 8.0                              # bounded connect: fail-closed pre-record
CLOSE_JOIN_S = 5.0                                   # bounded reader join (A6 s2.5)
WS_TRANSPORT_MESSAGE = ("Live cloud dictation needs the websocket-client package. Install it with: "
           "pip install -r requirements.txt, then restart the app.")


def ws_url_for_log(url: str) -> str:
    """D9: scheme + host + path ONLY — never log userinfo/query/fragment/keys."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return f"{parts.scheme}://{host}{parts.path}"
    except ValueError:
        return "<redacted url>"


def _load_websocket(engine_id: str):
    """Lazy transport (U1): absent package -> typed error BEFORE any connection."""
    try:
        import websocket
    except ImportError as error:
        raise EngineUnavailableError(WS_TRANSPORT_MESSAGE, engine_id=engine_id, reason="websocket-client not installed") from error
    return websocket


def resample_pcm16(pcm16: bytes, src_rate: int, dst_rate: int) -> bytes:
    """s16le linear resample (stdlib): OpenAI's verified rate is 24 kHz vs 16 kHz capture."""
    if src_rate == dst_rate or len(pcm16) < 4:
        return bytes(pcm16)
    samples = array("h")
    samples.frombytes(bytes(pcm16))
    count = len(samples) * dst_rate // src_rate           # exact ratio (320 -> 480 at 1.5x)
    out = array("h")
    for index in range(count):
        position = index * (len(samples) - 1) / max(1, count - 1)
        left = min(int(position), len(samples) - 2)
        out.append(int(samples[left] + (samples[left + 1] - samples[left]) * (position - left)))
    return out.tobytes()


class LiveCloudEngine:
    """One WS session per recording: bounded reconnect with fresh setup per attempt
    (B7-style), Gemini resumption rollover, U6 confirmed-only emission."""

    def __init__(self, engine_id: str, *, ws_factory: Optional[Callable] = None,
                 clock=time.monotonic, sleep=time.sleep):
        self.engine_id = engine_id
        self._gemini = engine_id == "gemini-live"
        self._factory, self._clock, self._sleep = ws_factory, clock, sleep
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._emit: Optional[Callable[[EmissionOut], None]] = None
        self.degraded_sink: Optional[Callable[..., None]] = None   # D6: session-bound notice sink
        self._ws = None
        self._key = self._resumption = self._final_item = self._last_item = ""
        self._running = self._dead = False
        self._reconnects = self._turn_closed = self._commits = self._audio_since_commit = 0
        self._interim_at = self._final_at = 0.0        # Gemini in-flight signal
        self._thread: Optional[threading.Thread] = None

    # ---------------- LiveStreamEngine port ---------------------------------
    def start(self, config: SessionConfig, emit: Callable[[EmissionOut], None]) -> None:
        """U1: consent/key/vocabulary were preflighted BEFORE recording; connect still fails closed."""
        if self._running:
            return
        # VENDOR_OF.get: unknown engine ids coerce (N1) — config is choice-coerced upstream.
        self._emit, self._key = emit, (config.api_keys.get(VENDOR_OF.get(self.engine_id, "openai")) or "").strip()
        try:
            self._open_socket()
        except EngineUnavailableError:
            raise                                   # typed transport guard passes through
        except Exception as error:
            raise LLMNetworkError(f"Could not connect to {self.engine_id} ({type(error).__name__}). Check your network and API key, then try again.") from error
        with self._lock:
            self._running = True
        self._thread = threading.Thread(target=self._reader_loop, name="cloud-live-reader", daemon=True)
        self._thread.start()

    def push_audio(self, pcm16: bytes) -> None:
        """One capture block up (D10): Gemini verbatim 16 kHz; OpenAI resampled to 24 kHz."""
        if not pcm16:
            return
        if self._gemini:
            payload = {"realtimeInput": {"audio": {"data": base64.b64encode(pcm16).decode(), "mimeType": f"audio/pcm;rate={CAPTURE_RATE}"}}}
        else:
            pcm = resample_pcm16(pcm16, CAPTURE_RATE, OPENAI_RATE)
            with self._lock:
                self._audio_since_commit += len(pcm)
            payload = {"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()}
        if not self._send(json.dumps(payload)):
            with self._lock:                       # half-dead: drop the socket -> reader reconnects
                sock, self._ws = self._ws, None
            self._close_sock(sock)

    def flush(self) -> None:
        """A6 s3c confirm-all, bounded: OpenAI commits the client-VAD turn and waits for THAT item's
        terminal event; Gemini best-effort ends the stream, waiting only while mid-flight (timeout logged)."""
        with self._lock:
            in_flight = self._interim_at > self._final_at if self._gemini else self._audio_since_commit > 0
            if not self._running or self._dead or self._ws is None or not in_flight:
                return
            target, base = self._turn_closed + 1, self._commits
        if self._gemini:
            ok = self._send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))
        else:
            ok = self._send(json.dumps({"type": "input_audio_buffer.commit"}))
            with self._lock:
                self._audio_since_commit = 0
        if not ok:
            return                                 # socket died: reader owns recovery
        deadline = self._clock() + FLUSH_WAIT_S
        with self._lock:
            while self._running and not self._dead:
                if self._gemini:
                    done = self._turn_closed >= target
                else:
                    # Docs: match OUR terminal event by item_id — completion order
                    # across turns is NOT guaranteed.
                    done = bool(self._commits > base and self._last_item
                                and self._last_item == self._final_item)
                if done:
                    return
                remaining = deadline - self._clock()
                if remaining <= 0:
                    logger.info("cloud-live flush timed out engine=%s", self.engine_id)
                    return
                self._cond.wait(timeout=remaining)

    def close(self) -> None:
        """Stop the reader (bounded join) + close the socket — M2: abort closes everything,
        no late event can emit. Idempotent."""
        with self._lock:
            self._running = False
            sock, self._ws = self._ws, None
            self._cond.notify_all()
        self._close_sock(sock)
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=CLOSE_JOIN_S)
        self._emit = None

    # ---------------- connection lifecycle ----------------------------------
    def _open_socket(self) -> None:
        """Fresh connect + first frame; the default factory lazy-loads the transport."""
        factory = self._factory
        if factory is None:
            def factory(url, headers, timeout):     # noqa: F811 - default seam
                return _load_websocket(self.engine_id).create_connection(
                    url, header=headers, timeout=timeout)
        auth = f"x-goog-api-key: {self._key}" if self._gemini else f"Authorization: Bearer {self._key}"
        sock = factory(GEMINI_URL if self._gemini else OPENAI_URL, [auth], CONNECT_TIMEOUT_S)   # D9: key in header, never URL
        try:
            sock.send(self._setup_frame())          # first frame before anything else
        except Exception:
            self._close_sock(sock)
            raise
        with self._lock:
            previous, self._ws = self._ws, sock
        self._close_sock(previous)

    def _setup_frame(self) -> str:
        if self._gemini:
            setup = {"model": GEMINI_MODEL, "inputAudioTranscription": {"wordTimestamp": False}}
            if self._resumption:                   # rollover continuity (VERIFIED handle)
                setup["sessionResumption"] = {"handle": self._resumption}
            return json.dumps({"setup": setup})
        return json.dumps({"type": "session.update", "session": {"type": "transcription",
            "audio": {"input": {"format": {"type": "audio/pcm", "rate": OPENAI_RATE},
                                "transcription": {"model": OPENAI_MODEL}, "turn_detection": None}}}})

    @staticmethod
    def _close_sock(sock) -> None:
        if sock is not None:
            try:
                sock.close()
            except Exception:                       # teardown never raises
                logger.debug("cloud-live socket close failed", exc_info=True)

    def _send(self, frame: str) -> bool:
        """One frame out under the lock (serialized vs socket swaps); False = gone."""
        with self._lock:
            sock = self._ws
            if sock is None or self._dead:
                return False
            try:
                sock.send(frame)
                return True
            except Exception as error:
                logger.warning("cloud-live send failed (%s)", type(error).__name__)
                return False

    # ---------------- reader + bounded reconnect ----------------------------
    def _reader_loop(self) -> None:
        """Single reader + inbound mapping (U6 confirmed-only): dead socket -> ANNEX-bounded reconnect."""
        while True:
            with self._lock:
                if not self._running:
                    return
                sock = self._ws
            if sock is None:
                if not self._reconnect():
                    return
                continue
            try:
                raw = sock.recv()
            except Exception as error:
                if type(error).__name__ == "WebSocketTimeoutException":
                    continue                        # idle is NOT a disconnect (hazard note)
                logger.info("cloud-live socket closed (%s)", type(error).__name__)
                with self._lock:
                    sock, self._ws = self._ws, None
                self._close_sock(sock)
                continue
            try:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    continue
                if "error" in message or message.get("type") == "error":
                    error = message.get("error")
                    logger.warning("cloud-live error event code=%s", error.get("code") if isinstance(error, dict) else None)   # code only (D11)
                    continue
                (self._gemini_event if self._gemini else self._openai_event)(message)
            except Exception:
                logger.exception("cloud-live event mapping failed")   # types only (D11)

    def _reconnect(self) -> bool:
        """Annex: backoff + jitter, MAX_RECONNECTS total/session, fresh setup per attempt (Gemini
        carries the resumption handle -> continuity); exhaustion degrades loudly (D6) — never infinite."""
        with self._lock:
            if not self._running or self._dead:
                return False
            if self._reconnects >= MAX_RECONNECTS:
                self._dead, sink, self.degraded_sink = True, self.degraded_sink, None
                self._cond.notify_all()
            else:
                self._reconnects += 1
                attempt, sink = self._reconnects, None
        if self._dead:                          # just exhausted (only this path sets it)
            logger.warning("cloud-live reconnect budget exhausted engine=%s", self.engine_id)
            if sink is not None:
                try:
                    sink("unit_error", "Live connection was lost — transcription stopped. "
                         "Your recording will finish at stop.", engine_id=self.engine_id)
                except Exception:
                    logger.debug("degraded sink failed", exc_info=True)
            return False
        delay = min(BACKOFF_MAX_S, BACKOFF_BASE_S * (2 ** (attempt - 1)))
        self._sleep(min(delay * random.uniform(0.5, 1.5), BACKOFF_MAX_S))   # jitter, bounded
        if not self._running or self._dead:
            return False
        try:
            self._open_socket()
        except Exception as error:
            logger.warning("cloud-live reconnect failed (%s)", type(error).__name__)
        return True

    # ---------------- inbound mapping (U6 confirmed-only) -------------------
    def _gemini_event(self, message: dict) -> None:
        content = message.get("serverContent")
        if isinstance(content, dict):
            final, interim = content.get("inputTranscription"), content.get("interimInputTranscription")
            if isinstance(final, dict):
                # VERIFIED final stream: finalized text per message (order vs other server msgs is vendor-UNGUARANTEED).
                text = str(final.get("text") or "").strip()
                with self._lock:
                    self._turn_closed, self._final_at = self._turn_closed + 1, self._clock()
                    self._cond.notify_all()
                self._emit_final(text)              # U6: interim is never emitted
            elif isinstance(interim, dict):
                with self._lock:
                    self._interim_at = self._clock()
            return
        update = message.get("sessionResumptionUpdate")
        if isinstance(update, dict):
            handle = update.get("newHandle")
            if update.get("resumable") and isinstance(handle, str) and handle:
                with self._lock:
                    self._resumption = handle       # VERIFIED: rollover continuity
            return
        if "goAway" in message:
            # VERIFIED: server disconnects soon -> recv failure drives the bounded reconnect (<= MAX_RECONNECTS).
            logger.info("cloud-live goAway received engine=%s", self.engine_id)

    def _openai_event(self, message: dict) -> None:
        kind = message.get("type")
        if kind in ("conversation.item.input_audio_transcription.completed",
                    "conversation.item.input_audio_transcription.failed"):
            with self._lock:
                self._turn_closed, self._final_item = self._turn_closed + 1, str(message.get("item_id") or "")
                self._cond.notify_all()
            if kind.endswith(".completed"):
                self._emit_final(str(message.get("transcript") or "").strip())
            else:
                logger.warning("cloud-live transcription failed")     # no transcript (D11)
        elif kind == "input_audio_buffer.committed":
            with self._lock:
                self._commits, self._last_item = self._commits + 1, str(message.get("item_id") or "")
                self._cond.notify_all()
        # deltas + every other event: U6 — deltas revise earlier text, NEVER typed

    def _emit_final(self, text: str) -> None:
        """U6: cloud-live types ONLY finalized text (``is_final=True``)."""
        emit = self._emit
        if emit is None or not text:
            return
        try:
            emit(EmissionOut(text=text, segment_seq=-1, kind="phrase", is_final=True))
        except Exception:                           # defect never kills the reader
            logger.warning("cloud-live emit callback failed", exc_info=True)
