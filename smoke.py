"""``--smoke`` self-check (R-8c; devops annex U.3 clean-VM intent).

Import health + pinned manifest + provider registry + optional-ML status with
a strict core and OPTIONAL probes — zero Tk, zero network, zero downloads,
exit 0 = healthy / 1 = failed.  Run ``python main.py --smoke`` (dispatched in
``main.__main__`` BEFORE any Tk init or instance lock) or ``python smoke.py``.

Strict checks (failure -> exit 1): core module imports, ``assets/models_manifest.json``
parse via ``model_manifest`` (schema + NO placeholder zero-digests), provider registry.
Optional probes run ONLY when the extras AND their data already exist (absent
extras are REPORTED as SKIP — LOCAL/cloud-live stay optional, nothing installs).
"""

import importlib
import importlib.util
import io
import json
import os
import sys

CORE_MODULES = (
    "providers", "config_manager", "llm_errors", "llm_api", "llm_payloads",
    "dictation_types", "session_state", "quota_ledger", "model_manifest",
    "pcm_stream", "vad_segmenter", "local_engine", "chunked_engine", "whisper_http",
    "typer_actor", "focus_guard", "polish_engine", "model_store",
    "live_local_engine", "live_cloud_engine", "dictation_session",
)

_ML_EXTRAS = ("faster_whisper", "ctranslate2", "onnxruntime", "websocket", "av",
              "tokenizers", "huggingface_hub")


def _asset_path(*parts) -> str:
    """Repo/frozen-aware asset path (PyInstaller ships data under ``_MEIPASS``)."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, *parts)


def run() -> int:
    checks = failures = 0

    def report(status: str, label: str, detail: str = "") -> None:
        nonlocal checks, failures
        checks += 1
        if status == "FAIL":
            failures += 1
        print(f"[{status}] {label}" + (f" — {detail}" if detail else ""))

    # --- STRICT: core module import health (no Tk, no recorder, no threads) ---
    for name in CORE_MODULES:
        try:
            importlib.import_module(name)
            report("OK", f"import {name}")
        except Exception as error:
            report("FAIL", f"import {name}", f"{type(error).__name__}: {error}")

    # --- STRICT: in-binary model manifest parses and every digest is real -----
    try:
        from model_manifest import parse_manifest
        with open(_asset_path("assets", "models_manifest.json"), encoding="utf-8") as handle:
            manifest = parse_manifest(json.load(handle))
        placeholders = [entry.path for artifact in manifest.artifacts
                        for entry in artifact.files if set(entry.sha256) == {"0"}]
        if placeholders:
            report("FAIL", "models_manifest pins", f"placeholder digests: {placeholders}")
        else:
            report("OK", "models_manifest pins",
                   f"{len(manifest.artifacts)} models, "
                   f"{sum(len(a.files) for a in manifest.artifacts)} sha256-pinned files")
    except Exception as error:
        report("FAIL", "models_manifest pins", f"{type(error).__name__}: {error}")

    # --- STRICT: provider registry (incl. the key-optional local endpoint) ----
    try:
        import providers
        healthy = (providers.is_known_provider("openai")
                   and providers.is_known_provider("ollama-local")
                   and not providers.needs_api_key("ollama-local"))
        report("OK" if healthy else "FAIL", "providers registry",
               "presets load; ollama-local is key-optional" if healthy
               else "expected presets missing or ollama-local requires a key")
    except Exception as error:
        report("FAIL", "providers registry", f"{type(error).__name__}: {error}")

    # --- OPTIONAL: ML extras status (absent = SKIP, never a failure) ----------
    for package in _ML_EXTRAS:
        present = importlib.util.find_spec(package) is not None
        report("OK" if present else "SKIP", f"optional {package}",
               "" if present else "not installed (extras stay optional)")

    # --- OPTIONAL probe: silero ONNX session, only when data is installed -----
    if importlib.util.find_spec("onnxruntime") is None:
        report("SKIP", "silero ONNX probe", "onnxruntime not installed (optional)")
    else:
        candidates = [_asset_path("assets", "silero_vad.onnx")]
        try:
            from local_engine import default_store_dir
            candidates.append(os.path.join(default_store_dir(""), "silero_vad.onnx"))
        except Exception:
            pass                                   # store-dir helper failed: repo path still tried
        silero = next((path for path in candidates if os.path.isfile(path)), None)
        if silero is None:
            report("SKIP", "silero ONNX probe", "silero_vad.onnx not installed (Settings > Models)")
        else:
            try:
                import onnxruntime
                session = onnxruntime.InferenceSession(silero, providers=["CPUExecutionProvider"])
                report("OK", "silero ONNX probe", f"{len(session.get_inputs())} input(s) loaded")
            except Exception as error:
                report("FAIL", "silero ONNX probe", f"{type(error).__name__}: {error}")

    # --- OPTIONAL probe: local faster-whisper decode, never downloads (D7) ----
    if importlib.util.find_spec("faster_whisper") is None:
        report("SKIP", "faster-whisper probe", "not installed (optional)")
    else:
        try:
            from faster_whisper import WhisperModel
            from local_engine import BASE_MODEL_ID, default_store_dir, find_model_dir
            from pcm_stream import pcm16_to_wav_bytes
            directory = find_model_dir(default_store_dir(""), BASE_MODEL_ID)
            if directory is None:
                report("SKIP", "faster-whisper probe",
                       "local model not downloaded (Settings > Models)")
            else:
                model = WhisperModel(directory, device="cpu", compute_type="int8")
                try:
                    silence = io.BytesIO(pcm16_to_wav_bytes(b"\x00\x00" * 16000))
                    list(model.transcribe(silence)[0])       # 1 s silence, generator consumed
                    report("OK", "faster-whisper probe", "1 s silence decoded locally")
                finally:
                    model.close()
        except Exception as error:
            report("FAIL", "faster-whisper probe", f"{type(error).__name__}: {error}")

    print(f"smoke: {'FAIL' if failures else 'PASS'} — {checks} checks, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
