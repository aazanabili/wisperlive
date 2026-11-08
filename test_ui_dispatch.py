"""Regression tests for the thread-safe UI dispatch.

The app used to call ``root.after`` directly from worker and hotkey threads,
which can deadlock the Tcl interpreter and freeze the whole application.
Callbacks must now go through the dispatch queue and be executed on the Tk
main thread, and one failing callback must never stop the pump.
"""

import queue
import unittest
from unittest import mock

import main


class FakeRoot:
    def __init__(self):
        self.scheduled = []

    def after(self, delay, callback):
        self.scheduled.append((delay, callback))
        return "after-id"


def make_app():
    app = main.WhisperLiveApp.__new__(main.WhisperLiveApp)
    app.root = FakeRoot()
    app.is_exiting = False
    app._ui_queue = queue.Queue()
    return app


class UiDispatchTests(unittest.TestCase):
    def test_scheduled_callback_runs_on_drain(self):
        app = make_app()
        calls = []
        app.call_in_ui(lambda: calls.append("ran"))
        app._drain_ui_queue()
        self.assertEqual(calls, ["ran"])
        self.assertTrue(app._ui_queue.empty())

    def test_failing_callback_does_not_stop_the_pump(self):
        app = make_app()
        calls = []

        def boom():
            raise RuntimeError("callback failed")

        app.call_in_ui(boom)
        app.call_in_ui(lambda: calls.append("still ran"))
        with mock.patch.object(main.logger, "exception") as logged:
            app._drain_ui_queue()
        self.assertEqual(calls, ["still ran"])
        logged.assert_called_once()
        # The pump reschedules itself while the app is running.
        self.assertTrue(app.root.scheduled)

    def test_pump_stops_rescheduling_after_exit(self):
        app = make_app()
        app.is_exiting = True
        app._drain_ui_queue()
        self.assertEqual(app.root.scheduled, [])


if __name__ == "__main__":
    unittest.main()
