"""Model store tests (plan 2.3; gate G3 supply chain).

Owner: qa-test-assurance.  ``urllib.request.urlopen`` is fully mocked — no
network ever.  ``ModelStore`` is pointed at a TEMP store dir and a TEMP
manifest whose digests are computed from known payloads, so every path is
deterministic.

Covers: Range+If-Range resume (resumed bytes JOIN the digest -> verify-then-
install), disk preflight x1.2 before the first byte, atomic ``os.replace``
install, ordered mirror fallback with BOUNDED retries, placeholder-digest
fail-closed, post-activation version eviction, ``.part`` GC that KEEPS fresh
parts (kill/restart resume), re-check tamper detection, and cancel semantics.
"""

import hashlib
import json
import os
import threading
import time
import unittest
import urllib.error
from unittest import mock

import model_store
from model_store import ModelStore

BASE_ID = "faster-whisper-base"
SILERO_ID = "silero-vad"


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class FakeResponse:
    """urlopen double: streams ``payload`` in chunks; records reads."""

    def __init__(self, payload, *, status=200, headers=None, on_read=None):
        self._payload = bytes(payload)
        self._pos = 0
        self.status = status
        self.headers = dict(headers or {})
        self.on_read = on_read
        self.reads = 0

    def read(self, size=-1):
        self.reads += 1
        if self.on_read is not None:
            self.on_read(self)
        if size is None or size < 0:
            size = len(self._payload) - self._pos
        chunk = self._payload[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class StoreHarness:
    """Temp store dir + temp manifest + patched save_config/urlopen."""

    def __init__(self, files_by_url, *, manifest_files=None, model_size="fast",
                 disk_free=None, placeholder=False, extra_models=None):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.store_dir = os.path.join(self.temp.name, "models")
        os.makedirs(self.store_dir)
        self.manifest_path = os.path.join(self.temp.name, "manifest.json")
        self.files_by_url = files_by_url            # url -> payload (or callable)
        self.requests = []                          # (url, headers)
        entries = manifest_files if manifest_files is not None else \
            self._default_manifest(placeholder)
        if extra_models:
            entries = list(entries) + list(extra_models)
        self._write_manifest(entries)
        self.save_calls = []
        self.disk_free = disk_free

    def _default_manifest(self, placeholder):
        base_payload = b"BASE-WEIGHTS-" + os.urandom(64)
        silero_payload = b"SILERO-" + os.urandom(32)
        self.base_payload = base_payload
        self.silero_payload = silero_payload
        base_url = "https://primary.example/base/model.bin"
        silero_url = "https://primary.example/silero/silero_vad.onnx"
        self.files_by_url.setdefault(base_url, base_payload)
        self.files_by_url.setdefault(silero_url, silero_payload)
        return [
            {"model_id": BASE_ID, "version": 1, "kind": "faster-whisper",
             "files": [{"path": "model.bin", "url": [base_url],
                        "sha256": ("0" * 64 if placeholder else _sha(base_payload)),
                        "size": len(base_payload)}]},
            {"model_id": SILERO_ID, "version": 1, "kind": "silero-vad",
             "files": [{"path": "silero_vad.onnx", "url": [silero_url],
                        "sha256": _sha(silero_payload),
                        "size": len(silero_payload)}]},
        ]

    def _write_manifest(self, entries):
        with open(self.manifest_path, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "models": entries}, handle)

    def make_app(self):
        app = type("App", (), {})()
        app.config = {"model_size": "fast", "model_store_dir": self.store_dir,
                      "local_models": {}}
        app.settings_widgets = {}
        app.is_exiting = False
        app.call_in_ui = lambda callback: callback()
        app._refresh_models_view = lambda: None
        return app

    def build(self):
        app = self.make_app()
        patches = [
            mock.patch.object(model_store, "_manifest_path",
                              return_value=self.manifest_path),
            mock.patch.object(model_store, "save_config",
                              side_effect=lambda cfg: self.save_calls.append(
                                  json.loads(json.dumps(cfg))) or True),
            mock.patch.object(model_store.time, "sleep"),      # bounded retries, fast
        ]
        if self.disk_free is not None:
            usage = mock.Mock()
            usage.free = self.disk_free
            patches.append(mock.patch.object(model_store.shutil, "disk_usage",
                                             return_value=usage))
        for patch in patches:
            patch.start()
            self.add_patch(patch)
        store = ModelStore(app)
        store.app = app
        return store

    def add_patch(self, patch):
        if not hasattr(self, "_patches"):
            self._patches = []
        self._patches.append(patch)

    def opener(self, request, timeout=None):
        url = request.full_url
        headers = dict(request.header_items())
        self.requests.append((url, headers))
        spec = self.files_by_url.get(url)
        if spec is None:
            raise urllib.error.URLError(f"scripted refusal for {url}")
        if callable(spec):
            return spec(request)
        # default: full payload with 200 unless the client asked for a Range
        ranged = any(name == "range" for name, _value in _lower(headers))
        return FakeResponse(spec, status=206 if ranged else 200)

    def close(self):
        for patch in getattr(self, "_patches", []):
            patch.stop()
        self.temp.cleanup()


