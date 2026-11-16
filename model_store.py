"""Settings-only model store (D7): download / resume / verify / install / evict.

Implements the port ``settings_models_view`` binds (``app.model_store``:
download/pause/resume/cancel_download/re_check_file/delete) under the devops
annex s3 contract: ``Range``+``If-Range`` resume, size*1.2 disk preflight,
streamed SHA-256 via the REUSED ``model_manifest`` parser/verifier, atomic
``os.replace`` into ``models/<id>/<version>/``, ordered mirror fallback,
post-activation eviction, and startup ``.part`` GC that KEEPS fresh parts so
kill/restart resume survives.  stdlib ``urllib`` only; 64 KiB bounded chunks;
D7: NOTHING downloads unless a Settings button calls in; D11: sizes/speeds
only in logs — URLs pass the redaction helper first.
"""

import json
import logging
import os
import random
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request

from config_manager import save_config
from local_engine import default_store_dir, resolve_model_id
from model_manifest import DigestVerifier, find, parse_manifest
from whisper_http import _safe_url_for_log as _redact_url

logger = logging.getLogger(__name__)

CHUNK_BYTES = 64 * 1024                  # bounded memory per read (A5)
SOURCE_TRIES = 3                         # annex: 3 tries per mirror source
DISK_MARGIN = 1.2                        # annex: size * 20% headroom preflight
SILERO_ID = "silero-vad"                 # VAD artifact (D8 torch-forbidden raw ONNX)
PART_STALE_DAYS = 7                      # startup GC: stale parts only (fresh = resume)

# A5A7 s1.5 intermediate-state copy, rendered by the store (terminal
# ready/not_downloaded belong to the view's config-driven refresh; the failed/
# disk strings mirror the A5A7 table verbatim — the store cannot import Tk).
COPY_DOWNLOADING = "Downloading {size} \u2014 {pct}%."
COPY_PAUSED = "Paused at {pct}%. Resume when you're ready."
COPY_VERIFYING = "Verifying download (SHA-256)\u2026"
COPY_CANCELLED = "Download cancelled. Nothing was installed."
COPY_FAILED = "Download failed. Nothing was installed."
COPY_DISK = "Not enough disk space: {need} MB needed, {free} MB available on {path}."


class _StoreError(Exception):
    """Internal: state name + user-safe detail (never surfaces raw)."""

    def __init__(self, state, detail):
        super().__init__(detail)
        self.state = state
        self.detail = detail


class _Restart(Exception):
    """Range ignored / entity changed: retry the source from byte 0."""


def _manifest_path() -> str:
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "assets", "models_manifest.json")


