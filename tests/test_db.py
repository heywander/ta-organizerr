from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ta_organizerr import db


class DatabaseSecurityTests(unittest.TestCase):
    def test_init_creates_private_database_and_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            db_path = data_dir / "videos.db"
            with patch.object(db, "DB_PATH", db_path):
                db.init_db()

            self.assertEqual(data_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(db_path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
