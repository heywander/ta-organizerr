from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from ta_organizerr import auth, db


class AdminSetupTests(unittest.TestCase):
    def test_concurrent_setup_creates_exactly_one_admin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data" / "videos.db"
            with patch.object(db, "DB_PATH", db_path):
                db.init_db()
                barrier = threading.Barrier(2)

                def create(username: str) -> str:
                    barrier.wait()
                    try:
                        auth.create_admin(username, "password123")
                        return "created"
                    except auth.AdminAlreadyExists:
                        return "exists"

                with (
                    patch.object(auth, "generate_password_hash", return_value="hash"),
                    ThreadPoolExecutor(max_workers=2) as pool,
                ):
                    results = list(pool.map(create, ("first", "second")))

                with db.get_db() as conn:
                    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

        self.assertCountEqual(results, ["created", "exists"])
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
