from __future__ import annotations

import unittest
from unittest.mock import patch

from flask import Flask

from ta_organizerr import availability
from ta_organizerr.routes import availability as availability_routes


CHANNEL_ID = "UCtbyZ5js_2m1LLi4nrrnFfw"


class AvailabilityRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = Flask(__name__, static_folder=None)
        self.app.secret_key = "availability-route-test-secret"
        self.app.register_blueprint(availability_routes.bp)
        self.client = self.app.test_client()

    def login(self) -> None:
        with self.client.session_transaction() as session:
            session["logged_in"] = True
            session["username"] = "route-tester"

    def test_every_read_job_and_designation_route_requires_authentication(self) -> None:
        endpoints = (
            ("get", "/api/availability/channels", None),
            ("get", f"/api/availability/channels/{CHANNEL_ID}", None),
            ("post", f"/api/availability/channels/{CHANNEL_ID}/check", {}),
            ("get", f"/api/availability/channels/{CHANNEL_ID}/videos", None),
            ("post", f"/api/availability/channels/{CHANNEL_ID}/videos/check", {}),
            ("get", f"/api/availability/channels/{CHANNEL_ID}/observations", None),
            ("get", "/api/availability/jobs/job-id", None),
            ("post", "/api/availability/jobs/job-id/cancel", {}),
            (
                "post",
                f"/api/availability/channels/{CHANNEL_ID}/designation/plan",
                {"designation": "terminated"},
            ),
            (
                "post",
                f"/api/availability/channels/{CHANNEL_ID}/designation/apply",
                {"plan_token": "token", "confirmation": "confirmation"},
            ),
        )
        for method, path, body in endpoints:
            with self.subTest(method=method, path=path):
                kwargs = {"json": body} if body is not None else {}
                response = getattr(self.client, method)(path, **kwargs)
                self.assertEqual(response.status_code, 401)

    def test_mutation_payloads_reject_malformed_and_non_object_json(self) -> None:
        self.login()
        path = f"/api/availability/channels/{CHANNEL_ID}/check"

        malformed = self.client.post(path, data="{", content_type="application/json")
        non_object = self.client.post(path, json=["not", "an", "object"])
        unexpected = self.client.post(path, json={"force": True})

        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(
            malformed.get_json()["error"],
            "Request body must be a valid JSON object",
        )
        self.assertEqual(non_object.status_code, 400)
        self.assertEqual(unexpected.status_code, 400)

    def test_channel_check_accepts_only_an_empty_body_and_records_actor(self) -> None:
        self.login()
        expected = {"job_id": "job", "status": "queued"}
        with patch.object(
            availability_routes.availability,
            "start_channel_check",
            return_value=expected,
        ) as start:
            response = self.client.post(
                f"/api/availability/channels/{CHANNEL_ID}/check"
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["job"], expected)
        start.assert_called_once_with(
            CHANNEL_ID, actor="route-tester", background=True
        )

    def test_video_check_rejects_null_and_non_string_id_payloads(self) -> None:
        self.login()
        path = f"/api/availability/channels/{CHANNEL_ID}/videos/check"

        null_selection = self.client.post(path, json={"video_ids": None})
        nested_selection = self.client.post(
            path, json={"video_ids": [{"id": "8jaoFHJ0SQE"}]}
        )

        self.assertEqual(null_selection.status_code, 400)
        self.assertEqual(nested_selection.status_code, 400)
        self.assertIn("non-empty array", null_selection.get_json()["error"])
        self.assertIn("exact 11-character", nested_selection.get_json()["error"])

    def test_designation_plan_and_apply_have_strict_contracts(self) -> None:
        self.login()
        plan = {"plan_token": "a" * 64, "safe_to_apply": True}
        result = {"success": True, "mode": "rename"}
        plan_path = (
            f"/api/availability/channels/{CHANNEL_ID}/designation/plan"
        )
        apply_path = (
            f"/api/availability/channels/{CHANNEL_ID}/designation/apply"
        )
        with (
            patch.object(
                availability_routes.availability_designation,
                "build_designation_plan",
                return_value=plan,
            ) as build,
            patch.object(
                availability_routes.availability_designation,
                "apply_designation",
                return_value=result,
            ) as apply,
        ):
            planned = self.client.post(
                plan_path, json={"designation": "terminated"}
            )
            applied = self.client.post(
                apply_path,
                json={
                    "plan_token": "a" * 64,
                    "confirmation": f"TERMINATE {CHANNEL_ID}",
                },
            )
            extra = self.client.post(
                apply_path,
                json={
                    "plan_token": "a" * 64,
                    "confirmation": f"TERMINATE {CHANNEL_ID}",
                    "force": True,
                },
            )

        self.assertEqual(planned.status_code, 200)
        self.assertEqual(planned.get_json()["plan"], plan)
        build.assert_called_once_with(CHANNEL_ID, "terminated")
        self.assertEqual(applied.status_code, 200)
        self.assertEqual(applied.get_json()["result"], result)
        apply.assert_called_once_with(
            CHANNEL_ID,
            "a" * 64,
            f"TERMINATE {CHANNEL_ID}",
            actor="route-tester",
        )
        self.assertEqual(extra.status_code, 400)

    def test_service_errors_preserve_operator_status(self) -> None:
        self.login()
        with patch.object(
            availability_routes.availability,
            "get_channel",
            side_effect=availability.AvailabilityError(
                "Channel identity not found", 404
            ),
        ):
            response = self.client.get(
                f"/api/availability/channels/{CHANNEL_ID}"
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"], "Channel identity not found")


if __name__ == "__main__":
    unittest.main()
