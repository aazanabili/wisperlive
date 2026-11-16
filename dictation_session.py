"""Application orchestrator for the three dictation modes (D5/D6/D10/M2).

WHY: one object owns the whole session lifecycle — state machine (via pure
``session_state``), bounded queues, segmenter/worker/emitter threads, the
immutable ``SessionConfig`` snapshot (D10, built here on the UI thread), the
D6 quota admission hooks (NO silent failover — exhaustion becomes a Degraded
NOTICE + unit disposition, the offering dialog is Phase 4), and the M2
``is_exiting`` abort contract (abort ALL workers/buffers and suppress ALL
injection).

Layering (A6 map): application tier — imports the pure core + adapters, NEVER
``main``/tkinter (the app object is DUCK-TYPED, so the dependency arrow stays
main -> session).  UI calls (status/beep/indicator/is_exiting) arrive as app
callbacks; everything else runs on session-owned daemon threads.
"""

import logging
import queue
import threading
import time
import uuid
from dataclasses import replace

from auto_typer import PasteError, paste_text
import config_manager
from audio_recorder import RecorderError
from chunked_engine import ChunkedEngine, min_segment_seconds, quota_engine_id
from dictation_types import (Segment, SegmentIn, SessionConfig,
                             build_session_config, next_seq, resolve_chunked_engine,
                             set_status)
from llm_errors import (ConsentRequiredError, DictationError, EngineUnavailableError,
                        LLMConfigError, LLMError, LLMQuotaError,
                        ModelNotDownloadedError)
from live_cloud_engine import WS_TRANSPORT_MESSAGE, LiveCloudEngine
from live_local_engine import LiveLocalEngine, live_model_id
from local_engine import LocalEngine, resolve_model_id
import providers
from pcm_stream import BlockQueue, END_OF_STREAM, PcmRing
import polish_engine
import session_state as machine
from quota_ledger import (EMPTY_LEDGER, GROQ_BILL_FLOOR_S, WINDOW_DAY, billable_units,
                          check_admission, charge, default_engine_quota, record_vendor_usage)
from typer_actor import build_typer
from vad_segmenter import RawSpan, VadSegmenter

logger = logging.getLogger(__name__)

DICTATION_MODES = ("local", "chunked", "live")
SEGMENT_QUEUE_MAX = 3                 # D5: ~3 merged units (~60 s) bounded backlog
WORKER_COUNT = {"local": 1, "chunked": 2}      # A6 s3b: local serial, chunked x2
DRAIN_TIMEOUT_S = 240.0               # A6: ~2x engine timeout (120 s) — a slow
                                      # in-flight unit DRAINS, it is never cut at stop
THREAD_JOIN_TIMEOUT_S = 5.0           # bounded joins (A6 s2.2/s2.5)
_PUT_TIMEOUT_S = 5.0
_SEGMENT_EOS = object()               # segment_q end-of-stream marker (workers only)
_MAX_MERGE_BYTES = 30 * 16000 * 2     # A6 s2.2: merge cap 30 s per queued unit

_LOCAL_EXTRAS_MESSAGE = (
    "Local dictation needs the optional ML extras (faster-whisper, onnxruntime). "
    "Install them with: pip install -r requirements-ml.txt, then restart the app."
)
_LIVE_SOON_MESSAGE = "This live engine arrives in a later update. Choose the Local live engine."


def _quota_units_for(engine_id, segment: SegmentIn) -> int:
    """D6 charge input: Groq bills max(10 s, ceil) (V1), local tracks audio
    seconds (observability), chat/cleanup engines bill ONE request per dispatch."""
    if engine_id == "groq-whisper":
        return billable_units(segment.audio_seconds, GROQ_BILL_FLOOR_S)
    if engine_id == "local":
        return billable_units(segment.audio_seconds, 0)
    return 1


