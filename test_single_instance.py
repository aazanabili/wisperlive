import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import main
import single_instance
from single_instance import SingleInstanceLock

REPO_DIR = Path(single_instance.__file__).resolve().parent

# Child process that takes the lock, reports success, then holds it until the
# parent closes stdin. Real second process so the test is valid on Windows
# (where same-process lock conflicts across handles are not guaranteed) too.
LOCK_HOLDER = """
import sys
from single_instance import SingleInstanceLock

lock = SingleInstanceLock(sys.argv[1])
if not lock.acquire():
    print("busy", flush=True)
    raise SystemExit(2)
print("locked", flush=True)
sys.stdin.readline()
lock.release()
"""


class SingleInstanceLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "whisperlive.lock"

    def test_acquire_takes_and_releases_the_lock(self):
        lock = SingleInstanceLock(self.path)
        self.assertTrue(lock.acquire())
        lock.release()
        reacquired = SingleInstanceLock(self.path)
        self.assertTrue(reacquired.acquire())
        reacquired.release()

    @unittest.skipIf(
        sys.platform == "win32",
        "same-process lock conflicts across Windows handles are not guaranteed; "
        "covered by the real subprocess test",
    )
    def test_second_acquire_is_refused_while_lock_is_held(self):
        holder = SingleInstanceLock(self.path)
        self.assertTrue(holder.acquire())
        contender = SingleInstanceLock(self.path)
        self.assertFalse(contender.acquire())
        holder.release()
        self.assertTrue(contender.acquire())
        contender.release()

    def test_second_process_is_refused_until_first_exits(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", LOCK_HOLDER, str(self.path)],
            cwd=str(REPO_DIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(proc.stdout.readline().strip(), "locked")
            contender = SingleInstanceLock(self.path)
            self.assertFalse(contender.acquire())
            proc.stdin.write("\n")
            proc.stdin.flush()
            proc.stdin.close()
            self.assertEqual(proc.wait(timeout=10), 0)
            self.assertTrue(contender.acquire())
            contender.release()
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()

    def test_message_matches_expected_wording(self):
        self.assertEqual(
            single_instance.ALREADY_RUNNING_MESSAGE,
            "Another instance of the application is already running.",
        )

    def test_lock_file_path_is_per_user_in_tempdir(self):
        path = single_instance.lock_file_path()
        self.assertEqual(path.parent, Path(tempfile.gettempdir()))
        self.assertRegex(path.name, r"^whisperlive-.+\.lock$")


class StartupGuardTests(unittest.TestCase):
    def test_second_instance_notifies_and_exits_with_code_one(self):
        with mock.patch.object(main, "acquire_instance_lock", return_value=None), \
                mock.patch.object(main, "_notify_already_running") as notify:
            with self.assertRaises(SystemExit) as raised:
                main._acquire_single_instance_lock_or_exit()
        self.assertEqual(raised.exception.code, 1)
        notify.assert_called_once_with()

    def test_lock_failure_reports_error_and_exits_with_code_one(self):
        stderr = io.StringIO()
        with mock.patch.object(
            main, "acquire_instance_lock", side_effect=OSError("lock unavailable")
        ), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                main._acquire_single_instance_lock_or_exit()
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("Could not acquire the single-instance lock", stderr.getvalue())

    def test_first_instance_gets_the_held_lock(self):
        sentinel = mock.sentinel.lock
        with mock.patch.object(main, "acquire_instance_lock", return_value=sentinel):
            self.assertIs(main._acquire_single_instance_lock_or_exit(), sentinel)


if __name__ == "__main__":
    unittest.main()
