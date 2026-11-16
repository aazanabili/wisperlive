"""Pure manifest schema + streaming SHA-256 verification (D7 supply chain).

WHY split from ``model_store``: parse/verify must be testable with ZERO I/O —
the store adapter (later phase) owns download/resume/atomic-install while THIS
module owns the pinned truth: ``assets/models_manifest.json`` (app-versioned,
never user-editable ``config.json``) and the incremental digest check.

Adopted schema (A10A11 s3, per A3 R2):

* document ``{"version": <int>, "models": [<entry>, ...]}`` (``version``
  defaults to 1 when absent; a bare list of entries is also accepted);
* entry ``{model_id, version, files: [{path, url: [<https, ...>], sha256,
  size}], hf_repo_sha?, license?, kind?}`` — mirrors ride in ``url`` in
  ORDER (primary first, then hf-mirror -> GitHub Release fallbacks, H4);
* format pins: ``sha256`` = 64 lowercase hex, ``hf_repo_sha`` = 40 hex,
  ``size > 0``, every URL https with no userinfo/query/fragment.

``parse_manifest`` RAISES ``ValueError`` on malformed input — engine-tier
callers translate it to a user-safe error (never-raise does NOT apply here:
a tampered manifest must fail loudly, never degrade into loading unverified
weights).

PURITY: stdlib only (``hashlib``/``hmac``/``re``/``dataclasses``/``typing``/
``urllib.parse``) — no file reads, no network; callers stream bytes in.
"""

import hashlib
import hmac
import re
import urllib.parse
from dataclasses import dataclass
from typing import Optional, Tuple

# Format pins (A3 R2): lowercase hex only — an uppercase/short digest never matches.
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_HF_REPO_SHA_HEX = re.compile(r"[0-9a-f]{40}")

KNOWN_KINDS = ("faster-whisper", "silero-vad")


def _is_sha256_hex(value) -> bool:
    return isinstance(value, str) and _SHA256_HEX.fullmatch(value) is not None


def _require_https_url(value, where: str) -> str:
    """H4 mirror rule: https only, host required, NO userinfo/query/fragment
    (credentials in a manifest URL are both a leak and a fetch-correctness bug)."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}: url must be a non-empty string")
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError as error:
        raise ValueError(f"{where}: malformed url") from error
    host = parts.hostname
    if parts.scheme != "https" or not host:
        raise ValueError(f"{where}: only absolute https urls are allowed")
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise ValueError(f"{where}: url must not contain credentials, query, or fragment")
    return value


def _require_relative_path(value, where: str) -> str:
    """Install-time traversal defense: manifest paths are RELATIVE file names
    (or relative subpaths) — never absolute, never ``..`` (A4)."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}: path must be a non-empty string")
    if value.startswith(("/", "\\")) or (len(value) > 1 and value[1] == ":"):
        raise ValueError(f"{where}: absolute paths are not allowed")
    segments = value.replace("\\", "/").split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise ValueError(f"{where}: relative paths with '.'/'..' segments are not allowed")
    return value


@dataclass(frozen=True)
class ModelFile:
    """ONE downloadable file of an artifact (A10A11 s3)."""

    path: str                               # install-relative target inside the model dir
    urls: Tuple[str, ...]                   # ordered: primary first, then mirrors
    sha256: str                             # pinned digest of the exact bytes
    size: int                               # expected byte size (> 0)


@dataclass(frozen=True)
class ModelArtifact:
    """ONE model entry of the manifest: files + provenance pins."""

    model_id: str
    version: int
    files: Tuple[ModelFile, ...]
    hf_repo_sha: str = ""                   # 40-hex Hugging Face repo pin when present
    license: str = ""
    kind: str = ""                          # "" | one of KNOWN_KINDS


@dataclass(frozen=True)
class ModelManifest:
    """The whole parsed ``models_manifest.json`` (never serialized from here)."""

    version: int
    artifacts: Tuple[ModelArtifact, ...]


