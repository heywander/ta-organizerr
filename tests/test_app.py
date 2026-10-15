from __future__ import annotations

import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from werkzeug.middleware.proxy_fix import ProxyFix

from ta_organizerr import db as db_module


class _AppFactory(unittest.TestCase):
    """Build the real app against a temp DB, skipping startup journal recovery.

    The crash-recovery passes run by ``create_app`` have their own suites and
    need the full ``/app/data`` layout; here only the HTTP layer is under test.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._patches = [
            patch.object(db_module, "DB_PATH", Path(self._tmp.name) / "videos.db"),
            patch("ta_organizerr.availability_designation.recover_pending_designations", return_value=[]),
            patch("ta_organizerr.availability.reclassify_legacy_channel_observations", return_value=None),
            patch("ta_organizerr.reference_repair.recover_interrupted_repairs", return_value=None),
            patch("ta_organizerr.app.ALLOWED_IPS", []),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def _create_app(self, **overrides):
        from ta_organizerr.app import create_app

        with patch.multiple("ta_organizerr.app", **overrides) if overrides else nullcontext():
            app = create_app()
        app.config["TESTING"] = True
        return app


class SameOriginTests(_AppFactory):
    def setUp(self) -> None:
        super().setUp()
        self.client = self._create_app().test_client()

    def test_setup_status_is_public(self) -> None:
        res = self.client.get("/api/auth/setup-status")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["setup_required"])

    def test_cross_origin_post_is_rejected(self) -> None:
        res = self.client.post("/api/auth/logout", headers={"Origin": "https://evil.example"})
        self.assertEqual(res.status_code, 403)
        res = self.client.post("/api/auth/logout", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(res.status_code, 403)

    def test_same_origin_post_is_allowed(self) -> None:
        res = self.client.post("/api/auth/logout", headers={"Origin": "http://localhost"})
        self.assertEqual(res.status_code, 200)
        res = self.client.post("/api/auth/logout")
        self.assertEqual(res.status_code, 200)

    def test_api_requires_login(self) -> None:
        self.assertEqual(self.client.get("/api/status").status_code, 401)
        self.assertEqual(self.client.post("/api/transcode/start", json={"filepath": "/x"}).status_code, 401)
        self.assertEqual(self.client.post("/api/transcode/scan").status_code, 401)


class ProxyFixTests(_AppFactory):
    def _get_as(self, client, remote_addr: str, forwarded_for: str):
        return client.get(
            "/api/auth/setup-status",
            headers={"X-Forwarded-For": forwarded_for},
            environ_base={"REMOTE_ADDR": remote_addr},
        )

    def test_forwarded_for_is_ignored_without_trusted_proxies(self) -> None:
        app = self._create_app(TRUSTED_PROXY_COUNT=0)
        self.assertNotIsInstance(app.wsgi_app, ProxyFix)
        with patch("ta_organizerr.app.ALLOWED_IPS", ["203.0.113.9"]):
            # The proxy's own address is what the allowlist sees; the header
            # cannot be used to spoof an allowed client.
            self.assertEqual(self._get_as(app.test_client(), "10.0.0.2", "203.0.113.9").status_code, 403)
            self.assertEqual(self._get_as(app.test_client(), "203.0.113.9", "198.51.100.7").status_code, 200)

    def test_forwarded_for_is_trusted_for_configured_hops(self) -> None:
        app = self._create_app(TRUSTED_PROXY_COUNT=1)
        self.assertIsInstance(app.wsgi_app, ProxyFix)
        self.assertEqual(app.wsgi_app.x_for, 1)

    def test_allowlist_sees_forwarded_client_behind_trusted_proxy(self) -> None:
        app = self._create_app(TRUSTED_PROXY_COUNT=1)
        with patch("ta_organizerr.app.ALLOWED_IPS", ["203.0.113.9"]):
            self.assertEqual(self._get_as(app.test_client(), "10.0.0.2", "203.0.113.9").status_code, 200)
            self.assertEqual(self._get_as(app.test_client(), "10.0.0.2", "198.51.100.7").status_code, 403)

if __name__ == "__main__":
    unittest.main()