def _find_silero_model(store_dir: str = "") -> str:
    """Locate ``silero_vad.onnx`` (bundled assets, model store, cwd assets).
    NO download logic here (model_store is Phase 5); None = not found."""
    import os
    candidates = []
    if store_dir:
        candidates.append(os.path.join(store_dir, "silero_vad.onnx"))
    base = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(base, "assets", "silero_vad.onnx"))
    candidates.append(os.path.join(os.getcwd(), "assets", "silero_vad.onnx"))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def _default_vad_factory(*, min_segment_s: float = 0.0, store_dir: str = ""):
    """Silero over raw ONNX (D8: optional onnxruntime, torch FORBIDDEN, session
    constructed HERE and never downloaded — Phase 5 owns fetch/verify)."""
    import importlib.util
    if importlib.util.find_spec("onnxruntime") is None:
        raise EngineUnavailableError(_LOCAL_EXTRAS_MESSAGE, engine_id="local",
                                     reason="onnxruntime not installed")
    model_path = _find_silero_model(store_dir)
    if model_path is None:
        raise EngineUnavailableError(
            "The local voice-activity model is missing. Download the local models "
            "in Settings > Models before recording.",
            engine_id="local", reason="silero_vad.onnx not found")
    import onnxruntime                      # lazy: optional dep (D8)
    session = onnxruntime.InferenceSession(model_path, providers=["CPUExecutionProvider"])
    return VadSegmenter(session, min_segment_s=min_segment_s)


def _default_engine_factory(config: SessionConfig, store_dir: str = ""):
    if config.dictation_mode == "chunked":
        return ChunkedEngine()
    if config.dictation_mode == "local":
        return LocalEngine(store_dir=store_dir)
    if config.dictation_mode == "live" and config.live_engine == "local":
        return LiveLocalEngine(store_dir=store_dir)    # Phase 6: LA-2 streaming
    if config.dictation_mode == "live":
        return LiveCloudEngine(config.live_engine)     # Phase 7: Gemini/OpenAI WS (U2 preflighted)
    raise EngineUnavailableError(                  # cloud live + unknown: fail closed (U.2)
        _LIVE_SOON_MESSAGE, engine_id=config.live_engine or "local", reason="phase not shipped")


