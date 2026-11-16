"""Central logging setup for WhisperLive (stdlib only).

Privacy policy (app-wide, applies to every module): NEVER log transcript text,
prompt text, audio bytes, or the API key. Log file paths as basenames only
(a full %TEMP% path leaks the Windows username).

Handler-state note (D1): `setup_logging()` appends EXACTLY ONE root file
handler (never a console handler) and does not guard against pre-existing
global handler state. TESTS that call `setup_logging()` MUST capture and
restore the root logger's handlers (and `sys.stderr`) in setUp/tearDown.
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
MAX_LOG_BYTES = 1_048_576
LOG_BACKUP_COUNT = 3


def _log_dir() -> Path:
    """Read %LOCALAPPDATA% at call time so tests can patch the environment."""
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "WhisperLive" / "logs"


LOG_DIR = _log_dir()


def setup_logging() -> None:
    """Best-effort logging for the windowed (console-less) app. Never raises."""
    try:
        log_dir = _log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / "whisperlive.log",
            maxBytes=MAX_LOG_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(handler)
    except Exception:  # a broken log path (OSError/permission) must not stop the app from starting
        logging.getLogger(__name__).addHandler(logging.NullHandler())
    # Quiet noisy third-party sinks (privacy + readability).
    logging.getLogger("google").setLevel(logging.WARNING)
    logging.getLogger("grpc").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a module logger; formatting/rotation come from setup_logging()."""
    return logging.getLogger(name)


def log_thread_exception(args) -> None:
    """Visible diagnostics for worker-thread crashes without killing the app.
    Installed as `threading.excepthook` in main's __main__ guard (upstream
    `_log_thread_exception` moved here verbatim in wording)."""
    logging.getLogger(__name__).error(
        "Uncaught exception in thread %r",
        getattr(args.thread, "name", "?"),
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )
