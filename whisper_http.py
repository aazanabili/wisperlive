"""Groq ``POST /audio/transcriptions`` transport (multipart, stdlib urllib).

WHY a separate transport (A6 s3b): ``llm_api`` owns the chat-completions wire
and sits AT the 350-line gate, and multipart/form-data is a different wire
shape — the PURE body comes from ``llm_payloads.build_multipart_body`` and
this module owns only the bounded send + ``verbose_json`` parse (V1 verified:
``file``/``model``/``language`` ISO-639-1, ``response_format=verbose_json``,
``timestamp_granularities[]=word`` -> ``.text`` + ``.words[].word/.start/.end``).

Policy:
* H6: NO auto-retry on this billable send — the UI is the retry control.
* H4/D9: endpoint validated (https, no userinfo/query/fragment); log lines get
  scheme+host+path ONLY (``_safe_url_for_log``).
* 429 -> ``LLMQuotaError`` with a reset hint when the vendor supplies a
  parseable one; header-less 429 keeps the legacy generic copy (A3 s4/Q5).
* Bounded reads under a total deadline (H5 pattern) — no stream can outlive
  the timeout by trickling.  (llm_http extraction is deferred: llm_api is not
  owned this phase, so the ~15-line read loop is deliberately mirrored.)
"""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Tuple

from dictation_types import WordSpan
from llm_errors import LLMAuthError, LLMConfigError, LLMError, LLMNetworkError, LLMQuotaError
from llm_payloads import build_multipart_body

logger = logging.getLogger(__name__)

ENGINE_ID = "groq-whisper"
DEFAULT_MODEL = "whisper-large-v3-turbo"     # A3 R2 registry constant (V1 verified)
DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
TRANSCRIPTION_TIMEOUT = 120
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
RATE_LIMIT_COPY = ("The provider is rate-limiting requests. "
                   "Please wait a moment and try again.")


@dataclass(frozen=True)
class Transcript:
    """Parsed ``verbose_json`` result.  ``text`` is transcript-bearing (D11:
    hidden from repr); word timings are ``WordSpan``s in MILLISECONDS."""

    text: str = field(repr=False)
    words: Tuple[WordSpan, ...] = ()