class DictationSession:
    """Session object (A6 s2): single owner of queues, threads, and state."""

    def __init__(self, app, *, engine_factory=None, vad_factory=None, typer_factory=None,
                 clock=time.time):
        self._app = app
        self._engine_factory = engine_factory or _default_engine_factory
        self._vad_factory = vad_factory or _default_vad_factory
        self._typer_factory = typer_factory or build_typer   # Phase 3 typed path (D4)
        self._typer = None
        self._last_dictation = ""                   # memory-only history for Polish (U.4)
        self._clock = clock
        self._state = machine.SessionState.IDLE
        self._aborted = False
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._threads = []
        self._engine = None
        self._vad = None
        self._snapshot: SessionConfig | None = None
        self._block_q = None
        self._segment_q = None
        self._ring = None
        self._ledger = EMPTY_LEDGER
        self._engine_id = "local"
        self._segments: dict = {}
        self._pending: dict = {}
        self._failed: set = set()
        self._results: list = []
        self._notices: list = []
        self._expected = 0
        self._segments_total = 0
        self._workers_alive = 0
        self._workers_done = True
        self._segment_eos = False
        self._candidate: RawSpan | None = None
        self._dropped_notice = False
        self._drain_deadline: float | None = None   # set at stop (DRAIN_TIMEOUT bound)
        self._drained = False                       # late publishes after deadline drop

    # ---------------- public surface (main + Phase-4 UI) --------------------
    @property
    def state(self):
        return self._state

    @property
    def is_active(self) -> bool:
        return self._state is not machine.SessionState.IDLE

    @property
    def emissions(self) -> tuple:
        with self._lock:
            return tuple(self._results)

    @property
    def notices(self) -> tuple:
        with self._lock:
            return tuple(self._notices)

    def preflight(self, live=None) -> str:
        """Hard fail-closed guards BEFORE recording (A3 s5 mode table).

        Returns the resolved chunked-engine id (or ``"local"``); RAISES typed
        ``llm_errors`` — callers on the UI thread catch and show ``str(error)``.
        Never downloads (D7) and never touches the network.
        """
        source = self._app.config if live is None else live
        source = source if isinstance(source, dict) else {}
        mode = source.get("dictation_mode")
        cleanup = source.get("cleanup_mode")
        if cleanup == "local-ollama":
            polish_engine.require_ollama()         # shared bounded presence gate (R-11): absent raises
        if mode == "live":
            engine_id = str(source.get("live_engine") or "local")
            if engine_id != "local":
                return self._require_cloud_live(engine_id, source)   # consent -> key -> transport
            self._require_local_model(source, live_model_id(source.get("model_size")))
            return "local"
        if mode == "local":
            self._require_local_model(
                source, resolve_model_id(str(source.get("model_size") or "auto")))
            if cleanup == "cloud":
                self._require_classic_stage(source)   # cleanup stage guard (A3 s5)
            return "local"
        # -- chunked ---------------------------------------------------------
        resolved = resolve_chunked_engine(source)
        if resolved == "groq-whisper":
            if not config_manager.get_vendor_key(source, "groq"):
                raise LLMConfigError("Add your Groq API key in Settings before recording.")
        else:
            if resolved == "custom" and resolved != providers.resolve_provider_id(
                    source.get("provider")):
                raise LLMConfigError(
                    "Set the chunked engine to your current provider or a preset in Settings.")
            if resolved == "google-gemini":
                self._require_gemini_consent(source, resolved)
            if resolved != "groq-whisper" and cleanup != "off":
                self._require_classic_stage(source)
        return resolved

    def maybe_start(self) -> bool:
        """Mode router for ``main.start_recording`` (UI thread; never raises).
        False = not a session mode (the classic path stays byte-identical);
        True = handled (started, already active, or rejected with status+beep)."""
        live = getattr(self._app, "config", None)
        mode = live.get("dictation_mode") if isinstance(live, dict) else None
        if mode not in DICTATION_MODES:
            return False
        if self._state is not machine.SessionState.IDLE:
            return True                            # already active: no double start
        try:
            resolved = self.preflight(live)
            snapshot = build_session_config(
                live, session_id=uuid.uuid4().hex,
                started_at_epoch=int(self._clock()),
                api_keys=self._api_keys_for(live, mode, resolved))
            engine = self._engine_factory(snapshot, str(live.get("model_store_dir") or ""))
            min_s = min_segment_seconds(resolved) if mode == "chunked" else 0.0
            vad = self._vad_factory(min_segment_s=min_s,
                                    store_dir=str(live.get("model_store_dir") or ""))
            if mode == "live" and hasattr(engine, "degraded_sink"):
                engine.degraded_sink = self._add_notice  # D6: WS reconnect exhaustion -> notice
            engine.start(snapshot, *(self._live_emit,) if mode == "live" else ())  # D8/D7 typed failures land here
        except (DictationError, LLMError) as error:
            self._fail_start(str(error))
            return True
        self._install(mode, resolved, snapshot, engine, vad, live)
        try:
            self._app.recorder.start_block_stream(self._block_q.offer)
        except RecorderError as error:
            self._teardown()
            self._fail_start(str(error), processing=True)
            return True
        self._spawn_threads()
        self._app.beep(1000, 100)
        self._notice_status("Recording in progress. Press the shortcut to stop.", "recording",
                            indicator=("Recording", "Listening for your voice", "recording"))
        return True

    def request_stop(self) -> None:
        """RECORDING -> DRAINING (hotkey stop / hold release; UI thread). The
        recorder join is the only synchronous wait; the drain runs on session
        threads and completes via ``_finish`` (deadline-abandoned if needed)."""
        if self._state is not machine.SessionState.RECORDING:
            return
        self._transition("stop_requested")
        # Bounded drain (A6 DRAIN_TIMEOUT): pending work past the deadline is
        # abandoned WITH a notice — completion (and is_processing) never stick.
        self._drain_deadline = time.monotonic() + DRAIN_TIMEOUT_S
        self._app.beep(800, 100)
        self._notice_status("Processing your recording...", "processing",
                            indicator=("Processing", "Turning speech into text"))
        try:
            self._app.recorder.stop_block_stream()
        except RecorderError as error:
            self._add_notice("unit_error", str(error), engine_id=self._engine_id)
        finally:
            if self._block_q is not None:
                self._block_q.close()               # segmenter -> flush -> EOS chain

    def abort_all(self, reason: str = "user_cancel") -> None:
        """M2 extended: abort from ANY state — stop workers/buffers, suppress
        ALL injection (nothing is sunk after this flag), post completion."""
        if self._state is machine.SessionState.IDLE:
            return
        logger.info("Dictation abort (%s)", reason)  # reason token only (D11)
        self._aborted = True
        if self._typer is not None:                  # M2: the actor gates keystrokes too
            self._typer.abort()
        with self._cond:
            self._cond.notify_all()
        self._transition("abort")
        recorder = getattr(self._app, "recorder", None)
        if recorder is not None:
            try:
                recorder.stop_block_stream()
            except Exception:                        # teardown must never raise
                logger.warning("Recorder stream stop during abort failed", exc_info=True)
        if self._block_q is not None:
            self._block_q.close()
        for thread in self._threads:
            if thread is not None and thread.is_alive():
                thread.join(timeout=THREAD_JOIN_TIMEOUT_S)   # bounded (A6 s2.5)
        with self._cond:
            self._results.clear()                   # zero emissions after abort (M2)
            self._pending.clear()
            self._failed.clear()
        self._settle_live_quota()               # Phase 7: consumed duration still counts (D6)
        self._teardown()
        self._transition("abort_done")
        # Resets is_processing on the UI thread; strict no-op while exiting (M2).
        self._app.call_in_ui(getattr(self._app, "_on_processing_finished", lambda: None))

    def polish_last_dictation(self):
        """U.4 entry (tray item): re-run cleanup over the memory-only last
        dictation, then polish (safe INSERT default; replace only while the
        span is provably valid — D3/U3).  Busy/empty history = None."""
        if self.is_active or not self._last_dictation or self._typer is None:
            return None
        return polish_engine.polish_last(
            self._last_dictation, self._typer,
            getattr(self._app, "config", None) or {})

    def resume_focus(self) -> None:
        """Overlay Resume (D4): re-baseline the actor's focus target on the
        CURRENT foreground window, so an accepted resume re-arms typing."""
        typer = self._typer
        if typer is not None:
            typer.focus_guard.reset_target()

    @property
    def quota_snapshot(self) -> tuple:
        """Public read-only ledger view for the D6 countdown (A5A7 neutral
        view-model): ``(engine_id, ((window_id, used, limit, reset_at), ...))``
        per engine — values only; the ledger stays session-internal."""
        engines = self._ledger.engines
        return tuple(
            (engine_id, tuple((window.window_id, window.used, window.limit, window.reset_at)
                              for window in engine.windows))
            for engine_id, engine in engines.items())

    # ---------------- internals --------------------------------------------
    def _api_keys_for(self, live, mode, resolved) -> dict:
        """Least-privilege key set (A3 s2.2): ONLY vendors this session may
        call — provider (cleanup/chat) + the chunked engine's vendor (+ groq)."""
        vendors = {providers.resolve_provider_id(live.get("provider"))}
        if mode == "chunked":
            vendors.add("groq" if resolved == "groq-whisper" else resolved)
        if mode == "live" and resolved != "local":
            vendors.add("google-gemini" if resolved == "gemini-live" else "openai")  # WS engine key
        keys = {}
        for vendor in vendors:
            key = config_manager.get_vendor_key(live, vendor)
            if key:
                keys[vendor] = key
        return keys

    def _require_local_model(self, source, model_id: str) -> None:
        """U1/D7: ML extras + verified-ready model BEFORE recording; never downloads."""
        import importlib.util
        if importlib.util.find_spec("faster_whisper") is None:
            raise EngineUnavailableError(_LOCAL_EXTRAS_MESSAGE, engine_id="local",
                                         reason="faster-whisper not installed")
        models = source.get("local_models")
        if (models.get(model_id) if isinstance(models, dict) else None) != "ready":
            raise ModelNotDownloadedError(
                f"The local model '{model_id}' is not downloaded yet. "
                "Open Settings > Models to download it before recording.", model_id=model_id)

    @staticmethod
    def _require_gemini_consent(source, engine_id: str) -> None:
        """U2 fail-closed BEFORE any network: consent OR paid-key attestation (M20)."""
        if not (source.get("gemini_consent") is True or source.get("gemini_paid_key_confirmed") is True):
            raise ConsentRequiredError(
                "Google Gemini stays disabled until you accept the data notice "
                "in Settings (or confirm a paid key).", engine_id=engine_id)

    def _require_cloud_live(self, engine_id: str, source) -> str:
        """Phase-7 preflight ORDER (U2/D9): consent -> key -> transport import.  Never
        connects (the connect fails closed inside ``engine.start``) and never downloads."""
        if engine_id not in ("gemini-live", "openai-realtime"):
            # N1: config is choice-coerced (M13), but unknown ids still fail closed here.
            raise EngineUnavailableError(_LIVE_SOON_MESSAGE, engine_id=engine_id,
                                         reason="phase not shipped")
        if engine_id == "gemini-live":
            self._require_gemini_consent(source, engine_id)     # U2 FIRST
        vendor = "google-gemini" if engine_id == "gemini-live" else "openai"
        if not config_manager.get_vendor_key(source, vendor):
            raise LLMConfigError(
                "Add your Google Gemini API key in Settings." if engine_id == "gemini-live"
                else "Add your OpenAI API key in Settings.")
        import importlib.util
        if importlib.util.find_spec("websocket") is None:
            raise EngineUnavailableError(WS_TRANSPORT_MESSAGE, engine_id=engine_id,
                                         reason="websocket-client not installed")
        return engine_id

    def _require_classic_stage(self, source) -> None:
        """TODAY's exact URL+model guard, verbatim copy (D1) — used by the
        chunked preset path and by local when cleanup runs in the cloud."""
        effective_transcription = (
            source.get("transcription_model", "").strip() or source.get("model", "").strip())
        effective_post = source.get("model", "").strip()
        if not (source.get("provider_base_url", "").strip() and effective_transcription
                and effective_post):
            raise LLMConfigError(
                "Open Settings and set the provider URL and model before recording.")

    def _settle_live_quota(self) -> None:
        """D6 duration accounting for cloud-live WS sessions (session-duration billing; no
        vendor duration header exists for these WS APIs): charge() counts the ONE session
        dispatch, record_vendor_usage() writes the session seconds (max(local, vendor))."""
        snapshot, engine_id = self._snapshot, self._engine_id
        if not self._live or engine_id == "local" or snapshot is None:
            return                                  # local-live/on-device: no vendor budget
        elapsed = max(0, int(self._clock()) - int(snapshot.started_at_epoch))
        with self._cond:
            self._ledger = charge(self._ledger, engine_id, 0, self._clock())
            self._ledger = record_vendor_usage(self._ledger, engine_id, WINDOW_DAY, elapsed, 0)

    def _fail_start(self, message: str, *, processing: bool = False) -> None:
        self._app.set_status(message, "error")
        self._app.beep(400, 300)
        if processing:
            self._app._on_processing_finished()     # UI thread here (MP-10 writer)

    def _install(self, mode, resolved, snapshot, engine, vad, live) -> None:
        """Reset + wire all per-session state BEFORE the recorder starts."""
        self._aborted = False
        self._snapshot = snapshot
        self._engine = engine
        self._vad = vad
        self._engine_id = ("local" if mode == "local" else
                           snapshot.live_engine if mode == "live" else quota_engine_id(snapshot))
        self._live = mode == "live"                 # Phase 6: LA-2 live wiring switches
        self._block_q = BlockQueue()
        self._segment_q = queue.Queue(maxsize=SEGMENT_QUEUE_MAX)
        self._ring = PcmRing()
        self._segments, self._pending, self._failed = {}, {}, set()
        self._results, self._notices = [], []
        self._expected = self._segments_total = self._live_seq = 0
        self._workers_alive = 0
        self._workers_done = False
        self._segment_eos = False
        self._candidate = None
        self._dropped_notice = False
        self._drain_deadline = None
        self._drained = False
        self._threads = []
        self._ledger = EMPTY_LEDGER
        limits = live.get("quota_limits")
        base_quota = default_engine_quota(self._engine_id, limits if isinstance(limits, dict) else None)
        self._ledger = type(EMPTY_LEDGER)(EMPTY_LEDGER.version, {self._engine_id: base_quota})
        self._state = machine.next_state(self._state, "start") or machine.SessionState.RECORDING
        # UI-thread writer rule preserved: is_processing flips only here (start)
        # and inside _on_processing_finished (completion/abort).
        self._app.is_processing = True
        # Phase 3: actor types guarded emissions (D4); Phase-2 whole-text paste
        # remains the fallback when the actor typed nothing.
        try:
            self._typer = self._typer_factory(self._app, snapshot)
            self._typer.start()
        except Exception:                            # never-raises contract (maybe_start)
            logger.exception("Typing actor unavailable; whole-text paste fallback stays")
            self._typer = None

    def _teardown(self) -> None:
        """Drop per-session handles (bounded: threads were joined by caller)."""
        self._close_live_engine()               # Phase 6: LA-2 thread stops first
        self._stop_typer()
        self._engine, self._vad = None, None
        self._block_q = self._segment_q = self._ring = None
        self._candidate = None
        self._threads = []

    def _stop_typer(self) -> None:
        """ONE actor stop (bounded join, single clipboard restore); the stopped
        reference SURVIVES for later Polish (registry/focus) — idempotent."""
        if self._typer is not None:
            self._typer.stop()

    def _close_live_engine(self) -> None:
        """Live ONLY: stop the LA-2 decode thread + model (M2: never outlives its session)."""
        if self._live and self._engine is not None:
            try:
                self._engine.close()                # bounded join inside the engine
            except Exception:
                logger.warning("Live engine close failed", exc_info=True)

    def _spawn_threads(self) -> None:
        if self._live:
            self._workers_alive = 0   # live: feed + emitter ONLY (no segmenter/workers)
            names = (("dictation-live-feed", self._live_feed_loop),)
        else:
            workers = WORKER_COUNT.get(self._snapshot.dictation_mode if self._snapshot else "", 1)
            self._workers_alive = workers
            names = (("dictation-segmenter", self._segmenter_loop),) + tuple(
                (f"dictation-worker-{i}", self._worker_loop) for i in range(workers))
        self._threads = [threading.Thread(target=target, name=name, daemon=True) for name, target in names]
        self._threads.append(threading.Thread(target=self._emitter_loop, name="dictation-emitter", daemon=True))
        for thread in self._threads:
            thread.start()

    def _live_feed_loop(self) -> None:
        """LIVE feed (Phase 6): block_q -> LA-2 push; VAD-end/stop force confirm-all; emitter done-gate."""
        try:
            while not self._aborted:
                item = self._block_q.take(timeout=0.25)
                if item is END_OF_STREAM:
                    break
                self._engine.push_audio(item)       # None timeout ignored by push_audio
                if item is not None and any(self._vad.feed(item)):
                    self._engine.flush()            # VAD-end: confirm-all of the tail
            if not self._aborted and self._engine is not None:
                self._engine.flush()                # stop: tail -> ONE final emission
        except Exception:
            logger.exception("Live feed failed")    # D11: no audio/transcript logged
        finally:
            with self._cond:
                self._workers_done = True           # producer done: emitter may exit
                self._cond.notify_all()

    def _live_emit(self, emission) -> None:
        """Confirmed phrase -> ordered drain; emission_seq assigned ONLY in _drain_ordered; dropped after abort (M2)."""
        if self._aborted:
            return
        with self._cond:
            seq, self._live_seq = self._live_seq, self._live_seq + 1
            self._segments_total += 1
        self._publish(seq, [emission])

    def _add_notice(self, kind: str, detail: str, *, engine_id: str = "", reset_hint: str = ""):
        """Degraded path (D6: never silent, never failover): record the notice
        (Phase-4 offering dialog state) + a non-blocking status line."""
        notice = {"kind": kind, "engine_id": engine_id, "reset_hint": reset_hint,
                  "detail": detail,
                  # D6 offering STATE ONLY (Phase 4 renders it): no auto-switch.
                  "options": ("local", "finish_at_stop", "abort_with_clipboard")
                  if kind == "quota_exhausted" else ()}
        with self._cond:
            self._notices.append(notice)
        logger.warning("Degraded dictation notice: %s (%s)", kind, engine_id or "-")
        self._notice_status(detail, "error")

    def _notice_status(self, message, state, indicator=None):
        app = self._app
        app.set_status(message, state)
        if indicator is not None:
            app.indicator.show(*indicator)

    def _transition(self, trigger: str) -> None:
        target = machine.next_state(self._state, trigger)
        if target is None:
            logger.warning("Ignored illegal session transition: %s + %s",
                           getattr(self._state, "value", self._state), trigger)
            return
        self._state = target

    def _segmenter_loop(self) -> None:
        """block_q -> ring -> VAD -> bounded segment_q (D5 merge-on-backlog)."""
        seq = 0
        try:
            while not self._aborted:
                item = self._block_q.take(timeout=0.25)
                if item is END_OF_STREAM:
                    break
                if item is None:
                    continue
                self._ring.append(item)
                if self._ring.overflowed and not self._dropped_notice:
                    self._dropped_notice = True
                    # U.3: overflow is ALWAYS surfaced — never silent loss.
                    self._add_notice("queue_overflow",
                                     "Running reduced: audio was dropped under load.",
                                     engine_id=self._engine_id)
                for span in self._vad.feed(item):
                    seq = self._offer_span(seq, span, force=False)
            final = self._vad.flush()
            if final is not None and not self._aborted:
                seq = self._offer_span(seq, final, force=True)
            elif self._candidate is not None and not self._aborted:
                seq = self._flush_candidate(seq, force=True)
        except Exception:
            logger.exception("Segmenter failed")     # units disposition; drain never hangs (D11)
        finally:
            # EOS FLAG (not just the marker): the single sentinel wakes ONE
            # worker — every other worker observes the flag on its next Empty.
            with self._cond:
                self._segment_eos = True
                self._cond.notify_all()
            if not self._aborted:
                try:
                    self._segment_q.put(_SEGMENT_EOS, timeout=_PUT_TIMEOUT_S)
                except queue.Full:
                    pass                              # flag alone drains the workers

    def _offer_span(self, seq: int, span: RawSpan, *, force: bool) -> int:
        """Merge-on-backlog (A6 s2.2): while the queue holds >= 2 units, keep
        absorbing speech into ONE candidate (<= 30 s) instead of queueing more;
        otherwise flush through the bounded blocking put (D5 backpressure)."""
        if self._candidate is None:
            self._candidate = span
        else:
            self._candidate = RawSpan(self._candidate.start_byte, span.end_byte)
        if not force and self._segment_q.qsize() >= SEGMENT_QUEUE_MAX - 1:
            if (self._candidate.end_byte - self._candidate.start_byte) < _MAX_MERGE_BYTES:
                return seq                          # hold: the next speech run merges in
        return self._flush_candidate(seq, force=force)

    def _flush_candidate(self, seq: int, *, force: bool) -> int:
        candidate = self._candidate
        if candidate is None:
            return seq
        pcm16 = self._ring.read_range(candidate.start_byte, candidate.end_byte)
        if not pcm16:
            self._candidate = None
            return seq
        bytes_per_second = 16000 * 2
        segment_in = SegmentIn(
            segment_seq=seq,
            pcm16=pcm16,
            speech_start_s=candidate.start_byte / bytes_per_second,
            speech_end_s=candidate.end_byte / bytes_per_second,
            audio_seconds=len(pcm16) / bytes_per_second,
        )
        record = Segment(
            session_id=self._snapshot.session_id if self._snapshot else "",
            seq=seq,
            audio_ref=f"ring:{candidate.start_byte}:{candidate.end_byte}",
            start_ms=int(segment_in.speech_start_s * 1000),
            end_ms=int(segment_in.speech_end_s * 1000),
        )
        with self._cond:
            self._segments[seq] = record
            self._segments_total += 1
        try:
            self._segment_q.put(segment_in, timeout=_PUT_TIMEOUT_S)
            seq += 1
        except queue.Full:
            with self._cond:
                self._segments[seq] = set_status(record, "dropped")
            self._add_notice("queue_overflow",
                             "A speech segment could not be queued and was discarded.",
                             engine_id=self._engine_id)
        self._candidate = None
        return seq

    def _worker_loop(self) -> None:
        """Engine dispatch: D6 admission -> process -> ordered hand-off.
        Failures disposition the unit and processing CONTINUES (A6 s2.3)."""
        try:
            while not self._aborted:
                try:
                    item = self._segment_q.get(timeout=0.25)
                except queue.Empty:
                    if self._segment_eos:
                        return                        # queue drained after EOS
                    continue
                if item is _SEGMENT_EOS:
                    return
                self._process_one(item)
        finally:
            with self._cond:
                self._workers_alive -= 1
                if self._workers_alive <= 0:
                    self._workers_done = True
                    self._cond.notify_all()

    def _process_one(self, segment_in: SegmentIn) -> None:
        seq = segment_in.segment_seq
        record = self._segments.get(seq)
        if record is not None:
            record = set_status(record, "transcribing")
            with self._cond:
                self._segments[seq] = record
        units = _quota_units_for(self._engine_id, segment_in)
        with self._cond:
            verdict = check_admission(self._ledger, self._engine_id, units, self._clock())
            if verdict.ok:
                # Reserve ATOMICALLY with admission (A6 s6.2-1): two workers can
                # never both pass a budget of one, and the commit is conservative
                # — a later pre-send failure KEEPS the reservation (never free).
                self._charge(units)
        if not verdict.ok:
            # D6: NO silent failover — refusal becomes a notice; unit dispositioned.
            hint = verdict.reset_hint
            self._disposition(seq, LLMQuotaError.__name__,
                              f"Paused for {self._engine_id}" + (f" until {hint}" if hint else ""),
                              notice=("quota_exhausted",
                                      f"Quota exhausted — paused until {hint or 'the vendor reset'}. "
                                      "Switch engine, finish at stop, or abort (your choice).",
                                      hint))
            return
        try:
            emissions = self._engine.process(segment_in)
        except LLMConfigError as error:
            # Reserved at admission already (conservative): config failures
            # cannot happen mid-session after preflight, so no refund path.
            self._disposition(seq, type(error).__name__, str(error))
        except LLMError as error:
            # Request MAY have reached the vendor: the reservation stands
            # (A6 s6.2-4 — never pretend billable work was free).
            self._disposition(seq, type(error).__name__, str(error))
        except DictationError as error:
            self._disposition(seq, type(error).__name__, str(error))
        except Exception as error:                  # engine defect: CONTINUE the rest
            logger.exception("Engine failed on segment %d", seq)
            self._disposition(seq, type(error).__name__,
                              "Part of the recording could not be processed.",
                              notice=("unit_error",
                                      "Part of the recording could not be processed.", ""))
            return
        else:
            if record is not None:
                with self._cond:
                    self._segments[seq] = set_status(record, "confirmed")
            self._publish(seq, list(emissions or ()))

    def _charge(self, units: int) -> None:
        """Reserve/commit ledger units.  Caller MUST hold ``self._cond`` (the
        admission+charge pair is atomic — see _process_one)."""
        if units <= 0 or self._engine_id == "local":
            return                                   # local: no vendor budget (limit -1)
        self._ledger = charge(self._ledger, self._engine_id, units, self._clock())

    def _disposition(self, seq: int, error_kind: str, detail: str, *, notice=None) -> None:
        record = self._segments.get(seq)
        if record is not None:
            with self._cond:
                self._segments[seq] = set_status(record, "failed", error_kind=error_kind)
        self._publish(seq, [])
        if notice is not None:
            self._add_notice(notice[0], notice[1], engine_id=self._engine_id,
                             reset_hint=(notice[2] if len(notice) > 2 else ""))
        else:
            logger.warning("Segment %d dispositioned (%s)", seq, error_kind)  # kinds only

    def _publish(self, seq: int, emissions: list) -> None:
        with self._cond:
            if self._drained or seq in self._failed or seq in self._pending:
                return                               # drained: late units drop (deadline path);
                                                     # else idempotent duplicate-replay drop
            if emissions:
                self._pending[seq] = emissions
            else:
                self._failed.add(seq)
            self._cond.notify_all()

    def _emitter_loop(self) -> None:
        """The ONLY emission_seq assigner: strictly ordered by segment_seq;
        after ABORT nothing more is ever delivered (M2)."""
        latest = -1
        try:
            while not self._aborted:
                latest = self._drain_ordered(latest)
                with self._cond:
                    if self._workers_done and self._expected >= self._segments_total:
                        break
                    deadline = self._drain_deadline
                    if deadline is not None and time.monotonic() > deadline:
                        self._notices.append(
                            {"kind": "unit_error", "engine_id": self._engine_id,
                             "reset_hint": "", "detail": "Some audio could not be "
                             "processed in time; the rest was completed.", "options": ()})
                        break
                    self._cond.wait(timeout=0.2)
        finally:
            if not self._aborted:
                self._finish()

    def _drain_ordered(self, latest: int) -> int:
        while not self._aborted:
            with self._cond:
                if self._expected in self._failed:
                    self._failed.discard(self._expected)
                    self._expected += 1
                    continue
                batch = self._pending.pop(self._expected, None)
                if batch is None:
                    if (self._workers_done and self._expected < self._segments_total
                            and self._expected not in self._pending):
                        self._failed.add(self._expected)   # lost unit: never hang the drain
                        continue
                    return latest
                self._expected += 1
            for emission in batch:
                latest = next_seq(latest)
                ordered = replace(emission, emission_seq=latest)
                with self._cond:
                    if self._aborted:
                        return latest                 # M2: nothing lands after abort
                    self._results.append(ordered)
                if self._typer is not None:
                    self._typer.submit(ordered)       # Phase 3: guarded typed path (D4)
        return latest

    def _finish(self) -> None:
        """TYPING -> IDLE: paste the ordered text (worker-side, like classic),
        then marshal completion to the UI thread (M2: never when exiting)."""
        self._drained = True                          # no late unit may append post-drain
        self._transition("all_finals_emitted")
        text = " ".join(item.text for item in self.emissions if item.text).strip()
        self._last_dictation = text or self._last_dictation   # U.4 Polish history
        typed_by_actor = self._typer is not None and self._typer.typed_any
        self._stop_typer()                            # join + ONE clipboard restore (D4)
        try:
            if text and not typed_by_actor and not getattr(self._app, "is_exiting", False):
                paste_text(text)                      # Phase-2 fallback sink (actor typed none)
                self._app.beep(1200, 200)
        except (PasteError, ValueError) as error:
            message = f"Could not process recording: {error}"
            self._app.call_in_ui(lambda: self._app.set_status(message, "error"))
        else:
            self._app.call_in_ui(
                lambda: self._app.set_status("Done. Ready for the next recording.", "ready"))
        self._transition("done")                      # TYPING -> IDLE
        self._app.call_in_ui(getattr(self._app, "_on_processing_finished", lambda: None))
        self._settle_live_quota()                 # Phase 7: cloud-live D6 duration accounting
        self._close_live_engine()                 # Phase 6: LA-2 thread stops here
        self._engine = self._vad = None
