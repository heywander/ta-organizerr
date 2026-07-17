from __future__ import annotations

import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ta_organizerr import availability, db, symlinks, video_designation


CHANNEL_ID = "UCtbyZ5js_2m1LLi4nrrnFfw"
VIDEO_ID = "8jaoFHJ0SQE"


class VideoDesignationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.data = self.base / "data"
        self.target = self.base / "target"
        self.hidden = self.base / "hidden"
        self.source = self.base / "source"
        for path in (self.data, self.target, self.hidden, self.source):
            path.mkdir()
        self.patchers = (
            patch.object(db, "DB_PATH", self.data / "videos.db"),
            patch.object(video_designation, "TARGET_DIR", self.target),
            patch.object(video_designation, "HIDDEN_DIR", self.hidden),
            patch.object(symlinks, "SOURCE_DIR", self.source),
            patch.object(symlinks, "TARGET_DIR", self.target),
            patch.object(symlinks, "HIDDEN_DIR", self.hidden),
            patch.object(symlinks, "HOST_SOURCE_BASE", self.source),
        )
        for patcher in self.patchers:
            patcher.start()
        db.init_db()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary.cleanup()

    def _video(self, *, nested: bool = False) -> Path:
        channel = self.target / "Channel"
        parent = channel / "Nested" if nested else channel
        folder = parent / "2026-01-01 - Title"
        folder.mkdir(parents=True)
        source = self.source / f"{VIDEO_ID}.mp4"
        source.write_bytes(b"media")
        media = folder / "video.mp4"
        media.symlink_to(source)
        with db.get_db() as conn:
            conn.execute(
                """
                INSERT INTO channel_identities
                    (channel_id, display_name, provenance)
                VALUES (?, 'Channel', 'normal')
                """,
                (CHANNEL_ID,),
            )
            conn.execute(
                """
                INSERT INTO videos
                    (video_id, title, channel, channel_id, published, symlink, status)
                VALUES (?, 'Title', 'Channel', ?, '2026-01-01', ?, 'linked')
                """,
                (VIDEO_ID, CHANNEL_ID, str(media)),
            )
            conn.commit()
        result = {
            "state": "community_guidelines_removed",
            "reason_code": "community_guidelines",
            "raw_reason": "Removed for violating Community Guidelines",
            "provider": "yt-dlp",
            "evidence": {},
        }
        availability.record_observation(
            "video-check", "video", VIDEO_ID, CHANNEL_ID, result
        )
        return media

    def test_designation_preserves_symlink_inode_and_raw_target(self) -> None:
        media = self._video()
        inode = os.lstat(media).st_ino
        raw_target = os.readlink(media)
        plan = video_designation.build_plan(
            CHANNEL_ID, [VIDEO_ID], actor="tester"
        )
        self.assertTrue(plan["can_apply"])
        result = video_designation.apply_plan(
            CHANNEL_ID,
            plan["plan_token"],
            plan["confirmation"],
            actor="tester",
        )
        self.assertEqual(result["status"], "committed")
        destination = (
            self.target / "Channel" /
            "2026-01-01 - Title [Community Guidelines Removed]" / "video.mp4"
        )
        self.assertTrue(destination.is_symlink())
        self.assertEqual(os.lstat(destination).st_ino, inode)
        self.assertEqual(os.readlink(destination), raw_target)
        with db.get_db() as conn:
            row = conn.execute(
                "SELECT symlink FROM videos WHERE video_id=?", (VIDEO_ID,)
            ).fetchone()
            designation = conn.execute(
                "SELECT storage_component FROM video_designations WHERE video_id=?",
                (VIDEO_ID,),
            ).fetchone()
        self.assertEqual(row["symlink"], str(destination))
        self.assertEqual(
            designation["storage_component"],
            "2026-01-01 - Title [Community Guidelines Removed]",
        )

    def test_nested_layout_and_existing_destination_are_blocked(self) -> None:
        self._video(nested=True)
        plan = video_designation.build_plan(
            CHANNEL_ID, [VIDEO_ID], actor="tester"
        )
        self.assertFalse(plan["can_apply"])
        self.assertIn("unsupported_nested_layout", {
            blocker["code"] for blocker in plan["blockers"]
        })

    def test_available_video_can_restore_to_original_component(self) -> None:
        self._video()
        plan = video_designation.build_plan(
            CHANNEL_ID, [VIDEO_ID], actor="tester"
        )
        video_designation.apply_plan(
            CHANNEL_ID, plan["plan_token"], plan["confirmation"], actor="tester"
        )
        availability.record_observation(
            "available", "video", VIDEO_ID, CHANNEL_ID,
            {
                "state": "available", "reason_code": "public_response",
                "raw_reason": "Public metadata returned", "provider": "yt-dlp",
                "evidence": {},
            },
        )
        restore = video_designation.build_plan(
            CHANNEL_ID, [VIDEO_ID], action="restore", actor="tester"
        )
        self.assertTrue(restore["can_apply"])
        video_designation.apply_plan(
            CHANNEL_ID,
            restore["plan_token"],
            restore["confirmation"],
            actor="tester",
        )
        self.assertTrue(
            (self.target / "Channel" / "2026-01-01 - Title" / "video.mp4").is_symlink()
        )
        with db.get_db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM video_designations WHERE video_id=?",
                (VIDEO_ID,),
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_normal_organizer_rescan_keeps_designated_component(self) -> None:
        self._video()
        plan = video_designation.build_plan(
            CHANNEL_ID, [VIDEO_ID], actor="tester"
        )
        video_designation.apply_plan(
            CHANNEL_ID, plan["plan_token"], plan["confirmation"], actor="tester"
        )
        metadata = {
            VIDEO_ID: {
                "title": "Title", "channel_name": "Channel",
                "channel_id": CHANNEL_ID, "published": "2026-01-01",
                "is_live": False,
            }
        }
        with patch.object(symlinks, "fetch_all_metadata", return_value=metadata):
            self.assertIsNone(symlinks.process_videos())
        designated = (
            self.target / "Channel" /
            "2026-01-01 - Title [Community Guidelines Removed]" / "video.mp4"
        )
        self.assertTrue(designated.is_symlink())
        self.assertFalse(
            (self.target / "Channel" / "2026-01-01 - Title").exists()
        )

    def test_restart_guard_rolls_back_filesystem_only_journal(self) -> None:
        self._video()
        plan = video_designation.build_plan(
            CHANNEL_ID, [VIDEO_ID], actor="tester"
        )
        operation = plan["operations"][0]
        journal = {
            "schema": 1, "journal_id": "restart-test",
            "state": "filesystem_moving", "channel_id": CHANNEL_ID,
            "actor": "tester", "action": "designate",
            "operations": [operation], "completed": [VIDEO_ID],
        }
        journal_path = video_designation._journal_dir() / "restart-test.json"
        video_designation._rename_noreplace(operation)
        video_designation._write_journal(journal_path, journal)

        with video_designation.mutation_guard():
            pass

        self.assertTrue(Path(operation["source_path"]).is_dir())
        self.assertFalse(Path(operation["destination_path"]).exists())
        recovered = json.loads(journal_path.read_text())
        self.assertEqual(recovered["state"], "rolled_back")


class SmartVideoJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "videos.db"
        self.patcher = patch.object(db, "DB_PATH", self.db_path)
        self.patcher.start()
        db.init_db()
        with db.get_db() as conn:
            availability.upsert_channel_identity(conn, CHANNEL_ID, provenance="normal")
            conn.executemany(
                "INSERT INTO videos (video_id, channel_id, status) VALUES (?, ?, 'linked')",
                [(f"A{index:010d}", CHANNEL_ID) for index in range(51)],
            )
            conn.commit()

    def tearDown(self) -> None:
        self.patcher.stop()
        self.temporary.cleanup()

    def test_api_batches_resolve_fifty_ids_and_skip_detail_probes(self) -> None:
        channel = {
            "state": "accessible", "reason_code": "public_response",
            "raw_reason": "Public metadata returned", "provider": "yt-dlp",
            "evidence": {},
        }

        def batch(ids, _channel_id):
            return ({
                video_id: {
                    "state": "available", "reason_code": "api_resource_returned_for_channel",
                    "raw_reason": "API returned exact video", "provider": "youtube-data-api-batch",
                    "evidence": {},
                }
                for video_id in ids
            }, [], False, False)

        with (
            patch.object(availability, "YOUTUBE_API_KEY", "configured"),
            patch.object(availability, "probe", return_value=channel),
            patch.object(availability, "_api_probe_video_batch", side_effect=batch) as api,
            patch.object(availability, "_rate_limited_video_probe") as detail,
        ):
            job = availability.start_video_check(
                CHANNEL_ID, scope="all", background=False
            )
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["total"], 51)
        self.assertEqual(api.call_count, 2)
        detail.assert_not_called()

    def test_terminated_channel_runs_no_video_probes(self) -> None:
        terminated = {
            "state": "terminated_explicit", "reason_code": "channel_terminated",
            "raw_reason": "Channel terminated", "provider": "yt-dlp", "evidence": {},
        }
        with (
            patch.object(availability, "probe", return_value=terminated),
            patch.object(availability, "_rate_limited_video_probe") as detail,
        ):
            job = availability.start_video_check(
                CHANNEL_ID, ["A0000000000"], scope="selected", background=False
            )
        self.assertEqual(job["phase"], "channel_terminated")
        self.assertEqual(job["total"], 0)
        detail.assert_not_called()

    def test_video_removal_never_changes_accessible_channel_state(self) -> None:
        channel = {
            "state": "accessible", "reason_code": "public_response",
            "raw_reason": "Public metadata returned", "provider": "yt-dlp",
            "evidence": {},
        }
        removed = {
            "state": "community_guidelines_removed",
            "reason_code": "community_guidelines",
            "raw_reason": "Removed for violating Community Guidelines",
            "provider": "yt-dlp", "evidence": {},
        }
        video_id = "A0000000000"
        with (
            patch.object(availability, "YOUTUBE_API_KEY", ""),
            patch.object(availability, "probe", return_value=channel),
            patch.object(availability, "_rate_limited_video_probe", return_value=removed),
        ):
            job = availability.start_video_check(
                CHANNEL_ID, [video_id], scope="selected", background=False
            )
        self.assertEqual(job["status"], "completed")
        with db.get_db() as conn:
            channel_row = conn.execute(
                "SELECT current_state FROM channel_identities WHERE channel_id=?",
                (CHANNEL_ID,),
            ).fetchone()
            video_row = conn.execute(
                "SELECT current_state FROM video_availability WHERE channel_id=? AND video_id=?",
                (CHANNEL_ID, video_id),
            ).fetchone()
        self.assertEqual(channel_row["current_state"], "accessible")
        self.assertEqual(video_row["current_state"], "community_guidelines_removed")


if __name__ == "__main__":
    unittest.main()
