"""OpenAI-compatible LLM provider client.

Speaks the standard OpenAI REST surface (``GET /models`` and
``POST /chat/completions``) so any compatible endpoint works: OpenAI,
Gemini's OpenAI compatibility layer, local Ollama/vLLM servers, or custom
proxies.  Only the standard library is used, and every request is bounded
by an explicit timeout so an unreachable or slow provider surfaces as a
normal error instead of hanging the caller.

Recording processing is a two-stage pipeline:

1. ``transcribe_audio`` — audio in, raw rough transcript out (literal STT).
2. ``post_process_text`` — raw transcript in, clean final text out:
   intent-aware colloquial normalization, technical terms preserved in
   English, professional formatting, and translation to the target
   language only when one is selected.

``process_audio`` runs both stages back to back for callers that just want
the finished text.

Module boundary (plan ruling R6): this module owns TRANSPORT and pipeline
orchestration (HTTP, timeouts, retries, error taxonomy); pure wire-format
knowledge (prompts, dialects, builders, size bounds) lives in ``llm_payloads``.

Retry & key policy:

* **No auto-retry on completions (H6).** ``POST /chat/completions`` is
  non-idempotent — an automatic retry may bill the same work twice. The UI
  "try again" is the control. The idempotent ``GET /models`` in
  :func:`fetch_models` performs exactly ONE bounded retry on a network
  failure.
* **The API key is OPTIONAL (D2).** Local OpenAI-compatible providers
  (Ollama, llama.cpp, …) work keyless: when the key is empty the
  ``Authorization`` header is simply omitted.
"""

import base64
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

# Wire-format names are owned by llm_payloads (R6); the tags/prompts/MiMo bound are deliberate re-exports.
# Error taxonomy lives in llm_errors (pure leaf); every class is re-exported
# VERBATIM so `llm_api.LLMError` etc. keep exact class identity for existing
# imports/excepts and test pins (plan Step 1.4 / A3 T17) — never redefined here.
from llm_errors import (ConsentRequiredError, DictationError, EngineUnavailableError,
                        FocusLostError, LLMAuthError, LLMConfigError, LLMError,
                        LLMNetworkError, LLMQuotaError, ModelNotDownloadedError)
from llm_payloads import (DIALECT_MIMO_ASR, DIALECT_OPENAI, MIMO_MAX_AUDIO_BYTES,
                          _POST_PROCESS_PROMPT_TEMPLATE, _TRANSCRIBE_PROMPT,
                          _audio_size_limit, _build_transcription_payload)
from version import APP_VERSION

logger = logging.getLogger(__name__)

USER_AGENT = f"WhisperLive/{APP_VERSION}"

MODELS_TIMEOUT = 15
GENERATION_TIMEOUT = 120
# Refuse to buffer unbounded provider responses (a runaway endpoint must not
# defeat the request timeout by streaming forever).
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
# H5: the body is read in bounded chunks so its TOTAL duration can be
# deadline-checked (a per-socket timeout alone only bounds single recvs).
READ_CHUNK_BYTES = 64 * 1024
# H1: refuse absurd recordings before base64-encoding them into a request.
# Generic 20 MiB cap = the OpenAI-dialect entry of llm_payloads' size table.
MAX_AUDIO_BYTES = _audio_size_limit(DIALECT_OPENAI)[0]

# Sentinel output language: clean the transcript but do not translate it.
NO_TRANSLATION = "Original language"

# NOTE: the LLM* error classes moved to llm_errors (pure) and are re-exported
# through the import above — see its docstrings for the full hierarchy.


def _safe_url_for_log(url: str) -> str:
    """Return scheme + host + path only: drop userinfo, query, and fragment.

    Custom providers accept ``?key=...`` style URLs — the log must never
    contain a credential (H3, app-wide privacy policy).
    """
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return f"{parts.scheme}://{host}{parts.path}"
    except ValueError:  # malformed userinfo/port — never echo it back
        return "<redacted url>"


def _endpoint(base_url, suffix):
    base = (base_url or "").strip()
    if not base:
        raise LLMConfigError("Provider Base URL is not set. Open Settings and configure the provider.")
    scheme, separator, rest = base.partition("://")
    if not separator or scheme.lower() not in ("http", "https") or not rest.strip():
        raise LLMConfigError(
            f"Provider Base URL is invalid: {base_url!r}. Expected a URL like https://api.openai.com/v1"
        )
    # H4: credentials/query in a base URL are both a secret-leak vector and a
    # correctness bug (string concatenation would corrupt query-bearing URLs).
    parsed = urllib.parse.urlsplit(base)
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise LLMConfigError(
            "Provider Base URL must not contain credentials or a query string. Put the API key in the API key field."
        )
    return base.rstrip("/") + suffix