def _safe_url_for_log(url: str) -> str:
    """Scheme + host + path only — never credentials, query, or fragment (H4/D9)."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return f"{parts.scheme}://{host}{parts.path}"
    except ValueError:
        return "<redacted url>"


def _endpoint(base_url, suffix) -> str:
    """Validate the base and join the path (H4: no credentials/query/fragment,
    https-only — Groq is a public cloud endpoint, never a loopback proxy)."""
    base = (base_url or "").strip() or DEFAULT_BASE_URL
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme != "https" or not (parsed.hostname or ""):
        raise LLMConfigError(
            f"Provider Base URL is invalid: {base!r}. Expected a URL like {DEFAULT_BASE_URL}"
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LLMConfigError(
            "Provider Base URL must not contain credentials or a query string. "
            "Put the API key in the API key field."
        )
    return base.rstrip("/") + suffix


def _read_body(response, timeout: int, description: str) -> bytes:
    """Bounded read under a TOTAL deadline (H5) — every recv is itself socket-
    bounded, so total wait <= timeout + one read (same contract as llm_api)."""
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
            break
    return b"".join(chunks)


def _reset_hint(error) -> str:
    """Best-effort 'HH:MM' from 429 headers (Retry-After seconds or an
    epoch-style x-ratelimit-reset-*); '' when nothing parseable — never guessed."""
    seconds = None
    retry_after = error.headers.get("Retry-After") if error.headers else None
    if retry_after is not None:
        try:
            seconds = max(0, int(str(retry_after).strip()))
        except ValueError:
            seconds = None
    if seconds is None and error.headers:
        for name, value in error.headers.items():
            if name.lower().startswith("x-ratelimit-reset"):
                try:
                    stamp = int(str(value).strip())
                except ValueError:
                    continue
                seconds = max(0, stamp - int(time.time())) if stamp > 10**9 else stamp
                break
    if seconds is None:
        return ""
    return datetime.fromtimestamp(time.time() + seconds).strftime("%H:%M")


def transcribe_wav(wav_bytes: bytes, *, api_key: str = "", base_url: str = "",
                   model: str = "", language: str = "", timeout: int = TRANSCRIPTION_TIMEOUT,
                   session=None) -> Transcript:
    """Send one in-memory WAV to ``/audio/transcriptions`` (multipart, V1).

    ``language``: ISO-639-1 or "" for auto (already mapped by the caller).
    ``session``: optional INJECTED opener for tests (defaults to
    ``urllib.request.urlopen``).  No retry (H6); typed errors carry user-safe
    copy only (D11 — no transcript, no key material).
    """
    if not isinstance(wav_bytes, (bytes, bytearray)) or not wav_bytes:
        raise LLMError("The recording could not be read. Please try again.")
    url = _endpoint(base_url, "/audio/transcriptions")
    boundary = uuid.uuid4().hex
    fields = [("model", (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL),
              ("response_format", "verbose_json"),
              ("timestamp_granularities[]", "word")]
    if language:
        fields.append(("language", language))
    body = build_multipart_body(boundary, fields,
                                ("file", "segment.wav", "audio/wav", bytes(wav_bytes)))
    headers = {"Accept": "application/json", "Content-Type": f"multipart/form-data; boundary={boundary}"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    open_url = session if session is not None else urllib.request.urlopen
    try:
        with open_url(request, timeout=timeout) as response:
            raw = _read_body(response, timeout, "transcription")
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise LLMAuthError(
                f"Authentication failed (HTTP {error.code}). Check the Groq API key in Settings."
            ) from error
        if error.code == 429:
            hint = _reset_hint(error)
            if hint:
                # D6 typed detection: engine + reset time for the offering UI.
                raise LLMQuotaError(
                    f"Groq is rate-limiting transcription. Paused until {hint}.",
                    engine_id=ENGINE_ID, reset_hint=hint,
                ) from error
            raise LLMError(RATE_LIMIT_COPY) from error     # header-less: legacy copy (A3 s4)
        if error.code >= 500:
            raise LLMError(
                f"The provider is having a temporary problem (HTTP {error.code}). Please try again."
            ) from error
        raise LLMError(f"The provider returned HTTP {error.code} for the transcription request.") from error
    except urllib.error.URLError as error:
        reason = getattr(error, "reason", error)
        if isinstance(reason, TimeoutError):
            raise LLMNetworkError(
                f"The provider timed out after {timeout} seconds. Check the URL and network."
            ) from error
        raise LLMNetworkError(f"Could not reach the provider: {reason}") from error
    except TimeoutError as error:
        raise LLMNetworkError(
            f"The provider timed out after {timeout} seconds. Check the URL and network."
        ) from error
    except OSError as error:
        raise LLMNetworkError(f"Could not reach the provider: {error}") from error
    logger.info("Groq transcription ok (%d bytes) from %s",  # sizes/hosts only (D11)
                len(wav_bytes), _safe_url_for_log(url))
    return _parse_verbose_json(raw)


def _parse_verbose_json(raw: bytes) -> Transcript:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise LLMError("The provider returned an unexpected (non-JSON) response.")
    text = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(text, str):
        raise LLMError("The provider returned an unexpected transcription format.")
    words = []
    for entry in (payload.get("words") or []) if isinstance(payload, dict) else []:
        if not isinstance(entry, dict):
            continue
        token = entry.get("word")
        start = entry.get("start")
        end = entry.get("end")
        if isinstance(token, str) and isinstance(start, (int, float)) and isinstance(end, (int, float)):
            # Vendor seconds -> WordSpan milliseconds (A3 s2.3 contract).
            words.append(WordSpan(start_ms=int(start * 1000), end_ms=int(end * 1000),
                                  text=token))
    return Transcript(text=text.strip(), words=tuple(words))
