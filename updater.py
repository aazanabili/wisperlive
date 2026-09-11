import json
import os
import subprocess
import sys
import urllib.error
import urllib.request


REPOSITORY = "aazanabili/wisperlive"
RELEASES_API = f"https://api.github.com/repos/{REPOSITORY}/releases"
EXECUTABLE_NAME = "WhisperLive.exe"


class UpdateError(RuntimeError):
    pass


def update_from_source():
    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        capture_output=True,
        text=True,
        check=False,
    )
    branch_name = branch.stdout.strip()
    if branch.returncode or not branch_name:
        raise UpdateError("Could not determine the current Git branch.")
    result = subprocess.run(
        ["git", "pull", "--ff-only", "origin", branch_name],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise UpdateError(result.stderr.strip() or "Git could not update this checkout.")
    return result.stdout.strip()


def get_latest_release():
    request = urllib.request.Request(RELEASES_API, headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            releases = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise UpdateError("Updates are unavailable until this GitHub repository and a release are publicly accessible.") from error
        raise UpdateError(f"Could not reach GitHub: {error}") from error
    except OSError as error:
        raise UpdateError(f"Could not reach GitHub: {error}") from error
    release = next((item for item in releases if not item.get("draft") and not item.get("prerelease")), None)
    if not release:
        raise UpdateError("No published GitHub release is available yet.")
    return release


def download_release(release):
    asset = next((item for item in release.get("assets", []) if item.get("name") == EXECUTABLE_NAME), None)
    if not asset:
        raise UpdateError("The latest release does not include WhisperLive.exe.")

    destination = f"{sys.executable}.update"
    request = urllib.request.Request(asset["browser_download_url"], headers={"Accept": "application/octet-stream"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response, open(destination, "wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
    except OSError as error:
        if os.path.exists(destination):
            os.remove(destination)
        raise UpdateError(f"Could not download the update: {error}") from error
    return release.get("tag_name", "latest"), destination


def restart_with_downloaded_release(download_path):
    script_path = f"{sys.executable}.update.cmd"
    script = "\r\n".join([
        "@echo off",
        f'powershell -NoProfile -Command "Wait-Process -Id {os.getpid()}"',
        f'move /y "{download_path}" "{sys.executable}" > nul',
        f'start "" "{sys.executable}"',
        'del "%~f0"',
    ])
    with open(script_path, "w", encoding="ascii", newline="\r\n") as file:
        file.write(script)
    subprocess.Popen(["cmd", "/c", script_path], creationflags=subprocess.CREATE_NO_WINDOW)
