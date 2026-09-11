import json
import re
import urllib.error
import urllib.request


REPOSITORY = "aazanabili/wisperlive"
RELEASES_API = f"https://api.github.com/repos/{REPOSITORY}/releases"
RELEASES_PAGE = f"https://github.com/{REPOSITORY}/releases"


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
    request = urllib.request.Request(RELEASES_API, headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            releases = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise UpdateError("No public release was found. Publish WhisperLive.exe in GitHub Releases so installed apps can download updates.") from error
        if error.code == 403:
            raise UpdateError("GitHub blocked this unauthenticated update check. Publish releases publicly or use a public release mirror.") from error
        raise UpdateError(f"Could not reach GitHub: {error}") from error
    except OSError as error:
        raise UpdateError(f"Could not reach GitHub: {error}") from error
    release = next((item for item in releases if not item.get("draft") and not item.get("prerelease")), None)
    if not release:
        raise UpdateError("No published GitHub release is available yet.")
    return release
