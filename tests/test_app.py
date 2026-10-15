from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ta_organizerr import db as db_module


class SameOriginTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._db = patch.object(db_module, "DB_PATH", Path(self._tmp.name) / "videos.db")
        self._db.start()
        from ta_organizerr.app import create_app

        with patch("ta_organizerr.app.ALLOWED_IPS", []):
            app = create_app()
        app.config["TESTING"] = True
        self.client = app.test_client()

    def tearDown(self) -> None:
        self._db.stop()
        self._tmp.cleanup()

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


if __name__ == "__main__":
    unittest.main()
