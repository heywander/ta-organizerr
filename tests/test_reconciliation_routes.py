from __future__ import annotations

import unittest
from unittest.mock import patch

from flask import Flask

from ta_organizerr.routes import reconciliation as reconciliation_routes


class ReconciliationRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = Flask(__name__, static_folder=None)
        self.app.secret_key = "reconciliation-test-secret"
        self.app.register_blueprint(reconciliation_routes.bp)
        self.client = self.app.test_client()

    def _login(self) -> None:
        with self.client.session_transaction() as session:
            session["logged_in"] = True
            session["username"] = "tester"

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

    def test_inventory_returns_contract_payload(self) -> None:
        self._login()
        inventory = {
            "status": "complete",
            "scan_complete": True,
            "scan_token": "token",
            "summary": {},
            "entries": [],
            "database_entries": [],
            "link_entries": [],
            "source_files": [],
            "unindexed": [],
        }
        with patch.object(
            reconciliation_routes.reconciliation,
            "build_inventory",
            return_value=inventory,
        ):
            response = self.client.get("/api/reconciliation/inventory")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["scan_token"], "token")
        self.assertIn("entries", response.get_json())

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

    def test_inventory_scan_failure_returns_service_unavailable(self) -> None:
        self._login()
        error = reconciliation_routes.reconciliation.ReconciliationScanError(
            "Advanced Recovery inventory is unavailable",
            503,
        )
        with (
            patch.object(
                reconciliation_routes.reconciliation,
                "build_inventory",
                side_effect=error,
            ),
            patch.object(
                reconciliation_routes.reconciliation,
                "get_cached_inventory",
                return_value=None,
            ),
        ):
            response = self.client.get("/api/reconciliation/inventory")

        self.assertEqual(response.status_code, 503)
        self.assertIn("Advanced Recovery", response.get_json()["error"])

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
