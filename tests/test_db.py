from __future__ import annotations

import tempfile
import sqlite3
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

    def test_upgrade_adds_nullable_channel_id_before_creating_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data" / "videos.db"
            db_path.parent.mkdir()
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "CREATE TABLE videos (video_id TEXT PRIMARY KEY, title TEXT, "
                    "channel TEXT, symlink TEXT, status TEXT)"
                )
                conn.commit()
            with patch.object(db, "DB_PATH", db_path):
                db.init_db()
                with db.get_db() as conn:
                    columns = {row[1] for row in conn.execute("PRAGMA table_info(videos)")}
                    indexes = {row[1] for row in conn.execute("PRAGMA index_list(videos)")}
            self.assertIn("channel_id", columns)
            self.assertIn("idx_videos_channel_id", indexes)

    def test_upgrade_binds_video_availability_primary_key_to_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data" / "videos.db"
            db_path.parent.mkdir()
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE video_availability (
                        video_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL,
                        current_state TEXT, current_reason_code TEXT,
                        current_state_at TEXT, last_accessible_at TEXT,
                        last_observation_id INTEGER, updated_at TEXT
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO video_availability "
                    "(video_id, channel_id, current_state) VALUES (?, ?, ?)",
                    ("8jaoFHJ0SQE", "UCtbyZ5js_2m1LLi4nrrnFfw", "private"),
                )
                conn.commit()
            with patch.object(db, "DB_PATH", db_path):
                db.init_db()
                with db.get_db() as conn:
                    info = conn.execute("PRAGMA table_info(video_availability)").fetchall()
                    primary_key = [
                        row[1]
                        for row in sorted(
                            (row for row in info if row[5]), key=lambda row: row[5]
                        )
                    ]
                    row = conn.execute(
                        "SELECT channel_id, video_id, current_state FROM video_availability"
                    ).fetchone()
            self.assertEqual(primary_key, ["channel_id", "video_id"])
            self.assertEqual(
                tuple(row),
                ("UCtbyZ5js_2m1LLi4nrrnFfw", "8jaoFHJ0SQE", "private"),
            )

    def test_availability_timeline_index_matches_ordered_history_queries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data" / "videos.db"
            with patch.object(db, "DB_PATH", db_path):
                db.init_db()
                with db.get_db() as conn:
                    columns = [
                        row[2]
                        for row in conn.execute(
                            "PRAGMA index_info(idx_availability_observation_channel_timeline)"
                        )
                    ]
                    legacy = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'index' "
                        "AND name = 'idx_availability_observation_channel_state'"
                    ).fetchone()

            self.assertEqual(
                columns,
                [
                    "subject_type",
                    "channel_id",
                    "observed_at",
                    "observation_id",
                    "normalized_state",
                ],
            )
            self.assertIsNone(legacy)


if __name__ == "__main__":
    unittest.main()