def _parse_file(raw, where: str) -> ModelFile:
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: file entry must be an object")
    path = _require_relative_path(raw.get("path"), where)
    raw_urls = raw.get("url", raw.get("urls"))
    if not isinstance(raw_urls, list) or not raw_urls:
        raise ValueError(f"{where}: url must be a non-empty list (primary + mirrors)")
    urls = tuple(_require_https_url(url, f"{where} ({path})") for url in raw_urls)
    digest = raw.get("sha256")
    if not _is_sha256_hex(digest):
        raise ValueError(f"{where} ({path}): sha256 must be 64 lowercase hex chars")
    size = raw.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ValueError(f"{where} ({path}): size must be a positive integer")
    return ModelFile(path=path, urls=urls, sha256=digest, size=size)


def _parse_entry(raw, seen: set) -> ModelArtifact:
    if not isinstance(raw, dict):
        raise ValueError("models: each entry must be an object")
    model_id = raw.get("model_id")
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("models: model_id must be a non-empty string")
    if model_id in seen:
        raise ValueError(f"models: duplicate model_id {model_id!r}")
    seen.add(model_id)
    version = raw.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError(f"{model_id}: version must be a positive integer")
    files = raw.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError(f"{model_id}: files must be a non-empty list")
    parsed_files = tuple(_parse_file(entry, model_id) for entry in files)
    hf_repo_sha = raw.get("hf_repo_sha", "")
    if hf_repo_sha != "" and (not isinstance(hf_repo_sha, str)
                              or _HF_REPO_SHA_HEX.fullmatch(hf_repo_sha) is None):
        raise ValueError(f"{model_id}: hf_repo_sha must be 40 lowercase hex chars")
    license_name = raw.get("license", "")
    if not isinstance(license_name, str):
        raise ValueError(f"{model_id}: license must be a string")
    kind = raw.get("kind", "")              # optional (devops schema has no kind)
    if kind != "" and kind not in KNOWN_KINDS:
        raise ValueError(f"{model_id}: unknown kind {kind!r}")
    return ModelArtifact(model_id=model_id, version=version, files=parsed_files,
                         hf_repo_sha=hf_repo_sha, license=license_name, kind=kind)


def parse_manifest(data) -> ModelManifest:
    """Decoded JSON -> ``ModelManifest``; ``ValueError`` on ANY malformed pin
    (engine tier translates it — an unverified manifest never loads weights)."""
    if isinstance(data, list):              # bare entry list tolerated (version 1)
        raw_entries, version = data, 1
    elif isinstance(data, dict):
        raw_entries = data.get("models")
        version = data.get("version", 1)
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("manifest: version must be a positive integer")
    else:
        raise ValueError("manifest: expected an object or a list of model entries")
    if not isinstance(raw_entries, list):
        raise ValueError("manifest: 'models' must be a list")
    seen: set = set()
    artifacts = tuple(_parse_entry(entry, seen) for entry in raw_entries)
    return ModelManifest(version=version, artifacts=artifacts)


def find(manifest, model_id) -> Optional[ModelArtifact]:
    """Artifact by id (never raises: unknown id / garbage -> None, N1)."""
    if isinstance(manifest, ModelManifest) and isinstance(model_id, str):
        for artifact in manifest.artifacts:
            if artifact.model_id == model_id:
                return artifact
    return None


class DigestVerifier:
    """Streaming SHA-256 over downloaded bytes (D7: bytes are never trusted
    before verification; a mismatch deletes the temp file and the mirror chain
    tries the next url in ``ModelFile.urls``).

    Pure and incremental: ``DigestVerifier().update(chunk)...`` as bytes
    arrive; ``matches`` compares in constant time (A9) and rejects a malformed
    expectation instead of accidentally matching it.
    """

    def __init__(self, expected_sha256: str = "") -> None:
        self._digest = hashlib.sha256()
        self._expected = expected_sha256
        self._byte_count = 0

    @property
    def byte_count(self) -> int:
        """Bytes fed so far — lets the caller verify ``ModelFile.size`` too."""
        return self._byte_count

    def update(self, chunk) -> "DigestVerifier":
        if isinstance(chunk, (bytes, bytearray, memoryview)):
            self._digest.update(chunk)
            self._byte_count += len(chunk)
        return self

    def matches(self, expected_sha256: Optional[str] = None) -> bool:
        expected = self._expected if expected_sha256 is None else expected_sha256
        if not _is_sha256_hex(expected):
            return False
        return hmac.compare_digest(self._digest.hexdigest(), expected)