def _build_request(url, api_key, payload=None):
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    return urllib.request.Request(url, data=data, headers=headers)


def _read_body(response, timeout, description):
    """Read the response body under a total wall-clock deadline (H5).

    ``urlopen(timeout=...)`` bounds each blocking socket operation, but a
    slow-trickle response could otherwise outlive ``timeout`` in aggregate.
    Honest bound: total wait <= ``timeout`` + one socket read (every recv is
    itself already bounded by the socket timeout). End of body is an empty
    read OR a short read: ``http.client.HTTPResponse.read(amt)`` returns
    fewer than ``amt`` bytes only at end-of-body (verified for both the
    content-length and chunked paths).
    """
    deadline = time.monotonic() + timeout
    chunks = []
    total = 0
    while True:
        if time.monotonic() > deadline:
            raise LLMNetworkError(
                f"The provider timed out after {timeout} seconds. Check the URL and network."
            )
        chunk = response.read(READ_CHUNK_BYTES)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise LLMError(f"The provider response for the {description} request is too large.")
        if len(chunk) < READ_CHUNK_BYTES:
            break  # end of body (short read)
    return b"".join(chunks)


def _request_json(request, timeout, description):
    """Perform a bounded request and return the decoded JSON body."""
    logger.info("Requesting %s from %s", description, _safe_url_for_log(request.full_url))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = _read_body(response, timeout, description)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise LLMAuthError(
                f"Authentication failed (HTTP {error.code}). Check the API key in Settings."
            ) from error
        if error.code == 429:
            raise LLMError(
                "The provider is rate-limiting requests. Please wait a moment and try again."
            ) from error
        if error.code >= 500:
            raise LLMError(
                f"The provider is having a temporary problem (HTTP {error.code}). Please try again."
            ) from error
        raise LLMError(f"The provider returned HTTP {error.code} for the {description} request.") from error
    except urllib.error.URLError as error:
        reason = getattr(error, "reason", error)
        if isinstance(reason, TimeoutError):
            raise LLMNetworkError(f"The provider timed out after {timeout} seconds. Check the URL and network.") from error
        raise LLMNetworkError(f"Could not reach the provider: {reason}") from error
    except TimeoutError as error:
        raise LLMNetworkError(f"The provider timed out after {timeout} seconds. Check the URL and network.") from error
    except OSError as error:
        raise LLMNetworkError(f"Could not reach the provider: {error}") from error

    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise LLMError(f"The provider returned an unexpected (non-JSON) response for the {description} request.")


def fetch_models(base_url, api_key="", timeout=MODELS_TIMEOUT):
    """Return the sorted, de-duplicated model ids offered by the provider.

    Idempotent GET: exactly ONE bounded retry on a network failure (H6).
    """
    try:
        request = _build_request(_endpoint(base_url, "/models"), api_key)
        try:
            payload = _request_json(request, timeout, "model list")
        except LLMNetworkError:
            # Safe to retry once: GET /models is idempotent and never billed.
            payload = _request_json(request, timeout, "model list")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise LLMError("The provider returned an unexpected model list format.")
        models = set()
        for item in data:
            model_id = item.get("id") if isinstance(item, dict) else item
            if isinstance(model_id, str) and model_id.strip():
                models.add(model_id.strip())
        return sorted(models)
    except LLMError:
        raise
    except Exception as error:
        # H2 boundary: always translate, never continue corrupted state.
        logger.exception("Unexpected error during model list request")
        raise LLMError("Something went wrong while contacting the provider.") from error


def _require_model(model):
    model_name = (model or "").strip()
    if not model_name:
        raise LLMConfigError("No model is selected. Open Settings, fetch models, and choose one.")
    return model_name


def _translation_instruction(target_language):
    """Return the stage-2 instruction for the optional translation step."""
    language = (target_language or "").strip()
    if not language or language.lower() == NO_TRANSLATION.lower():
        return "لا تترجم النص؛ أخرجه في لغة المتحدث الأصلية كما هي."
    return f"ترجم المخرج النهائي إلى اللغة '{language}'."


