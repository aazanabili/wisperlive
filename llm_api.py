"""OpenAI-compatible LLM provider client.

Speaks the standard OpenAI REST surface (``GET /models`` and
``POST /chat/completions``) so any compatible endpoint works: OpenAI,
Gemini's OpenAI compatibility layer, local Ollama/vLLM servers, or custom
proxies.  Only the standard library is used, and every request is bounded
by an explicit timeout so an unreachable or slow provider surfaces as a
normal error instead of hanging the caller.
"""

import base64
import json
import logging
import urllib.error
import urllib.request

from version import APP_VERSION

logger = logging.getLogger(__name__)

USER_AGENT = f"WhisperLive/{APP_VERSION}"

MODELS_TIMEOUT = 15
GENERATION_TIMEOUT = 120
# Refuse to buffer unbounded provider responses (a runaway endpoint must not
# defeat the request timeout by streaming forever).
MAX_RESPONSE_BYTES = 16 * 1024 * 1024

_PROMPT_TEMPLATE = """
        استمع إلى هذا المقطع الصوتي. قم بتفريغه إلى نص بدقة.
        أكمل الكلمات الناقصة وصحح الأخطاء وصغ التعبيرات لتكون واضحة ومفهومة وسلسة.
        بعد ذلك، قم بترجمة النص النهائي إلى اللغة '{target_language}'.
        أعطني النص النهائي المترجم والمنسق فقط بدون أي مقدمات أو شروحات إضافية.
        """


class LLMError(RuntimeError):
    """A provider failure with a message safe to show in the UI."""


class LLMConfigError(LLMError):
    """Missing or invalid local configuration (URL, model, ...)."""


class LLMAuthError(LLMError):
    """The provider rejected the API key (HTTP 401/403)."""


class LLMNetworkError(LLMError):
    """The provider could not be reached, timed out, or dropped the link."""


def _endpoint(base_url, suffix):
    base = (base_url or "").strip()
    if not base:
        raise LLMConfigError("Provider Base URL is not set. Open Settings and configure the provider.")
    scheme, separator, rest = base.partition("://")
    if not separator or scheme.lower() not in ("http", "https") or not rest.strip():
        raise LLMConfigError(
            f"Provider Base URL is invalid: {base_url!r}. Expected a URL like https://api.openai.com/v1"
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


def _request_json(request, timeout, description):
    """Perform a bounded request and return the decoded JSON body."""
    logger.info("Requesting %s from %s", description, request.full_url)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise LLMAuthError(
                f"Authentication failed (HTTP {error.code}). Check the API key in Settings."
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

    if len(body) > MAX_RESPONSE_BYTES:
        raise LLMError(f"The provider response for the {description} request is too large.")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise LLMError(f"The provider returned an unexpected (non-JSON) response for the {description} request.")


def fetch_models(base_url, api_key="", timeout=MODELS_TIMEOUT):
    """Return the sorted, de-duplicated model ids offered by the provider."""
    request = _build_request(_endpoint(base_url, "/models"), api_key)
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


def process_audio(audio_path, target_language, api_key="", base_url="", model="", timeout=GENERATION_TIMEOUT):
    """Transcribe, polish, and translate a recording via a chat completion.

    The audio is sent as a base64 WAV ``input_audio`` content part together
    with the transcription/translation instruction, which is the standard
    OpenAI chat-completions shape for audio input.
    """
    model_name = (model or "").strip()
    if not model_name:
        raise LLMConfigError("No model is selected. Open Settings, fetch models, and choose one.")
    url = _endpoint(base_url, "/chat/completions")
    with open(audio_path, "rb") as handle:
        audio_data = handle.read()
    payload = {
        "model": model_name,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _PROMPT_TEMPLATE.format(target_language=target_language)},
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": base64.b64encode(audio_data).decode("ascii"),
                            "format": "wav",
                        },
                    },
                ],
            }
        ],
    }
    response = _request_json(_build_request(url, api_key, payload), timeout, "generation")
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
