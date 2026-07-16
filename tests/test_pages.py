from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from flask import Flask


class StaticPageGuardTests(unittest.TestCase):
    def test_html_build_artifacts_are_not_public_catch_all_assets(self) -> None:
        from ta_organizerr.routes.pages import bp

        with tempfile.TemporaryDirectory() as tmp:
            static_root = Path(tmp)
            (static_root / "maintenance").mkdir()
            (static_root / "maintenance" / "index.html").write_text(
                "private shell",
                encoding="utf-8",
            )
            app = Flask(__name__, static_folder=None)
            app.config["STATIC_BUILD_FOLDER"] = str(static_root)
            app.register_blueprint(bp)

            response = app.test_client().get("/maintenance/index.html")
            self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