class ModelStore:
    """Per-app download manager: ONE worker thread; Settings is the only trigger."""

    def __init__(self, app):
        self._app = app
        self._lock = threading.Lock()
        self._worker = None
        self._state = "not_downloaded"
        self._detail = ""
        self._pct = 0
        self._cancel = threading.Event()
        self._pause = threading.Event()
        self._pending = []
        self._store_dir = default_store_dir(
            (app.config.get("model_store_dir") or "") if isinstance(app.config, dict) else "")
        self._manifest = None
        try:
            with open(_manifest_path(), "r", encoding="utf-8") as handle:
                self._manifest = parse_manifest(json.load(handle))
        except Exception:
            logger.exception("Model manifest unavailable")   # honest: downloads fail closed
        self._gc_stale_parts()
        self._refresh_installed()

    # ---------------- port (settings_models_view) ----------------------------
    def download(self) -> None:
        """D7: the ONLY download trigger (never a hotkey); single-flight.
        Resume is free — existing ``.part`` sidecars become Range offsets."""
        with self._lock:
            if self._worker is not None or self._manifest is None:
                return
            ids = [m for m in self._wanted_ids() if not self._is_installed(m)]
            if not ids:
                return
            self._pending, self._cancel, self._pause = ids, threading.Event(), threading.Event()
            self._state, self._detail, self._pct = "downloading", "", 0
            self._worker = threading.Thread(target=self._run, name="model-store", daemon=True)
            self._worker.start()

    def pause(self) -> None:
        self._pause.set()                     # worker stops at the next chunk (.part kept)

    def resume(self) -> None:
        if self._worker is None:
            self.download()                   # pause kept the parts: Range resumes

    def cancel_download(self) -> None:
        self._cancel.set()                    # part kept for a later resume (A5A7)

    def re_check_file(self) -> None:
        """Re-verify installed bytes vs the manifest (streamed SHA-256)."""
        if self._manifest is None:
            return
        problems = []
        for model_id in self._wanted_ids():
            if self._is_installed(model_id) and not self._reverify(model_id):
                self._set_local_state(model_id, "stale")
                problems.append(model_id)
        if problems:
            self._push_state("failed", "Verification failed: " + ", ".join(problems) +
                             " does not match its pinned SHA-256. Re-download it.")
        else:
            self._push_state("ready", "")

    def delete(self) -> None:
        """Settings-only removal; refuses while a local session could use it
        (A5A7: 'In use by the active session.' instead of a silent no-op)."""
        if getattr(getattr(self._app, "dictation", None), "is_active", False):
            self._push_state("failed", "In use by the active session.")
            return
        for model_id in self._wanted_ids():
            shutil.rmtree(os.path.join(self._store_dir, model_id), ignore_errors=True)
            self._set_local_state(model_id, None)
        self._push_state("not_downloaded", "")
        self._call_view_refresh()

    # ---------------- worker -------------------------------------------------
    def _wanted_ids(self):
        config = self._app.config if isinstance(self._app.config, dict) else {}
        return [resolve_model_id(config.get("model_size", "auto")), SILERO_ID]

    def _run(self) -> None:
        try:
            for model_id in list(self._pending):
                if self._cancel.is_set():
                    return self._finish("not_downloaded", COPY_CANCELLED)
                if self._pause.is_set():
                    return self._finish("paused", "")
                self._download_one(model_id)              # raises _StoreError
            self._pending = []
            self._refresh_installed()
            self._finish("ready", "")
        except _StoreError as error:
            self._finish(error.state, error.detail)
        except Exception:
            logger.exception("Model store worker failed")
            self._finish("failed", "Unexpected download failure.")
        finally:
            with self._lock:
                self._worker = None

    def _finish(self, state, detail) -> None:
        if state == "ready":
            self._pending = []
        self._push_state(state, detail)
        self._call_view_refresh()

    def _download_one(self, model_id: str) -> None:
        artifact = find(self._manifest, model_id)
        if artifact is None:
            raise _StoreError("failed", f"Model {model_id} is not in this build's manifest.")
        if any(set(f.sha256) == {"0"} for f in artifact.files):
            raise _StoreError("failed",
                              "This build's manifest is not release-pinned yet (digest pending).")
        version_dir = os.path.join(self._store_dir, model_id, str(artifact.version))
        missing = [f for f in artifact.files
                   if not os.path.isfile(os.path.join(version_dir, f.path))]
        if not missing:
            return self._activate(model_id, artifact, version_dir)
        todo = sum(f.size for f in missing)
        self._preflight_disk(todo, version_dir)             # size*1.2 BEFORE first byte
        os.makedirs(version_dir, exist_ok=True)
        done, started = 0, time.monotonic()
        for entry in artifact.files:
            dest = os.path.join(version_dir, entry.path)
            if os.path.isfile(dest) and self._digest_file(dest, entry.sha256):
                done += entry.size
                continue
            outcome = self._fetch_file(entry, dest, model_id, done, todo, started)
            if outcome != "ok":
                raise _StoreError("paused" if outcome == "paused" else "not_downloaded",
                                  "" if outcome == "paused" else COPY_CANCELLED)
            done += entry.size
        # VERIFYING: re-hash installed bytes before activation — never trusted
        # on the file name alone (D7 honest states).
        self._push_state("verifying", COPY_VERIFYING, pct=100)
        self._reverify(model_id, version_dir)
        self._activate(model_id, artifact, version_dir)

    def _preflight_disk(self, need_bytes, path) -> None:
        required, probe = int(need_bytes * DISK_MARGIN), path
        while not os.path.isdir(probe) and len(probe) > 3:
            probe = os.path.dirname(probe) or probe
        try:
            free = shutil.disk_usage(probe).free
        except OSError:
            free = 0
        if free < required:
            raise _StoreError("disk_error", COPY_DISK.format(
                need=required // (1024 * 1024) + 1, free=free // (1024 * 1024),
                path=self._store_dir))

    def _fetch_file(self, entry, dest, model_id, base_done, total, started) -> str:
        """Ordered mirrors x bounded retries, Range resume, streamed digest,
        atomic install.  Returns 'ok' | 'paused' | 'cancelled'."""
        part, etag_file, expected = dest + ".part", dest + ".etag", entry.sha256
        resume_from = os.path.getsize(part) if os.path.isfile(part) else 0
        for url in entry.urls:
            for attempt in range(1, SOURCE_TRIES + 1):
                if self._cancel.is_set():
                    return "cancelled"
                if self._pause.is_set():
                    return "paused"
                verifier = DigestVerifier(expected)
                if resume_from:
                    self._hash_into(part, verifier)       # resumed bytes join the digest
                try:
                    headers = {"User-Agent": "WhisperLive-model-store"}
                    if resume_from:
                        headers["Range"] = f"bytes={resume_from}-"
                        etag = self._read_text(etag_file)
                        if etag:
                            headers["If-Range"] = etag    # changed entity -> full restart
                    request = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(request, timeout=60) as response:
                        if resume_from and getattr(response, "status", 200) != 206:
                            self._reset_file(part, etag_file)
                            raise _Restart()
                        if response.headers.get("ETag"):
                            self._write_text(etag_file, response.headers.get("ETag"))
                        wrote = resume_from
                        with open(part, "ab" if resume_from else "wb") as handle:
                            while True:
                                if self._cancel.is_set() or self._pause.is_set():
                                    handle.flush()
                                    os.fsync(handle.fileno())
                                    return ("cancelled" if self._cancel.is_set()
                                            else "paused")
                                chunk = response.read(CHUNK_BYTES)
                                if not chunk:
                                    break
                                handle.write(chunk)
                                verifier.update(chunk)
                                wrote += len(chunk)
                                self._report_progress(model_id, base_done + wrote,
                                                      total, started)
                            handle.flush()
                            os.fsync(handle.fileno())      # crash-safe before close (annex)
                    if verifier.matches() and os.path.getsize(part) == entry.size:
                        os.replace(part, dest)             # same-volume atomic install
                        return "ok"
                    logger.warning("Hash/size mismatch: %s", entry.path)
                    self._reset_file(part, etag_file)
                    resume_from = 0
                    break                                   # corrupted: next mirror
                except _Restart:
                    resume_from = 0
                    continue                                # same source, no Range now
                except _StoreError:
                    raise
                except Exception as error:                  # transient: KEEP the part
                    logger.warning("Download attempt failed (%s): %s",
                                   type(error).__name__, _redact_url(url))
                    time.sleep(min(4.0, attempt + random.uniform(0.0, 0.5)))
                    resume_from = os.path.getsize(part) if os.path.isfile(part) else 0
                    continue
        raise _StoreError("failed",
                          f"Could not fetch {entry.path} from any mirror (source exhausted).")

    def _reset_file(self, part, etag_file) -> None:
        for path in (part, etag_file):
            try:
                os.unlink(path)
            except OSError:
                pass

    def _digest_file(self, path, expected) -> bool:
        """Streamed SHA-256 of an on-disk file (also fills resumed parts)."""
        verifier = DigestVerifier(expected)
        self._hash_into(path, verifier)
        return verifier.matches()

    def _hash_into(self, path, verifier) -> None:
        try:
            with open(path, "rb") as handle:
                while True:
                    chunk = handle.read(CHUNK_BYTES)
                    if not chunk:
                        return
                    verifier.update(chunk)
        except OSError:
            return

    def _reverify(self, model_id, version_dir=None) -> bool:
        artifact = find(self._manifest, model_id)
        directory = version_dir or self._installed_dir(model_id)
        if artifact is None or directory is None:
            return False
        return all(self._digest_file(os.path.join(directory, f.path), f.sha256)
                   for f in artifact.files)

    def _activate(self, model_id, artifact, version_dir) -> None:
        """READY: persist local_models, flatten the VAD alias dictation_session's
        finder expects, evict older versions only AFTER successful activation."""
        self._set_local_state(model_id, "ready")
        if model_id == SILERO_ID:
            src = os.path.join(version_dir, "silero_vad.onnx")
            alias, tmp = os.path.join(self._store_dir, "silero_vad.onnx"), None
            try:
                tmp = alias + ".part"
                shutil.copyfile(src, tmp)
                os.replace(tmp, alias)
            except OSError:
                logger.warning("Could not write the flat silero alias", exc_info=True)
        self._evict_old_versions(model_id, keep=version_dir)
        logger.info("Model ready: %s (version %s)", model_id, artifact.version)

    def _evict_old_versions(self, model_id, keep) -> None:
        root = os.path.join(self._store_dir, model_id)
        try:
            versions = [os.path.join(root, n) for n in os.listdir(root)
                        if os.path.isdir(os.path.join(root, n))]
        except OSError:
            return

        def _mtime(path):
            try:
                return os.path.getmtime(path)
            except OSError:
                return 0.0

        versions.sort(key=_mtime, reverse=True)
        retained = {os.path.normpath(keep)}
        for candidate in versions:                   # newest OTHER = "previous" stays
            if os.path.normpath(candidate) not in retained:
                retained.add(os.path.normpath(candidate))
                break
        for candidate in versions:
            if os.path.normpath(candidate) not in retained:
                shutil.rmtree(candidate, ignore_errors=True)

    def _installed_dir(self, model_id):
        root = os.path.join(self._store_dir, model_id)
        try:
            candidates = [os.path.join(root, name) for name in os.listdir(root)
                          if os.path.isdir(os.path.join(root, name))]
        except OSError:
            return None
        if not candidates:
            return None

        def _mtime(path):
            try:
                return os.path.getmtime(path)
            except OSError:
                return 0.0

        return max(candidates, key=_mtime)   # newest activation; names aren't zero-padded ("10"<"9")

    def _is_installed(self, model_id: str) -> bool:
        directory, artifact = self._installed_dir(model_id), find(self._manifest, model_id)
        if directory is None or artifact is None:
            return False
        return all(os.path.isfile(os.path.join(directory, f.path)) for f in artifact.files)

    def _refresh_installed(self) -> None:
        """Startup truth: config 'ready' but files missing/size-wrong -> stale
        (presence+size only; full hashing stays on re_check_file — bounded boot)."""
        if not isinstance(self._app.config, dict) or self._manifest is None:
            return
        local_models = self._app.config.get("local_models")
        if not isinstance(local_models, dict):
            return
        changed = False
        for model_id, state in list(local_models.items()):
            artifact = find(self._manifest, model_id)
            if state != "ready" or artifact is None:
                continue
            directory = self._installed_dir(model_id)
            if directory is None or not all(
                    os.path.isfile(os.path.join(directory, f.path))
                    and os.path.getsize(os.path.join(directory, f.path)) == f.size
                    for f in artifact.files):
                local_models[model_id] = "stale"
                changed = True
        if changed:
            save_config(self._app.config)

    def _set_local_state(self, model_id, state) -> None:
        if not isinstance(self._app.config, dict):
            return
        local_models = self._app.config.get("local_models")
        if not isinstance(local_models, dict):
            local_models = self._app.config["local_models"] = {}
        if state is None:
            local_models.pop(model_id, None)
        else:
            local_models[model_id] = state
        save_config(self._app.config)

    def _gc_stale_parts(self) -> None:
        """Startup .part GC (annex): stale (>7d) or manifest-less sidecars only —
        fresh parts SURVIVE so kill/restart resume keeps working (R6)."""
        if not os.path.isdir(self._store_dir):
            return
        now = time.time()
        known = ({f.path for a in self._manifest.artifacts for f in a.files}
                 if self._manifest else set())
        for root, _dirs, files in os.walk(self._store_dir):
            for name in files:
                if not (name.endswith(".part") or name.endswith(".etag")):
                    continue
                path = os.path.join(root, name)
                stale = name.split(".part")[0].split(".etag")[0] not in known
                try:
                    stale = stale or (now - os.path.getmtime(path)) > PART_STALE_DAYS * 86400
                except OSError:
                    stale = True
                if stale:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

    # ---------------- UI push (annex: changes via call_in_ui) ----------------
    def _report_progress(self, model_id, done, total, started) -> None:
        elapsed = max(0.001, time.monotonic() - started)
        pct = int(min(100, (100 * done) // max(1, total))) if total else 0
        speed = (done / (1024 * 1024)) / elapsed
        eta = int((total - done) / (1024 * 1024) / max(speed, 0.001)) if total > done else 0
        detail = (f"{done // (1024 * 1024)} of {total // (1024 * 1024)} MB \u00b7 "
                  f"{speed:.1f} MB/s \u00b7 about {eta}s left")
        config = self._app.config if isinstance(self._app.config, dict) else {}
        self._push_state("downloading", detail, pct=pct,
                         label=COPY_DOWNLOADING.format(size=config.get("model_size", "auto"),
                                                       pct=pct))

    def _push_state(self, state, detail, pct=0, label=None) -> None:
        with self._lock:
            self._state, self._detail, self._pct = state, detail, pct
        self._render(label)

    def _render(self, label=None) -> None:
        """Push one state into the OPEN dialog (honest intermediate states;
        terminal ready/not_downloaded come from the view's config refresh)."""
        def apply():
            widgets = getattr(self._app, "settings_widgets", {}) or {}
            state_label = widgets.get("download_state")
            if state_label is None:
                return                      # dialog closed: state stays tracked
            config = self._app.config if isinstance(self._app.config, dict) else {}
            templates = {"downloading": COPY_DOWNLOADING, "paused": COPY_PAUSED,
                         "verifying": COPY_VERIFYING, "cancelled": COPY_CANCELLED}
            try:
                if self._state == "failed":
                    text = self._detail or COPY_FAILED
                elif self._state == "disk_error":
                    text = self._detail
                elif self._state == "not_downloaded":
                    text = "Not downloaded yet."
                elif label is not None:
                    text = label
                elif self._state in templates:
                    text = templates[self._state].format(
                        size=config.get("model_size", "auto"), pct=self._pct)
                else:
                    text = None
                if text:
                    state_label.config(text=text)
                progress = widgets.get("download_progress")
                if progress is not None:
                    if self._state == "verifying":
                        progress.config(mode="indeterminate")
                        progress.start()
                    else:
                        try:
                            progress.stop()
                        except Exception:
                            pass
                        progress.config(mode="determinate", value=self._pct)
                detail_label = widgets.get("download_detail")
                if detail_label is not None:
                    detail_label.config(
                        text="" if self._state in ("ready", "disk_error") else self._detail)
            except Exception:               # dialog closed mid-push
                pass

        try:
            self._app.call_in_ui(apply)
        except Exception:
            logger.debug("UI push failed", exc_info=True)

    def _call_view_refresh(self) -> None:
        refresh = getattr(self._app, "_refresh_models_view", None)
        if refresh is None:
            return
        try:
            self._app.call_in_ui(refresh)
        except Exception:
            logger.debug("models refresh failed", exc_info=True)

    @staticmethod
    def _read_text(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            return ""

    @staticmethod
    def _write_text(path, text):
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
        except OSError:
            pass
