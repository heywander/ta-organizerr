from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from ta_organizerr import db as db_module
from ta_organizerr import transcode
from ta_organizerr.routes import transcode as transcode_routes
from ta_organizerr.transcode import MediaStream, needs_transcode, summarize_codecs


def _stream(kind: str, codec: str | None, attached_pic: bool = False) -> MediaStream:
    return MediaStream(kind, codec, attached_pic)


class SummarizeCodecsTests(unittest.TestCase):
    def test_h264_aac_is_done(self) -> None:
        codecs = summarize_codecs([_stream("video", "h264"), _stream("audio", "aac")])
        self.assertEqual(codecs, ("h264", "aac"))
        self.assertFalse(needs_transcode(*codecs))

    def test_thumbnail_stream_is_ignored(self) -> None:
        codecs = summarize_codecs([
            _stream("video", "mjpeg", attached_pic=True),
            _stream("video", "h264"),
            _stream("audio", "aac"),
        ])
        self.assertEqual(codecs, ("h264", "aac"))

    def test_video_without_audio_is_done_when_h264(self) -> None:
        codecs = summarize_codecs([_stream("video", "h264")])
        self.assertEqual(codecs, ("h264", "none"))
        self.assertFalse(needs_transcode(*codecs))

    def test_non_aac_audio_needs_transcode(self) -> None:
        codecs = summarize_codecs([_stream("video", "h264"), _stream("audio", "aac"), _stream("audio", "opus")])
        self.assertEqual(codecs, ("h264", "opus"))
        self.assertTrue(needs_transcode(*codecs))

    def test_vp9_needs_transcode(self) -> None:
        self.assertTrue(needs_transcode(*summarize_codecs([_stream("video", "vp9"), _stream("audio", "aac")])))

    def test_odd_stream_layouts_stay_visible(self) -> None:
        self.assertEqual(summarize_codecs([_stream("audio", "aac")])[0], "none")
        self.assertEqual(summarize_codecs([_stream("video", "h264"), _stream("video", "h264")])[0], "multiple")
        self.assertTrue(needs_transcode(None, None))


class _TempDb(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._db = patch.object(db_module, "DB_PATH", Path(self._tmp.name) / "videos.db")
        self._db.start()
        db_module.init_db()

    def tearDown(self) -> None:
        self._db.stop()
        self._tmp.cleanup()

    def _insert(self, video_id: str, symlink: str, status: str = "linked", **codecs: str | None) -> None:
        with db_module.get_db() as conn:
            conn.execute(
                "INSERT INTO videos (video_id, title, channel, published, symlink, status, video_codec, audio_codec) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (video_id, f"Title {video_id}", "Chan", "2025-01-01", symlink, status,
                 codecs.get("video_codec"), codecs.get("audio_codec")),
            )
            conn.commit()

    def _codecs(self, video_id: str) -> tuple[str | None, str | None]:
        with db_module.get_db() as conn:
            row = conn.execute(
                "SELECT video_codec, audio_codec FROM videos WHERE video_id = ?", (video_id,)
            ).fetchone()
        return (row["video_codec"], row["audio_codec"]) if row else (None, None)


