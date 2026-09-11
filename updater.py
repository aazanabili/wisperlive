import json
import os
import re
import urllib.error
import urllib.request


REPOSITORY = "aazanabili/wisperlive"
RELEASES_API = f"https://api.github.com/repos/{REPOSITORY}/releases"
RELEASES_PAGE = f"https://github.com/{REPOSITORY}/releases"
USER_AGENT = "WhisperLive/1.1"


class UpdateError(RuntimeError):
    pass


def is_newer_version(candidate, current):
    def version_parts(value):
        return tuple(int(part) for part in re.findall(r"\d+", str(value)))

    candidate_parts = version_parts(candidate)
    current_parts = version_parts(current)
    if not candidate_parts or not current_parts:
        return candidate != current
    length = max(len(candidate_parts), len(current_parts))
    return candidate_parts + (0,) * (length - len(candidate_parts)) > current_parts + (0,) * (length - len(current_parts))


def get_latest_release():
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("WHISPERLIVE_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(RELEASES_API, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            releases = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise UpdateError("GitHub cannot access this private repository. Make releases public or set WHISPERLIVE_GITHUB_TOKEN.") from error
        if error.code == 403:
            raise UpdateError("GitHub blocked this update check. Wait for the rate limit or set WHISPERLIVE_GITHUB_TOKEN.") from error
        raise UpdateError(f"Could not reach GitHub: {error}") from error
    except OSError as error:
        raise UpdateError(f"Could not reach GitHub: {error}") from error
    release = next((item for item in releases if not item.get("draft") and not item.get("prerelease")), None)
    if not release:
        raise UpdateError("No published GitHub release is available yet.")
    return release