def _lower(headers):
    return [(name.lower(), value) for name, value in headers.items()]


def _header(headers, name):
    lowered = {key.lower(): value for key, value in headers.items()}
    return lowered.get(name.lower())


def wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class FreshInstallTests(unittest.TestCase):
    def test_fresh_download_verifies_and_installs_atomically(self):
        harness = StoreHarness({})
        store = harness.build()
        try:
            with mock.patch("urllib.request.urlopen", side_effect=harness.opener):
                store._download_one(BASE_ID)
            dest = os.path.join(store._store_dir, BASE_ID, "1", "model.bin")
            self.assertTrue(os.path.isfile(dest))
            with open(dest, "rb") as handle:
                self.assertEqual(handle.read(), harness.base_payload)
            self.assertFalse(os.path.exists(dest + ".part"))    # replaced, not copied
            self.assertEqual(store._app.config["local_models"][BASE_ID], "ready")
            self.assertTrue(harness.save_calls)                 # state persisted
        finally:
            harness.close()

    def test_no_range_header_on_a_fresh_fetch(self):
        harness = StoreHarness({})
        store = harness.build()
        try:
            with mock.patch("urllib.request.urlopen", side_effect=harness.opener):
                store._download_one(SILERO_ID)
            _url, headers = harness.requests[0]
            self.assertIsNone(_header(headers, "Range"))
            self.assertIsNone(_header(headers, "If-Range"))
            dest = os.path.join(store._store_dir, SILERO_ID, "1", "silero_vad.onnx")
            self.assertTrue(os.path.isfile(dest))
        finally:
            harness.close()

    def test_already_installed_files_skip_the_network(self):
        harness = StoreHarness({})
        store = harness.build()
        try:
            with mock.patch("urllib.request.urlopen", side_effect=harness.opener):
                store._download_one(BASE_ID)
            harness.requests.clear()
            with mock.patch("urllib.request.urlopen", side_effect=harness.opener):
                store._download_one(BASE_ID)                    # digest hits -> no fetch
            self.assertEqual(harness.requests, [])
        finally:
            harness.close()