def _completion_text(response):
    """Extract the assistant text from a chat-completion response body."""
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise LLMError("The provider returned an unexpected response (no completion).")
    if isinstance(content, list):
        # Some providers return content as a list of typed parts.
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    if not isinstance(content, str) or not content.strip():
        raise LLMError("The provider returned an empty completion.")
    return content.strip()


def transcribe_audio(audio_path, api_key="", base_url="", model="", timeout=GENERATION_TIMEOUT,
                     *, dialect=DIALECT_OPENAI, target_language=""):
    """Stage 1 — transcribe a recording into a raw rough transcript.

    The audio is sent as a base64 WAV ``input_audio`` content part with a
    literal-transcription instruction — the standard OpenAI chat-completions
    shape (MiMo's dialect sends the vendor's Data-URL form; both built by
    llm_payloads).  ``target_language`` only feeds the MiMo ``asr_options``;
    no cleanup/translation happens here — that belongs to :func:`post_process_text`.
    """
    try:
        model_name = _require_model(model)
        url = _endpoint(base_url, "/chat/completions")
        # H1: file-read failures and absurd recordings surface as user-safe errors.
        # The dialect-specific bound resolves BEFORE the read — before base64 or network.
        size_limit, size_label = _audio_size_limit(dialect)
        try:
            with open(audio_path, "rb") as handle:
                audio_data = handle.read(size_limit + 1)
        except OSError as error:
            logger.warning("Audio file read failed: %s", type(error).__name__)
            raise LLMError("The recorded audio file could not be read. Please try again.") from error
        if len(audio_data) > size_limit:
            logger.warning("Audio file too large: %d bytes", len(audio_data))
            raise LLMError(
                f"The recording is too large to process (over {size_label}). Please record a shorter clip."
            )
        audio_b64 = base64.b64encode(audio_data).decode("ascii")
        payload = _build_transcription_payload(model_name, audio_b64, dialect, target_language)
        response = _request_json(_build_request(url, api_key, payload), timeout, "transcription")
        return _completion_text(response)
    except LLMError:
        raise
    except Exception as error:
        # H2 boundary: translate unknown failures without leaking internals.
        logger.exception("Unexpected error during transcription")
        raise LLMError("Something went wrong while contacting the provider.") from error


def post_process_text(raw_text, target_language, api_key="", base_url="", model="", timeout=GENERATION_TIMEOUT):
    """Stage 2 — clean up a raw transcript and optionally translate it.

    Understands speaker intent, normalizes colloquial phrasing and phonetic
    errors, keeps technical/product terms in English, and formats the result
    as clean prose or bullets.  Translation runs only when a target language
    is selected; otherwise the cleaned text stays in the spoken language.
    """
    try:
        model_name = _require_model(model)
        url = _endpoint(base_url, "/chat/completions")
        prompt = _POST_PROCESS_PROMPT_TEMPLATE.format(
            translation_instruction=_translation_instruction(target_language),
            raw_text=raw_text,
        )
        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": prompt}],
        }
        response = _request_json(_build_request(url, api_key, payload), timeout, "post-processing")
        return _completion_text(response)
    except LLMError:
        raise
    except Exception as error:
        # H2 boundary: a post-processing failure never falls back to the raw
        # transcript (pipeline invariant), it always surfaces as LLMError.
        logger.exception("Unexpected error during post-processing")
        raise LLMError("Something went wrong while contacting the provider.") from error


def process_audio(audio_path, target_language, api_key="", base_url="", model="", timeout=GENERATION_TIMEOUT,
                  *, dialect=DIALECT_OPENAI, transcription_model=""):
    """Run the full pipeline: transcribe a recording, then post-process it.

    Returns the final cleaned (and translated when selected) text.  The
    pipeline is sequential: a post-processing failure surfaces as a normal
    :class:`LLMError` instead of falling back to the distorted raw transcript.

    Split-mode presets (MiMo) pass stage 1 through ``transcription_model``
    (effective stage 1 = ``transcription_model or model``, validated by
    ``_require_model``); stage 2 always uses ``model``.  ``dialect`` picks
    stage 1's wire shape — both new args are keyword-only, positional calls unchanged.
    """
    stage_one_model = transcription_model or model
    raw_text = transcribe_audio(
        audio_path, api_key=api_key, base_url=base_url, model=stage_one_model,
        timeout=timeout, dialect=dialect, target_language=target_language,
    )
    return post_process_text(
        raw_text, target_language, api_key=api_key, base_url=base_url, model=model, timeout=timeout
    )
