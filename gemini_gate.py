"""Unified Gemini consent gate — the ONE U2/F-2 rule for Gemini-bound traffic.

WHY: the red ``gemini_consent`` toggle (Settings → Engines & keys) OR the
explicit ``gemini_paid_key_confirmed`` attestation must gate EVERY request
whose DESTINATION is a Google Gemini endpoint — the Gemini Live engine, the
``google-gemini`` chunked preset, and the GENERAL provider path (stage-1
transcription and cloud cleanup, incl. "Polish last dictation") — not only the
dictation engines (SecOps F-2 / HN-DICT-04; user decision: gate BOTH paths).

Fail-closed (default OFF) and coercion-proof (M20 literal ``True``):

* :func:`gemini_consent_ok` — the ONE consent predicate; accepts a live config
  ``Mapping`` or the D10 ``SessionConfig`` snapshot (``gemini_enabled``).
* :func:`routes_to_gemini` — the ENDPOINT rule: the ``google-gemini`` preset id
  (or its display label) OR a base URL on a Gemini host — hand-edited Custom
  URLs included.
* :func:`require_gemini_consent` / :func:`require_gemini_route` — raise a typed
  ``ConsentRequiredError`` BEFORE any network I/O (preflight + engines).
* :func:`save_route` — the settings save-blocker decision token, rendered to
  copy by ``settings_dictation_view.validate_save``.

Pure policy leaf: imports only the error taxonomy + the provider registry —
no I/O, no tkinter, no transport, and no URL/key material in the copy (D11).
"""

from typing import Mapping

from llm_errors import ConsentRequiredError
from providers import provider_id_from_display, resolve_provider_id

GEMINI_PROVIDER_ID = "google-gemini"     # providers registry preset id
GEMINI_ENGINE_ID = "gemini-live"         # live WS engine id (U2)
# Endpoint rule (F-2): the registry endpoint host and its subdomains — a
# Custom base URL on the same host is Gemini-bound traffic too.
_GEMINI_HOSTS = ("generativelanguage.googleapis.com",)
_CONSENT_MESSAGE = ("Google Gemini stays disabled until you accept the data notice "
                    "in Settings (or confirm a paid key).")


def _host_of(base_url) -> str:
    """Lowercase hostname of a base URL ('' for a non-string); scheme, port,
    userinfo, path, query and fragment are stripped — no URL library needed."""
    if not isinstance(base_url, str):
        return ""
    host = base_url.strip().lower().split("//", 1)[-1]
    host = host.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    return host.split("@")[-1].split(":", 1)[0]


def _provider_id(value) -> str:
    """Registry id for a preset id OR its display label; '' for a non-string."""
    if not isinstance(value, str):
        return ""
    return provider_id_from_display(value) or resolve_provider_id(value)


def gemini_consent_ok(source) -> bool:
    """The ONE consent predicate: literal ``True`` on EITHER flag (M20), from a
    live config ``Mapping`` or the D10 snapshot's ``gemini_enabled``."""
    snapshot = getattr(source, "gemini_enabled", None)      # SessionConfig (D10)
    if isinstance(snapshot, bool):
        return snapshot
    route = source if isinstance(source, Mapping) else {}
    return (route.get("gemini_consent") is True
            or route.get("gemini_paid_key_confirmed") is True)


def routes_to_gemini(provider_id="", base_url="") -> bool:
    """F-2 ENDPOINT rule: True when traffic is destined for a Gemini endpoint."""
    if _provider_id(provider_id) == GEMINI_PROVIDER_ID:
        return True
    host = _host_of(base_url)
    return bool(host) and any(host == bare or host.endswith("." + bare)
                              for bare in _GEMINI_HOSTS)


def _route_endpoint(route) -> tuple:
    """``(provider_id, base_url)`` from a live config ``Mapping`` or a snapshot."""
    if isinstance(route, Mapping):
        return route.get("provider"), route.get("provider_base_url")
    return getattr(route, "provider_id", ""), getattr(route, "provider_base_url", "")


def require_gemini_consent(source, engine_id: str = GEMINI_PROVIDER_ID) -> None:
    """U2/F-2 fail-closed BEFORE any network: consent OR paid-key (M20)."""
    if not gemini_consent_ok(source):
        raise ConsentRequiredError(_CONSENT_MESSAGE, engine_id=engine_id)


def require_gemini_route(route) -> None:
    """F-2: raise BEFORE any request whose destination is a Gemini endpoint —
    the general provider path (transcription and/or cloud cleanup/Polish)."""
    provider_id, base_url = _route_endpoint(route)
    if routes_to_gemini(provider_id, base_url):
        require_gemini_consent(route, GEMINI_PROVIDER_ID)


def save_route(live_engine, provider_values, keys_values) -> str:
    """Settings save-blocker decision: ``"live"`` (the Gemini live engine),
    ``"provider"`` (a Gemini-bound provider route), or ``""`` (save may run).
    Never raises — the view renders the matching copy constant."""
    if gemini_consent_ok(keys_values):
        return ""
    if live_engine == GEMINI_ENGINE_ID:
        return "live"
    provider_id, base_url = _route_endpoint(provider_values)
    return "provider" if routes_to_gemini(provider_id, base_url) else ""
