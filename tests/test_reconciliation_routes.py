from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from flask import Flask

from ta_organizerr.routes import reconciliation as reconciliation_routes


class ReconciliationRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        reconciliation_routes.reconciliation._reset_scan_runtime_for_tests()
        reconciliation_routes.reconciliation._clear_probe_cache()
        self.app = Flask(__name__, static_folder=None)
        self.app.secret_key = "reconciliation-test-secret"
        self.app.register_blueprint(reconciliation_routes.bp)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        thread = reconciliation_routes.reconciliation._SCAN_THREAD
        if thread is not None:
            thread.join(timeout=3)
        reconciliation_routes.reconciliation._reset_scan_runtime_for_tests()
        reconciliation_routes.reconciliation._clear_probe_cache()

    def _login(self) -> None:
        with self.client.session_transaction() as session:
            session["logged_in"] = True
            session["username"] = "tester"

    def _wait_for_scan(self, expected: str, timeout: float = 2) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = reconciliation_routes.reconciliation.get_scan_state()
            if state["status"] == expected:
                return state
            time.sleep(0.01)
        self.fail(
            f"scan did not reach {expected!r}: "
            f"{reconciliation_routes.reconciliation.get_scan_state()}"
        )

    @staticmethod
    def _inventory(token: str = "token") -> dict:
        return {
            "status": "complete",
            "scan_complete": True,
            "scan_token": token,
            "summary": {},
            "entries": [],
            "database_entries": [],
            "link_entries": [],
            "source_files": [],
            "unindexed": [],
        }

    def test_inventory_and_all_actions_require_authentication(self) -> None:
        routes = [
            ("get", "/api/reconciliation/inventory"),
            ("post", "/api/reconciliation/rescan"),
            ("post", "/api/reconciliation/repair-link"),
            ("post", "/api/reconciliation/adopt-real-file"),
            ("post", "/api/reconciliation/preview-candidate"),
            ("post", "/api/reconciliation/remove-broken-link"),
            ("post", "/api/reconciliation/forget-stale-record"),
        ]
        for method, path in routes:
            response = getattr(self.client, method)(path, json={})
            self.assertEqual(response.status_code, 401, path)

    def test_inventory_is_single_flight_and_completed_get_keeps_root_contract(self) -> None:
        self._login()
        inventory = self._inventory()
        started = threading.Event()
        release = threading.Event()
        calls = 0

        def build(*, progress):
            nonlocal calls
            calls += 1
            progress("probing", 1, 2, "Validating media")
            started.set()
            if not release.wait(2):
                raise RuntimeError("test scan was not released")
            return inventory

        try:
            with patch.object(
                reconciliation_routes.reconciliation,
                "build_inventory",
                side_effect=build,
            ):
                response = self.client.get("/api/reconciliation/inventory")
                self.assertEqual(response.status_code, 202)
                payload = response.get_json()
                self.assertEqual(payload["status"], "scanning")
                self.assertFalse(payload["scan_complete"])
                self.assertIsNone(payload["inventory"])
                self.assertFalse(payload["stale"])
                self.assertIn("job_id", payload["scan"])
                job_id = payload["scan"]["job_id"]

                self.assertTrue(started.wait(1))
                second = self.client.post("/api/reconciliation/rescan")
                self.assertEqual(second.status_code, 202)
                second_payload = second.get_json()
                self.assertEqual(second_payload["scan"]["job_id"], job_id)
                self.assertEqual(second_payload["scan"]["phase"], "probing")
                self.assertEqual(second_payload["scan"]["current"], 1)
                self.assertEqual(calls, 1)

                poll = self.client.get("/api/reconciliation/inventory?poll=1")
                self.assertEqual(poll.status_code, 202)
                poll_payload = poll.get_json()
                self.assertNotIn("inventory", poll_payload)
                self.assertEqual(poll_payload["scan"]["job_id"], job_id)

                release.set()
                self._wait_for_scan("complete")
                completed = self.client.get("/api/reconciliation/inventory")
        finally:
            release.set()

        self.assertEqual(completed.status_code, 200)
        completed_payload = completed.get_json()
        self.assertEqual(completed_payload["scan_token"], "token")
        self.assertIn("entries", completed_payload)
        self.assertEqual(completed_payload["scan"]["status"], "complete")
        self.assertFalse(completed_payload["stale"])

    def test_action_rejects_non_object_json_and_logs_rejection(self) -> None:
        self._login()
        with patch.object(reconciliation_routes, "log") as log:
            response = self.client.post(
                "/api/reconciliation/repair-link",
                json=["not", "an", "object"],
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "Request body must be a JSON object")
        self.assertIn("tester", log.call_args.args[0])

    def test_action_rejects_malformed_json(self) -> None:
        self._login()
        response = self.client.post(
            "/api/reconciliation/repair-link",
            data="{",
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "Request body must be a JSON object")

    def test_guard_errors_preserve_conflict_and_media_statuses(self) -> None:
        self._login()
        for status, message in ((409, "Inventory changed"), (422, "Candidate media is invalid")):
            with self.subTest(status=status):
                error = reconciliation_routes.reconciliation.ReconciliationError(
                    message,
                    status,
                )
                with (
                    patch.object(
                        reconciliation_routes.reconciliation,
                        "repair_link",
                        side_effect=error,
                    ),
                    patch.object(
                        reconciliation_routes.reconciliation,
                        "get_cached_inventory",
                        return_value=None,
                    ),
                ):
                    response = self.client.post(
                        "/api/reconciliation/repair-link",
                        json={"video_id": "abcdefghijk"},
                    )

                self.assertEqual(response.status_code, status)
                self.assertEqual(response.get_json()["error"], message)

    def test_failed_rescan_returns_error_and_preserves_prior_inventory(self) -> None:
        self._login()
        inventory = self._inventory("prior-token")
        with patch.object(
            reconciliation_routes.reconciliation,
            "build_inventory",
            return_value=inventory,
        ):
            started = self.client.get("/api/reconciliation/inventory")
            self.assertEqual(started.status_code, 202)
            self._wait_for_scan("complete")

        error = reconciliation_routes.reconciliation.ReconciliationScanError(
            "Advanced Recovery inventory is unavailable",
            503,
        )
        with patch.object(
            reconciliation_routes.reconciliation,
            "build_inventory",
            side_effect=error,
        ):
            rescan = self.client.post("/api/reconciliation/rescan")
            self.assertEqual(rescan.status_code, 202)
            scanning_payload = rescan.get_json()
            self.assertTrue(scanning_payload["stale"])
            self.assertEqual(
                scanning_payload["inventory"]["scan_token"],
                "prior-token",
            )
            self._wait_for_scan("error")
            response = self.client.get("/api/reconciliation/inventory")

        self.assertEqual(response.status_code, 503)
        payload = response.get_json()
        self.assertEqual(payload["status"], "error")
        self.assertFalse(payload["scan_complete"])
        self.assertIn("Advanced Recovery", payload["error"])
        self.assertEqual(payload["scan"]["status"], "error")
        self.assertTrue(payload["stale"])
        self.assertEqual(payload["inventory"]["scan_token"], "prior-token")

    def test_failed_initial_scan_has_clear_error_without_stale_inventory(self) -> None:
        self._login()
        with patch.object(
            reconciliation_routes.reconciliation,
            "build_inventory",
            side_effect=RuntimeError("inventory provider offline"),
        ):
            started = self.client.get("/api/reconciliation/inventory")
            self.assertEqual(started.status_code, 202)
            self._wait_for_scan("error")
            response = self.client.get("/api/reconciliation/inventory")

        self.assertEqual(response.status_code, 503)
        payload = response.get_json()
        self.assertEqual(payload["error"], "inventory provider offline")
        self.assertIsNone(payload["inventory"])
        self.assertFalse(payload["stale"])

    def test_thread_start_failure_returns_503_and_never_sticks_scanning(self) -> None:
        self._login()
        with patch.object(
            reconciliation_routes.reconciliation.threading.Thread,
            "start",
            side_effect=RuntimeError("thread quota exhausted"),
        ):
            response = self.client.get("/api/reconciliation/inventory")

        self.assertEqual(response.status_code, 503)
        payload = response.get_json()
        self.assertEqual(payload["status"], "error")
        self.assertIn("thread quota exhausted", payload["error"])
        self.assertEqual(payload["scan"]["status"], "error")
        self.assertIsNone(reconciliation_routes.reconciliation._SCAN_THREAD)

        follow_up = self.client.get("/api/reconciliation/inventory")
        self.assertEqual(follow_up.status_code, 503)
        self.assertEqual(follow_up.get_json()["scan"]["status"], "error")

    def test_successful_action_returns_fresh_inventory_token(self) -> None:
        self._login()
        result = {
            "success": True,
            "action_applied": True,
            "message": "Link repaired",
            "inventory": {
                "status": "complete",
                "scan_token": "fresh-token",
                "summary": {},
                "entries": [],
            },
        }
        with patch.object(
            reconciliation_routes.reconciliation,
            "repair_link",
            return_value=result,
        ):
            response = self.client.post(
                "/api/reconciliation/repair-link",
                json={"video_id": "abcdefghijk"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["action_applied"])
        self.assertEqual(payload["inventory"]["scan_token"], "fresh-token")


if __name__ == "__main__":
    unittest.main()