class ResumeTests(unittest.TestCase):
    """G3: resume-then-verify — resumed bytes JOIN the streamed digest."""

    def test_range_and_if_range_resume_completes_the_digest(self):
        harness = StoreHarness({})
        payload = harness.base_payload
        half = len(payload) // 2
        store = harness.build()
        try:
            version_dir = os.path.join(store._store_dir, BASE_ID, "1")
            os.makedirs(version_dir, exist_ok=True)
            part = os.path.join(version_dir, "model.bin.part")
            etag = os.path.join(version_dir, "model.bin.etag")
            with open(part, "wb") as handle:
                handle.write(payload[:half])                    # crashed mid-download
            with open(etag, "w", encoding="utf-8") as handle:
                handle.write('"etag-123"')

            def serve_tail(request):
                return FakeResponse(payload[half:], status=206,
                                    headers={"ETag": '"etag-123"'})
            harness.files_by_url = {"https://primary.example/base/model.bin":
                                    serve_tail}
            with mock.patch("urllib.request.urlopen", side_effect=harness.opener):
                store._download_one(BASE_ID)

            _url, headers = harness.requests[0]
            self.assertEqual(_header(headers, "Range"), f"bytes={half}-")
            self.assertEqual(_header(headers, "If-Range"), '"etag-123"')
            dest = os.path.join(version_dir, "model.bin")
            with open(dest, "rb") as handle:
                self.assertEqual(handle.read(), payload)        # full file installed
            self.assertFalse(os.path.exists(part))              # consumed atomically
        finally:
            harness.close()

    def test_tampered_resume_fails_verification_and_never_installs(self):
        harness = StoreHarness({})
        payload = harness.base_payload
        half = len(payload) // 2
        store = harness.build()
        try:
            version_dir = os.path.join(store._store_dir, BASE_ID, "1")
            os.makedirs(version_dir, exist_ok=True)
            part = os.path.join(version_dir, "model.bin.part")
            with open(part, "wb") as handle:
                handle.write(b"\x00" * half)                    # CORRUPTED prefix
            # only ONE mirror: corrupt resume -> reset -> source exhausted -> fail
            harness.files_by_url = {
                "https://primary.example/base/model.bin":
                    lambda request: FakeResponse(payload[half:], status=206)}
            with mock.patch("urllib.request.urlopen", side_effect=harness.opener), \
                    mock.patch.object(model_store.logger, "warning"):
                with self.assertRaises(model_store._StoreError) as ctx:
                    store._download_one(BASE_ID)
            self.assertEqual(ctx.exception.state, "failed")
            self.assertFalse(os.path.exists(os.path.join(version_dir, "model.bin")))
            self.assertFalse(os.path.exists(part))              # corrupt part reset
        finally:
            harness.close()

    def test_corrupt_resume_falls_back_to_the_next_mirror(self):
        payload = b"B" * 1500
        half = len(payload) // 2
        primary = "https://primary.example/base/model.bin"
        mirror = "https://mirror.example/base/model.bin"
        entry = {"model_id": BASE_ID, "version": 1,
                 "files": [{"path": "model.bin",
                            "url": [primary, mirror],       # ordered mirrors (H4)
                            "sha256": _sha(payload),
                            "size": len(payload)}]}
        harness = StoreHarness({}, manifest_files=[entry])
        store = harness.build()
        try:
            version_dir = os.path.join(store._store_dir, BASE_ID, "1")
            os.makedirs(version_dir, exist_ok=True)
            part = os.path.join(version_dir, "model.bin.part")
            with open(part, "wb") as handle:
                handle.write(b"\xff" * half)                    # corrupt prefix
            # primary serves the ranged tail (-> corrupt combine -> mismatch ->
            # reset), then the ordered mirror serves the full file fresh (H4)
            harness.files_by_url = {
                primary: lambda request: FakeResponse(payload[half:], status=206),
                mirror: payload}
            with mock.patch("urllib.request.urlopen", side_effect=harness.opener), \
                    mock.patch.object(model_store.logger, "warning"):
                store._download_one(BASE_ID)
            urls = [url for url, _h in harness.requests]
            self.assertEqual(urls, [primary, mirror])           # ordered fallback
            with open(os.path.join(version_dir, "model.bin"), "rb") as handle:
                self.assertEqual(handle.read(), payload)
        finally:
            harness.close()