class CodecPersistenceTests(_TempDb):
    def test_migration_adds_codec_columns_to_old_schema(self) -> None:
        with db_module.get_db() as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(videos)")}
        self.assertIn("video_codec", columns)
        self.assertIn("audio_codec", columns)

    def test_stash_and_restore_survive_delete_and_reinsert(self) -> None:
        self._insert("aaaaaaaaaaa", "/t/a/video.mp4", video_codec="vp9", audio_codec="opus")
        self._insert("bbbbbbbbbbb", "/t/b/video.mp4")
        with db_module.get_db() as conn:
            db_module.stash_codecs(conn)
            conn.execute("DELETE FROM videos WHERE status = 'linked'")
            conn.executemany(
                "INSERT OR REPLACE INTO videos (video_id, title, channel, published, symlink, status) "
                "VALUES (?, ?, ?, ?, ?, 'linked')",
                [("aaaaaaaaaaa", "A", "Chan", "2025-01-01", "/t/a/video.mp4"),
                 ("bbbbbbbbbbb", "B", "Chan", "2025-01-01", "/t/b/video.mp4")],
            )
            db_module.restore_codecs(conn)
            conn.commit()
        self.assertEqual(self._codecs("aaaaaaaaaaa"), ("vp9", "opus"))
        self.assertEqual(self._codecs("bbbbbbbbbbb"), (None, None))

    def test_scan_codecs_records_probe_results_by_video_id(self) -> None:
        self._insert("aaaaaaaaaaa", "/t/a/video.mp4")
        self._insert("bbbbbbbbbbb", "/t/b/video.mkv")
        self._insert("ccccccccccc", "/t/c/video.mp4", video_codec="h264", audio_codec="aac")

        def resolve(path: str):
            if path.endswith("b/video.mkv"):
                return None, "Source file does not exist"
            return Path(path), None

        def probe(path: str):
            return [_stream("video", "hevc"), _stream("audio", "aac")]

        with (
            patch.object(transcode, "_resolve_source", side_effect=resolve),
            patch.object(transcode, "probe_streams", side_effect=probe),
        ):
            probed = transcode.scan_codecs()

        self.assertEqual(probed, 1)
        self.assertEqual(self._codecs("aaaaaaaaaaa"), ("hevc", "aac"))
        self.assertEqual(self._codecs("bbbbbbbbbbb"), (None, None))
        self.assertEqual(self._codecs("ccccccccccc"), ("h264", "aac"))

    def test_record_codecs_by_symlink_is_a_noop_without_key(self) -> None:
        self._insert("aaaaaaaaaaa", "/t/a/video.mp4")
        transcode._record_codecs("symlink", None, "h264", "aac")
        self.assertEqual(self._codecs("aaaaaaaaaaa"), (None, None))
        transcode._record_codecs("symlink", "/t/a/video.mp4", "h264", "aac")
        self.assertEqual(self._codecs("aaaaaaaaaaa"), ("h264", "aac"))


class _ImmediateThread:
    def __init__(self, *, target, daemon: bool, name: str | None = None) -> None:
        self.target = target

    def start(self) -> None:
        self.target()


class TranscodeQueueRouteTests(_TempDb):
    def setUp(self) -> None:
        super().setUp()
        self.app = Flask(__name__, static_folder=None)
        self.app.secret_key = "transcode-route-test-secret"
        self.app.register_blueprint(transcode_routes.bp)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["logged_in"] = True
            session["username"] = "route-tester"

    def test_queue_lists_only_media_that_still_needs_transcode(self) -> None:
        self._insert("aaaaaaaaaaa", "/t/a/video.mp4", video_codec="h264", audio_codec="aac")
        self._insert("bbbbbbbbbbb", "/t/b/video.mkv", video_codec="vp9", audio_codec="opus")
        self._insert("ccccccccccc", "/t/c/video.mp4")
        self._insert("ddddddddddd", "/t/d/video.webm")
        self._insert("eeeeeeeeeee", "/t/e/video.mp4", status="missing")
        self._insert("fffffffffff", "/t/f/video.mp4", video_codec="h264", audio_codec="none")

        res = self.client.get("/api/transcode/videos")
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        ids = [v["video_id"] for v in body["videos"]]
        self.assertEqual(ids, ["bbbbbbbbbbb", "ccccccccccc"])
        self.assertEqual(body["total"], 2)
        self.assertEqual(body["unknown"], 1)
        self.assertFalse(body["scanning"])
        self.assertEqual(body["videos"][0]["video_codec"], "vp9")
        self.assertIsNone(body["videos"][1]["video_codec"])

    def test_scan_endpoint_runs_once_at_a_time(self) -> None:
        calls: list[str] = []
        with (
            patch.object(transcode_routes.threading, "Thread", _ImmediateThread),
            patch.object(transcode_routes, "scan_codecs", side_effect=lambda: calls.append("scan")),
        ):
            res = self.client.post("/api/transcode/scan")
        self.assertEqual(res.status_code, 202)
        self.assertEqual(res.get_json()["status"], "started")
        self.assertEqual(calls, ["scan"])
        self.assertFalse(transcode_routes._CODEC_SCAN_LOCK.locked())

        transcode_routes._CODEC_SCAN_LOCK.acquire()
        try:
            res = self.client.post("/api/transcode/scan")
            self.assertEqual(res.status_code, 202)
            self.assertEqual(res.get_json()["status"], "running")
            self.assertTrue(self.client.get("/api/transcode/videos").get_json()["scanning"])
        finally:
            transcode_routes._CODEC_SCAN_LOCK.release()


if __name__ == "__main__":
    unittest.main()
