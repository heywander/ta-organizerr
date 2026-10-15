from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ta_organizerr import db as db_module
from ta_organizerr import symlinks


def _meta(title: str, channel: str, published: str = "2025-01-02", live: bool = False) -> dict:
    return {
        "title": title,
        "channel_name": channel,
        "published": published,
        "is_live": live,
        "filesystem_path": None,
    }


class ScanLayoutTests(unittest.TestCase):
    """End-to-end scan against a temp source tree with a mocked TA API."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.source = root / "source"
        self.target = root / "target"
        self.hidden = root / "hidden"
        self.data = root / "data"
        for d in (self.source, self.target, self.hidden, self.data):
            d.mkdir()
        self.db_path = self.data / "videos.db"

        self._patches = [
            patch.object(symlinks, "SOURCE_DIR", self.source),
            patch.object(symlinks, "TARGET_DIR", self.target),
            patch.object(symlinks, "HIDDEN_DIR", self.hidden),
            # Symlinks point at the "host" view of the source; use the same
            # path so they resolve in the test.
            patch.object(symlinks, "HOST_SOURCE_BASE", self.source),
            patch.object(db_module, "DB_PATH", self.db_path),
        ]
        for p in self._patches:
            p.start()
        db_module.init_db()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _add_source(self, channel_dir: str, vid: str) -> Path:
        d = self.source / channel_dir
        d.mkdir(exist_ok=True)
        f = d / f"{vid}.mp4"
        f.write_bytes(b"\x00" * 16)
        return f

    def _rows(self) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute("SELECT * FROM videos ORDER BY video_id").fetchall()
        finally:
            conn.close()

    def test_layout_for_regular_live_hidden_and_unicode(self) -> None:
        self._add_source("c1", "aaaaaaaaaaa")
        self._add_source("c1", "bbbbbbbbbbb")
        self._add_source("c2", "ccccccccccc")
        self._add_source("c3", "ddddddddddd")
        meta = {
            "aaaaaaaaaaa": _meta("Hello: World?", "Chan One"),
            "bbbbbbbbbbb": _meta("Live Show", "Chan One", live=True),
            "ccccccccccc": _meta("Secret", "Hidden Chan"),
            "ddddddddddd": _meta("動画タイトル", "日本語チャンネル"),
        }
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO hidden_channels VALUES ('Hidden Chan')")
        conn.commit()
        conn.close()

        with patch.object(symlinks, "fetch_all_metadata", return_value=meta):
            self.assertIsNone(symlinks.process_videos())

        regular = self.target / "Chan One" / "2025-01-02 - Hello_ World_" / "video.mp4"
        live = self.target / "Chan One" / "#streams" / "2025-01-02 - Live Show" / "video.mp4"
        hidden = self.hidden / "Hidden Chan" / "2025-01-02 - Secret" / "video.mp4"
        unicode_link = self.target / "日本語チャンネル" / "2025-01-02 - 動画タイトル" / "video.mp4"
        for link in (regular, live, hidden, unicode_link):
            self.assertTrue(link.is_symlink(), link)
            self.assertTrue(link.exists(), f"broken link: {link} -> {os.readlink(link)}")
        # Nothing may be dumped at the target root.
        self.assertFalse((self.target / "video.mp4").exists())

        rows = self._rows()
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(r["status"] == "linked" for r in rows))
        self.assertTrue(all(r["source_path"] for r in rows))

    def test_rescan_is_idempotent_and_keeps_codecs(self) -> None:
        self._add_source("c1", "aaaaaaaaaaa")
        meta = {"aaaaaaaaaaa": _meta("T", "C")}
        with patch.object(symlinks, "fetch_all_metadata", return_value=meta):
            symlinks.process_videos()
            conn = sqlite3.connect(self.db_path)
            conn.execute("UPDATE videos SET video_codec='vp9', audio_codec='opus'")
            conn.commit()
            conn.close()
            symlinks.process_videos()
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["video_codec"], rows[0]["audio_codec"]), ("vp9", "opus"))

    def test_missing_rows_are_pruned_when_link_is_gone(self) -> None:
        src = self._add_source("c1", "aaaaaaaaaaa")
        meta = {"aaaaaaaaaaa": _meta("T", "C")}
        with patch.object(symlinks, "fetch_all_metadata", return_value=meta):
            symlinks.process_videos()
        src.unlink()
        orphans = symlinks.check_orphaned_links()
        self.assertEqual(len(orphans), 1)
        self.assertEqual(self._rows()[0]["status"], "missing")

        # User deletes the broken link; the next scan must forget the row.
        Path(orphans[0]["path"]).unlink()
        with patch.object(symlinks, "fetch_all_metadata", return_value={}):
            symlinks.process_videos()
        self.assertEqual(self._rows(), [])


if __name__ == "__main__":
    unittest.main()