class BoundedMirrorRetryTests(unittest.TestCase):
    def test_transient_failures_retry_each_source_three_times_then_next(self):
        payload = b"A" * 1000
        primary = "https://primary.example/a.bin"
        mirror = "https://mirror.example/a.bin"
        entry = {"model_id": "m", "version": 1,
                 "files": [{"path": "f.bin", "url": [primary, mirror],
                            "sha256": _sha(payload), "size": len(payload)}]}
        harness = StoreHarness({}, manifest_files=[entry])
        store = harness.build()

        def refuse(_request):
            raise urllib.error.URLError("scripted transient")

        try:
            version_dir = os.path.join(store._store_dir, "m", "1")
            os.makedirs(version_dir, exist_ok=True)
            harness.files_by_url = {primary: refuse, mirror: payload}
            with mock.patch("urllib.request.urlopen", side_effect=harness.opener), \
                    mock.patch.object(model_store.logger, "warning"):
                store._download_one("m")
            urls = [url for url, _h in harness.requests]
            self.assertEqual(urls, [primary] * model_store.SOURCE_TRIES + [mirror])
            self.assertEqual(model_store.SOURCE_TRIES, 3)       # bounded (annex)
            self.assertTrue(os.path.isfile(os.path.join(version_dir, "f.bin")))
        finally:
            harness.close()

    def test_all_sources_exhausted_fails_loudly_with_no_install(self):
        harness = StoreHarness({})
        store = harness.build()

        def refuse(_request):
            raise urllib.error.URLError("all down")

        try:
            harness.files_by_url = {"https://primary.example/base/model.bin": refuse,
                                    "https://primary.example/silero/silero_vad.onnx":
                                    refuse}
            with mock.patch("urllib.request.urlopen", side_effect=harness.opener), \
                    mock.patch.object(model_store.logger, "warning"):
                with self.assertRaises(model_store._StoreError) as ctx:
                    store._download_one(BASE_ID)
            self.assertEqual(ctx.exception.state, "failed")
            self.assertIn("any mirror", ctx.exception.detail)
        finally:
            harness.close()


class DiskPreflightTests(unittest.TestCase):
    def test_insufficient_disk_fails_before_the_first_byte(self):
        harness = StoreHarness({}, disk_free=0)                 # nothing free
        store = harness.build()
        try:
            with mock.patch("urllib.request.urlopen", side_effect=harness.opener):
                with self.assertRaises(model_store._StoreError) as ctx:
                    store._download_one(BASE_ID)
            self.assertEqual(ctx.exception.state, "disk_error")
            self.assertIn("Not enough disk space", ctx.exception.detail)
            self.assertEqual(harness.requests, [])              # BEFORE any byte
        finally:
            harness.close()

    def test_margin_of_twenty_percent_is_required(self):
        self.assertEqual(model_store.DISK_MARGIN, 1.2)
        harness = StoreHarness({})                              # real payload size
        needed = int(len(harness.base_payload) * model_store.DISK_MARGIN)
        harness.disk_free = needed - 1                          # just short of 120%
        store = harness.build()
        try:
            with mock.patch("urllib.request.urlopen", side_effect=harness.opener):
                with self.assertRaises(model_store._StoreError) as ctx:
                    store._download_one(BASE_ID)
            self.assertEqual(ctx.exception.state, "disk_error")
            self.assertEqual(harness.requests, [])
            # exactly enough free space passes the preflight
            harness2 = StoreHarness({})
            harness2.disk_free = int(len(harness2.base_payload) *
                                     model_store.DISK_MARGIN)
            store2 = harness2.build()
            try:
                with mock.patch("urllib.request.urlopen",
                                side_effect=harness2.opener):
                    store2._download_one(BASE_ID)
                self.assertTrue(os.path.isfile(os.path.join(
                    store2._store_dir, BASE_ID, "1", "model.bin")))
            finally:
                harness2.close()
        finally:
            harness.close()


class PlaceholderDigestTests(unittest.TestCase):
    def test_placeholder_zero_digest_fails_closed_before_any_fetch(self):
        harness = StoreHarness({}, placeholder=True)
        store = harness.build()
        try:
            self.assertIsNotNone(store._manifest)              # manifest loaded
            with mock.patch("urllib.request.urlopen", side_effect=harness.opener):
                with self.assertRaises(model_store._StoreError) as ctx:
                    store._download_one(BASE_ID)
            self.assertEqual(ctx.exception.state, "failed")
            self.assertIn("not release-pinned", ctx.exception.detail)
            self.assertEqual(harness.requests, [])             # zero network (G3)
        finally:
            harness.close()

    def test_unmanifested_model_fails_closed(self):
        harness = StoreHarness({})
        store = harness.build()
        try:
            with self.assertRaises(model_store._StoreError) as ctx:
                store._download_one("not-in-manifest")
            self.assertIn("manifest", ctx.exception.detail)
        finally:
            harness.close()


