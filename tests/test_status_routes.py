from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest.mock import patch

from flask import Flask

from ta_organizerr import channel_designation
from ta_organizerr.routes import status


class _ImmediateThread:
    def __init__(self, *, target, daemon: bool) -> None:
        self.target = target
        self.daemon = daemon

    def start(self) -> None:
        self.target()


class StatusRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = Flask(__name__, static_folder=None)
        self.app.secret_key = "status-route-test-secret"
        self.app.register_blueprint(status.bp)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["logged_in"] = True
            session["username"] = "route-tester"

    def test_cleanup_uses_cross_process_designation_exclusion(self) -> None:
        events: list[str] = []

        @contextmanager
        def guarded():
            events.append("guard-enter")
            try:
                yield
            finally:
                events.append("guard-exit")

        def cleanup() -> None:
            events.append("cleanup")

        with (
            patch.object(status.threading, "Thread", _ImmediateThread),
            patch.object(
                status.channel_designation,
                "designation_mutation_guard",
                side_effect=guarded,
            ),
            patch.object(status, "cleanup_old_folders", side_effect=cleanup),
        ):
            response = self.client.post("/api/cleanup")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(events, ["guard-enter", "cleanup", "guard-exit"])

    def test_cleanup_stops_when_designation_recovery_is_unresolved(self) -> None:
        error = channel_designation.ChannelDesignationError(
            "journal requires recovery", 409, code="unresolved_journal"
        )
        with (
            patch.object(status.threading, "Thread", _ImmediateThread),
            patch.object(
                status.channel_designation,
                "designation_mutation_guard",
                side_effect=error,
            ),
            patch.object(status, "cleanup_old_folders") as cleanup,
        ):
            response = self.client.post("/api/cleanup")

        self.assertEqual(response.status_code, 200)
        cleanup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
