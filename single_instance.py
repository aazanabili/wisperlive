"""Single-instance guard for WhisperLive.

Holds an OS-level lock on a per-user lock file in the system temp directory
for the lifetime of the process. Because the operating system releases the
lock when the process exits or crashes, a leftover lock file can never block
a later launch.
"""

import errno
import os
import tempfile
from pathlib import Path

if os.name == "nt":
    import getpass
    import msvcrt

    def _lock_fd(fd):
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock_fd(fd):
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

    def _is_lock_conflict(exc):
        return (
            exc.errno in (errno.EACCES, errno.EDEADLK)
            or getattr(exc, "winerror", None) in (32, 33)
        )
else:
    import fcntl

    def _lock_fd(fd):
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_fd(fd):
        fcntl.flock(fd, fcntl.LOCK_UN)

    def _is_lock_conflict(exc):
        return exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK)


ALREADY_RUNNING_MESSAGE = "Another instance of the application is already running."


def _lock_owner_tag():
    """Identify the current user session so each user may run one instance."""
    try:
        return getpass.getuser() if os.name == "nt" else str(os.getuid())
    except Exception:
        return "unknown"


def lock_file_path():
    return Path(tempfile.gettempdir()) / f"whisperlive-{_lock_owner_tag()}.lock"


class SingleInstanceLock:
    """An exclusive OS file lock; released explicitly or automatically at exit."""

    def __init__(self, path):
        self.path = Path(path)
        self._fd = None

    def acquire(self):
        """Take the lock; return False when another instance already holds it."""
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            _lock_fd(fd)
        except OSError as exc:
            os.close(fd)
            if _is_lock_conflict(exc):
                return False
            raise
        self._fd = fd
        self._write_pid()
        return True

    def _write_pid(self):
        # Diagnostic only: which process holds the lock. The lock is what
        # enforces single-instance, so a failed write must not abort startup.
        try:
            os.ftruncate(self._fd, 0)
            os.write(self._fd, str(os.getpid()).encode("ascii", "replace"))
        except OSError:
            pass

    def release(self):
        fd = self._fd
        self._fd = None
        if fd is None:
            return
        try:
            _unlock_fd(fd)
        finally:
            os.close(fd)


def acquire_instance_lock():
    """Return the held SingleInstanceLock, or None when another instance runs."""
    lock = SingleInstanceLock(lock_file_path())
    return lock if lock.acquire() else None