class EvictionTests(unittest.TestCase):
    def test_activation_keeps_the_active_and_newest_other_version_only(self):
        harness = StoreHarness({})
        store = harness.build()
        try:
            root = os.path.join(store._store_dir, BASE_ID)
            for name, stamp in (("1", 1000), ("2", 2000), ("3", 3000)):
                path = os.path.join(root, name)
                os.makedirs(path, exist_ok=True)
                with open(os.path.join(path, "model.bin"), "wb") as handle:
                    handle.write(harness.base_payload)          # already installed
                os.utime(path, (stamp, stamp))
            with mock.patch("urllib.request.urlopen", side_effect=harness.opener):
                store._download_one(BASE_ID)                    # -> _activate
            self.assertTrue(os.path.isdir(os.path.join(root, "1")))   # keep (active)
            self.assertTrue(os.path.isdir(os.path.join(root, "3")))   # newest OTHER
            self.assertFalse(os.path.isdir(os.path.join(root, "2")))  # evicted
        finally:
            harness.close()


class PartGcTests(unittest.TestCase):
    def test_gc_keeps_fresh_known_parts_and_removes_stale_or_unknown(self):
        harness = StoreHarness({})
        store = harness.build()
        try:
            version_dir = os.path.join(store._store_dir, BASE_ID, "1")
            os.makedirs(version_dir, exist_ok=True)
            fresh = os.path.join(version_dir, "model.bin.part")   # known + fresh
            stale = os.path.join(version_dir, "silero_vad.onnx.part")  # known + old
            unknown = os.path.join(version_dir, "rogue.bin.part")  # manifest-less
            for path in (fresh, stale, unknown):
                with open(path, "wb") as handle:
                    handle.write(b"partial")
            old = time.time() - (model_store.PART_STALE_DAYS + 1) * 86400
            os.utime(stale, (old, old))
            store._gc_stale_parts()
            self.assertTrue(os.path.exists(fresh))              # resume survives (R6)
            self.assertFalse(os.path.exists(stale))             # > 7 days -> gone
            self.assertFalse(os.path.exists(unknown))           # manifest-less -> gone
        finally:
            harness.close()


class RecheckTests(unittest.TestCase):
    def test_tampered_installed_file_is_marked_stale_and_reported(self):
        harness = StoreHarness({})
        store = harness.build()
        try:
            with mock.patch("urllib.request.urlopen", side_effect=harness.opener):
                store._download_one(BASE_ID)
                store._download_one(SILERO_ID)
            dest = os.path.join(store._store_dir, BASE_ID, "1", "model.bin")
            with open(dest, "wb") as handle:
                handle.write(b"TAMPERED-BYTES")                 # G3: on-disk tamper
            harness.save_calls.clear()
            pushed = []
            store._push_state = lambda state, detail, pct=0, label=None: \
                pushed.append((state, detail))
            store.re_check_file()
            self.assertEqual(store._app.config["local_models"][BASE_ID], "stale")
            self.assertEqual(store._app.config["local_models"][SILERO_ID], "ready")
            state, detail = pushed[-1]
            self.assertEqual(state, "failed")
            self.assertIn(BASE_ID, detail)
            self.assertIn("SHA-256", detail)
            self.assertTrue(any(cfg["local_models"].get(BASE_ID) == "stale"
                                for cfg in harness.save_calls))
        finally:
            harness.close()

    def test_healthy_files_recheck_to_ready(self):
        harness = StoreHarness({})
        store = harness.build()
        try:
            with mock.patch("urllib.request.urlopen", side_effect=harness.opener):
                store._download_one(BASE_ID)
                store._download_one(SILERO_ID)
            pushed = []
            store._push_state = lambda state, detail, pct=0, label=None: \
                pushed.append(state)
            store.re_check_file()
            self.assertEqual(pushed[-1], "ready")
        finally:
            harness.close()


class FullFlowTests(unittest.TestCase):
    def test_download_runs_both_wanted_models_to_ready(self):
        harness = StoreHarness({})
        store = harness.build()
        try:
            with mock.patch("urllib.request.urlopen", side_effect=harness.opener):
                store.download()
                self.assertTrue(wait_until(lambda: store._worker is None
                                           and store._state == "ready"),
                                f"never finished: {store._state} {store._detail}")
            states = []
            store._push_state = lambda state, detail, pct=0, label=None: \
                states.append(state)
            self.assertTrue(os.path.isfile(os.path.join(
                store._store_dir, BASE_ID, "1", "model.bin")))
            self.assertTrue(os.path.isfile(os.path.join(
                store._store_dir, SILERO_ID, "1", "silero_vad.onnx")))
        finally:
            harness.close()

    def test_download_is_single_flight(self):
        harness = StoreHarness({})
        store = harness.build()
        release = threading.Event()

        def blocking_run():
            release.wait(timeout=10)
            with store._lock:                             # mimic _run's finally
                store._worker = None
        try:
            store._run = blocking_run
            store.download()
            store.download()                                # second call: no-op
            self.assertIsNotNone(store._worker)
            # only ONE worker thread name exists among live threads
            workers = [thread for thread in threading.enumerate()
                       if thread.name == "model-store"]
            self.assertEqual(len(workers), 1)
            release.set()
            wait_until(lambda: store._worker is None)
        finally:
            release.set()
            harness.close()

    def test_cancel_mid_download_keeps_the_part_for_later_resume(self):
        harness = StoreHarness({})
        payload = harness.base_payload
        store = harness.build()
        first = {"flag": False}

        def cancel_after_first_read(request):
            def reader(response):
                if not first["flag"]:
                    first["flag"] = True
                    store.cancel_download()                  # user pressed Cancel
            return FakeResponse(payload, on_read=reader)
        try:
            harness.files_by_url = {"https://primary.example/base/model.bin":
                                    cancel_after_first_read,
                                    "https://primary.example/silero/silero_vad.onnx":
                                    harness.silero_payload}
            with mock.patch("urllib.request.urlopen", side_effect=harness.opener), \
                    mock.patch.object(model_store.logger, "warning"):
                store.download()
                self.assertTrue(wait_until(lambda: store._worker is None))
            self.assertEqual(store._state, "not_downloaded")
            part = os.path.join(store._store_dir, BASE_ID, "1", "model.bin.part")
            self.assertTrue(os.path.exists(part))            # kept for resume (A5A7)
        finally:
            harness.close()

    def test_missing_manifest_makes_download_a_safe_noop(self):
        harness = StoreHarness({})
        store = harness.build()
        try:
            store._manifest = None
            store.download()
            self.assertIsNone(store._worker)                 # D7: never downloads blind
            self.assertEqual(harness.requests, [])
        finally:
            harness.close()

    def test_delete_refuses_while_a_session_is_active(self):
        harness = StoreHarness({})
        store = harness.build()
        try:
            store._app.dictation = type("D", (), {"is_active": True})()
            pushed = []
            store._push_state = lambda state, detail, pct=0, label=None: \
                pushed.append((state, detail))
            store.delete()
            self.assertEqual(pushed[-1], ("failed", "In use by the active session."))
        finally:
            harness.close()


class CopyContractTests(unittest.TestCase):
    def test_state_copy_templates_are_the_approved_a5a7_strings(self):
        self.assertEqual(model_store.COPY_DISK.format(need=1, free=2, path="p"),
                         "Not enough disk space: 1 MB needed, 2 MB available on p.")
        self.assertIn("SHA-256", model_store.COPY_VERIFYING)
        self.assertIn("Nothing was installed", model_store.COPY_FAILED)
        self.assertIn("Nothing was installed", model_store.COPY_CANCELLED)
        self.assertIn("Resume", model_store.COPY_PAUSED)


if __name__ == "__main__":
    unittest.main()
