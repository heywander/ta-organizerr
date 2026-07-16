from __future__ import annotations

import os
import stat
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import subprocess

from ta_organizerr import db, reconciliation, symlinks


VIDEO_ID = "abcdefghijk"
SECOND_ID = "ABCDEFGHIJK"


def valid_probe(path: Path, **_kwargs):
    try:
        size = os.lstat(path).st_size
    except OSError:
        size = 0
    invalid = size == 0 or "corrupt" in path.name
    error = "Media file is empty" if size == 0 else ("corrupt media" if invalid else None)
    return {
        "valid": not invalid,
        "valid_media": not invalid,
        "error": error,
        "duration": 12.5 if not invalid else None,
        "streams": ([{"index": 0, "type": "video", "codec": "h264", "duration": "12.5"}] if not invalid else []),
        "has_video": not invalid,
        "has_audio": False,
        "size_bytes": size,
    }


class LinkReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.source = root / "source"
        self.target = root / "target"
        self.hidden = root / "hidden"
        self.host = root / "host-source"
        self.db_path = root / "data" / "videos.db"
        for directory in (self.source, self.target, self.hidden):
            directory.mkdir(parents=True)

        self.stack = ExitStack()
        self.stack.enter_context(patch.object(db, "DB_PATH", self.db_path))
        self.stack.enter_context(patch.object(reconciliation, "SOURCE_DIR", self.source))
        self.stack.enter_context(patch.object(reconciliation, "TARGET_DIR", self.target))
        self.stack.enter_context(patch.object(reconciliation, "HIDDEN_DIR", self.hidden))
        self.stack.enter_context(patch.object(reconciliation, "HOST_SOURCE_BASE", self.host))
        self.stack.enter_context(patch.object(reconciliation, "MANAGED_ROOTS", (self.target, self.hidden)))
        self.stack.enter_context(patch.object(reconciliation, "MEDIA_ROOTS", (self.source, self.target, self.hidden)))
        self.stack.enter_context(patch.object(reconciliation, "probe_media", side_effect=valid_probe))
        self.stack.enter_context(patch.object(reconciliation, "scan_for_unindexed_videos", side_effect=self._recovery_inventory))
        db.init_db()

    def tearDown(self) -> None:
        self.stack.close()
        self.temp.cleanup()

    def _recovery_inventory(self):
        rows = []
        lost = []
        for root in (self.source, self.target, self.hidden):
            for path in root.rglob("*"):
                try:
                    mode = os.lstat(path).st_mode
                except OSError:
                    continue
                if not stat.S_ISREG(mode) or not path.suffix:
                    continue
                video_id = reconciliation.extract_id_from_filename(path.name)
                if video_id:
                    rows.append({
                        "path": str(path),
                        "filename": path.name,
                        "video_id": video_id,
                        "size_mb": 0.1,
                        "ta_source": "test",
                    })
        return {"unindexed": rows, "lost": lost, "redundant": [], "rescue": []}

    def _row(self, link: Path, *, video_id: str = VIDEO_ID, adopted: int = 0) -> None:
        with db.get_db() as conn:
            conn.execute(
                """
                INSERT INTO videos
                (video_id, title, channel, published, symlink, status, adopted_real)
                VALUES (?, 'Title', 'Channel', '2026-01-01', ?, 'missing', ?)
                """,
                (video_id, str(link), adopted),
            )
            conn.commit()

    def _entry(self, inventory, video_id: str = VIDEO_ID, link_path: Path | None = None):
        matches = [item for item in inventory["entries"] if item["video_id"] == video_id]
        if link_path is not None:
            matches = [item for item in matches if item["link_path"] == str(link_path)]
        return matches[0]

    def _broken_link(self, path: Path, video_id: str = VIDEO_ID) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(self.host / f"missing [{video_id}].mp4")

    def test_healthy_link_is_verified_from_translated_host_target(self) -> None:
        media = self.source / "channel" / f"Good [{VIDEO_ID}].mp4"
        media.parent.mkdir()
        media.write_bytes(b"valid media")
        link = self.target / "Channel" / "Title" / "video.mp4"
        link.parent.mkdir(parents=True)
        link.symlink_to(self.host / media.relative_to(self.source))
        self._row(link)

        inventory = reconciliation.build_inventory()
        entry = self._entry(inventory)

        self.assertEqual(entry["status"], "healthy")
        self.assertTrue(entry["link"]["target_exists"])
        self.assertEqual(entry["stored_target_path"], os.readlink(link))
        self.assertIn(str(media), {item["path"] for item in inventory["source_files"]})

    def test_resolving_link_to_corrupt_media_is_invalid(self) -> None:
        media = self.source / "channel" / f"corrupt [{VIDEO_ID}].mp4"
        media.parent.mkdir()
        media.write_bytes(b"random bytes")
        link = self.target / "Channel" / "Title" / "video.mp4"
        link.parent.mkdir(parents=True)
        link.symlink_to(self.host / media.relative_to(self.source))
        self._row(link)

        entry = self._entry(reconciliation.build_inventory(), link_path=link)

        self.assertEqual(entry["status"], "invalid_media")
        self.assertIn("corrupt", entry["detail"])

    def test_broken_link_with_exact_unindexed_file_is_repairable_not_missing(self) -> None:
        candidate = self.source / f"Recovered [{VIDEO_ID}].mp4"
        candidate.write_bytes(b"valid")
        link = self.target / "Channel" / "Title" / "video.mp4"
        self._broken_link(link)
        self._row(link)

        inventory = reconciliation.build_inventory()
        entry = self._entry(inventory)

        self.assertEqual(entry["status"], "repair_available")
        self.assertEqual(len(entry["candidates"]), 1)
        self.assertTrue(entry["candidates"][0]["exact_video_id_match"])
        self.assertIn("repair_link", entry["allowed_actions"])

    def test_multiple_exact_paths_are_ambiguous_even_when_one_is_corrupt(self) -> None:
        (self.source / f"Good [{VIDEO_ID}].mp4").write_bytes(b"valid")
        (self.source / f"corrupt [{VIDEO_ID}].mkv").write_bytes(b"bad")
        link = self.target / "Channel" / "Title" / "video.mp4"
        self._broken_link(link)
        self._row(link)

        entry = self._entry(reconciliation.build_inventory(), link_path=link)

        self.assertEqual(entry["status"], "multiple_matches")
        self.assertNotIn("repair_link", entry["allowed_actions"])
        self.assertEqual(len(entry["candidates"]), 2)

    def test_only_invalid_exact_candidate_is_invalid_not_missing(self) -> None:
        (self.source / f"corrupt [{VIDEO_ID}].mp4").write_bytes(b"bad")
        link = self.target / "Channel" / "Title" / "video.mp4"
        self._broken_link(link)
        self._row(link)

        entry = self._entry(reconciliation.build_inventory())

        self.assertEqual(entry["status"], "invalid_media")
        self.assertFalse(entry["candidates"][0]["valid"])

    def test_no_exact_candidate_is_truly_missing_and_title_never_matches(self) -> None:
        (self.source / f"Title resemblance [{SECOND_ID}].mp4").write_bytes(b"valid")
        link = self.target / "Channel" / "Title" / "video.mp4"
        self._broken_link(link)
        self._row(link)

        entry = self._entry(reconciliation.build_inventory())

        self.assertEqual(entry["status"], "truly_missing")
        self.assertEqual(entry["candidates"], [])
        self.assertIn("remove_broken_link", entry["allowed_actions"])

    def test_real_file_can_be_adopted_without_moving_or_deleting_bytes(self) -> None:
        real = self.target / "Channel" / "Title" / "video.mp4"
        real.parent.mkdir(parents=True)
        original = b"real media bytes"
        real.write_bytes(original)
        self._row(real)

        inventory = reconciliation.build_inventory()
        entry = self._entry(inventory)
        self.assertEqual(entry["status"], "real_file_expected_symlink")

        result = reconciliation.adopt_existing_real_file({
            "video_id": VIDEO_ID,
            "link_path": str(real),
            "scan_token": inventory["scan_token"],
        })

        self.assertTrue(result["success"])
        self.assertEqual(real.read_bytes(), original)
        self.assertFalse(real.is_symlink())
        self.assertEqual(self._entry(result["inventory"])["status"], "healthy")
        with db.get_db() as conn:
            row = conn.execute("SELECT status, adopted_real FROM videos WHERE video_id = ?", (VIDEO_ID,)).fetchone()
        self.assertEqual(row["status"], "adopted")
        self.assertTrue(row["adopted_real"])

    def test_repair_reprobes_and_creates_exact_link_then_rescans(self) -> None:
        candidate = self.source / f"Recovered [{VIDEO_ID}].mp4"
        candidate.write_bytes(b"valid")
        link = self.target / "Channel" / "Title" / "video.mp4"
        self._broken_link(link)
        self._row(link)
        inventory = reconciliation.build_inventory()

        result = reconciliation.repair_link({
            "video_id": VIDEO_ID,
            "link_path": str(link),
            "candidate_path": str(candidate),
            "scan_token": inventory["scan_token"],
        })

        self.assertTrue(result["success"])
        self.assertTrue(link.is_symlink())
        self.assertEqual(Path(os.readlink(link)), self.host / candidate.relative_to(self.source))
        self.assertEqual(self._entry(result["inventory"])["status"], "healthy")

    def test_remove_broken_link_deletes_only_link_and_exact_db_row(self) -> None:
        link = self.target / "Channel" / "Title" / "video.mp4"
        self._broken_link(link)
        self._row(link)
        with db.get_db() as conn:
            conn.execute("INSERT INTO lost_media (video_id, filepath) VALUES (?, '/keep/me')", (VIDEO_ID,))
            conn.commit()
        inventory = reconciliation.build_inventory()

        result = reconciliation.remove_broken_link({
            "video_id": VIDEO_ID,
            "link_path": str(link),
            "scan_token": inventory["scan_token"],
        })

        self.assertTrue(result["success"])
        self.assertFalse(os.path.lexists(link))
        self.assertTrue(link.parent.is_dir())
        with db.get_db() as conn:
            video = conn.execute("SELECT 1 FROM videos WHERE video_id = ?", (VIDEO_ID,)).fetchone()
            lost = conn.execute("SELECT 1 FROM lost_media WHERE video_id = ?", (VIDEO_ID,)).fetchone()
        self.assertIsNone(video)
        self.assertIsNotNone(lost)

    def test_remove_quarantine_refuses_swapped_real_file(self) -> None:
        link = self.target / "Channel" / "Title" / "video.mp4"
        self._broken_link(link)
        self._row(link)
        inventory = reconciliation.build_inventory()
        real_rename = reconciliation._rename_noreplace
        swapped = False

        def swap_then_rename(source: Path, destination: Path):
            nonlocal swapped
            if source == link and not swapped:
                swapped = True
                source.unlink()
                source.write_bytes(b"do not delete me")
            return real_rename(source, destination)

        with patch.object(reconciliation, "_rename_noreplace", side_effect=swap_then_rename):
            with self.assertRaises(reconciliation.ReconciliationError):
                reconciliation.remove_broken_link({
                    "video_id": VIDEO_ID,
                    "link_path": str(link),
                    "scan_token": inventory["scan_token"],
                })

        self.assertTrue(link.is_file())
        self.assertFalse(link.is_symlink())
        self.assertEqual(link.read_bytes(), b"do not delete me")
        with db.get_db() as conn:
            self.assertIsNotNone(conn.execute("SELECT 1 FROM videos WHERE video_id = ?", (VIDEO_ID,)).fetchone())

    def test_stale_scan_token_and_duplicate_path_ownership_disable_mutation(self) -> None:
        candidate = self.source / f"Recovered [{VIDEO_ID}].mp4"
        candidate.write_bytes(b"valid")
        link = self.target / "Channel" / "Title" / "video.mp4"
        self._broken_link(link)
        self._row(link)
        with db.get_db() as conn:
            conn.execute(
                "INSERT INTO videos (video_id, symlink, status) VALUES (?, ?, 'missing')",
                (SECOND_ID, str(link)),
            )
            conn.commit()
        inventory = reconciliation.build_inventory()
        entry = self._entry(inventory)
        self.assertNotIn("repair_link", entry["allowed_actions"])

        with self.assertRaises(reconciliation.ReconciliationError) as caught:
            reconciliation.repair_link({
                "video_id": VIDEO_ID,
                "link_path": str(link),
                "candidate_path": str(candidate),
                "scan_token": "stale",
            })
        self.assertEqual(caught.exception.status, 409)

    def test_old_valid_token_rejects_candidate_added_after_scan(self) -> None:
        link = self.target / "Channel" / "Title" / "video.mp4"
        self._broken_link(link)
        self._row(link)
        inventory = reconciliation.build_inventory()
        candidate = self.source / f"Recovered [{VIDEO_ID}].mp4"
        candidate.write_bytes(b"valid")

        with self.assertRaises(reconciliation.ReconciliationError) as caught:
            reconciliation.remove_broken_link({
                "video_id": VIDEO_ID,
                "link_path": str(link),
                "scan_token": inventory["scan_token"],
            })

        self.assertEqual(caught.exception.status, 409)
        self.assertTrue(link.is_symlink())
        self.assertTrue(candidate.is_file())
        with db.get_db() as conn:
            self.assertIsNotNone(
                conn.execute(
                    "SELECT 1 FROM videos WHERE video_id = ?",
                    (VIDEO_ID,),
                ).fetchone()
            )

    def test_old_valid_token_rejects_link_replaced_by_real_file(self) -> None:
        link = self.target / "Channel" / "Title" / "video.mp4"
        self._broken_link(link)
        self._row(link)
        inventory = reconciliation.build_inventory()
        link.unlink()
        replacement = b"real video must survive"
        link.write_bytes(replacement)

        with self.assertRaises(reconciliation.ReconciliationError) as caught:
            reconciliation.remove_broken_link({
                "video_id": VIDEO_ID,
                "link_path": str(link),
                "scan_token": inventory["scan_token"],
            })

        self.assertEqual(caught.exception.status, 409)
        self.assertFalse(link.is_symlink())
        self.assertEqual(link.read_bytes(), replacement)
        with db.get_db() as conn:
            self.assertIsNotNone(
                conn.execute(
                    "SELECT 1 FROM videos WHERE video_id = ?",
                    (VIDEO_ID,),
                ).fetchone()
            )

    def test_old_valid_token_rejects_candidate_changed_after_scan(self) -> None:
        candidate = self.source / f"Recovered [{VIDEO_ID}].mp4"
        candidate.write_bytes(b"valid")
        link = self.target / "Channel" / "Title" / "video.mp4"
        self._broken_link(link)
        self._row(link)
        inventory = reconciliation.build_inventory()
        changed = b"different candidate bytes"
        candidate.write_bytes(changed)

        with self.assertRaises(reconciliation.ReconciliationError) as caught:
            reconciliation.repair_link({
                "video_id": VIDEO_ID,
                "link_path": str(link),
                "candidate_path": str(candidate),
                "scan_token": inventory["scan_token"],
            })

        self.assertEqual(caught.exception.status, 409)
        self.assertTrue(link.is_symlink())
        self.assertEqual(candidate.read_bytes(), changed)

    def test_target_unindexed_exact_file_prevents_truly_missing(self) -> None:
        candidate = self.hidden / "review" / f"Recovered [{VIDEO_ID}].mp4"
        candidate.parent.mkdir()
        candidate.write_bytes(b"valid")
        link = self.target / "Channel" / "Title" / "video.mp4"
        self._broken_link(link)
        self._row(link)

        entry = self._entry(reconciliation.build_inventory(), link_path=link)

        self.assertEqual(entry["status"], "repair_available")
        self.assertEqual(entry["candidates"][0]["location"], "managed")
        self.assertNotIn("repair_link", entry["allowed_actions"])
        self.assertIn("other managed mount", entry["detail"])

    def test_conflicting_database_and_stored_target_ids_disable_automatic_action(self) -> None:
        candidate = self.source / f"Recovered [{VIDEO_ID}].mp4"
        candidate.write_bytes(b"valid")
        link = self.target / "Channel" / "Title" / "video.mp4"
        link.parent.mkdir(parents=True)
        link.symlink_to(self.host / f"missing [{SECOND_ID}].mp4")
        self._row(link)

        entry = self._entry(reconciliation.build_inventory(), link_path=link)

        self.assertTrue(entry["identity_conflict"])
        self.assertEqual(entry["status"], "multiple_matches")
        self.assertEqual(entry["video_id_sources"]["database"], VIDEO_ID)
        self.assertEqual(entry["video_id_sources"]["stored_target"], SECOND_ID)
        self.assertNotIn("repair_link", entry["allowed_actions"])
        self.assertNotIn("remove_broken_link", entry["allowed_actions"])

    def test_recovery_reported_id_never_overrides_actual_filename_id(self) -> None:
        wrong_name = self.source / f"Wrong [{SECOND_ID}].mp4"
        wrong_name.write_bytes(b"valid")
        link = self.target / "Channel" / "Title" / "video.mp4"
        self._broken_link(link)
        self._row(link)

        def lying_inventory():
            return {
                "unindexed": [{"path": str(wrong_name), "video_id": VIDEO_ID}],
                "lost": [], "redundant": [], "rescue": [],
            }

        inventory = reconciliation.build_inventory(lying_inventory)
        entry = self._entry(inventory, link_path=link)

        self.assertEqual(entry["status"], "truly_missing")
        self.assertEqual(inventory["unindexed"][0]["video_id"], SECOND_ID)
        self.assertIsNotNone(inventory["unindexed"][0]["identity_error"])

    def test_real_expected_path_is_a_previewable_candidate_before_adoption(self) -> None:
        real = self.target / "Channel" / "Title" / "video.mp4"
        real.parent.mkdir(parents=True)
        real.write_bytes(b"valid")
        self._row(real)

        inventory = reconciliation.build_inventory()
        entry = self._entry(inventory, link_path=real)
        preview = entry["candidates"][0]

        self.assertEqual(preview["association"], "expected_link_path")
        self.assertFalse(preview["exact_video_id_match"])
        self.assertTrue(preview["valid"])
        self.assertEqual(preview["duration_seconds"], 12.5)
        self.assertTrue(preview["streams"])

        response = reconciliation.preview_candidate({
            "video_id": VIDEO_ID,
            "link_path": str(real),
            "candidate_path": str(real),
            "scan_token": inventory["scan_token"],
        })
        self.assertTrue(response["success"])
        self.assertFalse(response["exact_video_id_match"])

    def test_managed_unindexed_candidate_can_be_previewed(self) -> None:
        candidate = self.hidden / "review" / f"Recovered [{VIDEO_ID}].mp4"
        candidate.parent.mkdir()
        candidate.write_bytes(b"valid")
        link = self.target / "Channel" / "Title" / "video.mp4"
        self._broken_link(link)
        self._row(link)
        inventory = reconciliation.build_inventory()

        response = reconciliation.preview_candidate({
            "video_id": VIDEO_ID,
            "link_path": str(link),
            "candidate_path": str(candidate),
            "scan_token": inventory["scan_token"],
        })

        self.assertTrue(response["success"])
        self.assertIn("/api/recovery/preview?", response["preview_url"])

    def test_forget_stale_record_is_database_only(self) -> None:
        absent = self.target / "Channel" / "Title" / "video.mp4"
        absent.parent.mkdir(parents=True)
        self._row(absent)
        inventory = reconciliation.build_inventory()
        entry = self._entry(inventory, link_path=absent)
        self.assertEqual(entry["status"], "truly_missing")
        self.assertIn("forget_stale_record", entry["allowed_actions"])

        result = reconciliation.forget_stale_record({
            "video_id": VIDEO_ID,
            "link_path": str(absent),
            "scan_token": inventory["scan_token"],
        })

        self.assertTrue(result["success"])
        self.assertTrue(absent.parent.is_dir())
        with db.get_db() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM videos WHERE video_id = ?", (VIDEO_ID,)).fetchone())

    def test_failed_advanced_recovery_scan_preserves_last_snapshot_and_fails_closed(self) -> None:
        link = self.target / "Channel" / "Title" / "video.mp4"
        self._broken_link(link)
        self._row(link)
        good = reconciliation.build_inventory()

        with self.assertRaises(reconciliation.ReconciliationScanError):
            reconciliation.build_inventory(lambda: (_ for _ in ()).throw(RuntimeError("offline")))

        self.assertIs(reconciliation.get_cached_inventory(), good)


class MediaProbeSafetyTests(unittest.TestCase):
    def test_rejects_symlink_and_zero_byte_before_invoking_ffprobe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real.mp4"
            real.write_bytes(b"bytes")
            link = root / "link.mp4"
            link.symlink_to(real)
            empty = root / "empty.mp4"
            empty.touch()

            with patch.object(reconciliation.subprocess, "run") as run:
                symlink_result = reconciliation.probe_media(link, allowed_roots=(root,))
                empty_result = reconciliation.probe_media(empty, allowed_roots=(root,))

            self.assertFalse(symlink_result["valid"])
            self.assertIn("regular file", symlink_result["error"])
            self.assertFalse(empty_result["valid"])
            self.assertIn("empty", empty_result["error"])
            run.assert_not_called()

    def test_valid_probe_uses_pipe_only_forced_demuxer_and_parses_streams(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / "video.mp4"
            media.write_bytes(b"container")
            completed = SimpleNamespace(
                returncode=0,
                stdout='{"streams":[{"index":0,"codec_type":"video","codec_name":"h264","duration":"3.5"}],"format":{"duration":"3.5"}}',
                stderr="",
            )
            with patch.object(reconciliation.subprocess, "run", return_value=completed) as run:
                result = reconciliation.probe_media(media, allowed_roots=(root,))

            self.assertTrue(result["valid"])
            self.assertEqual(result["duration"], 3.5)
            self.assertTrue(result["has_video"])
            command = run.call_args.args[0]
            kwargs = run.call_args.kwargs
            self.assertEqual(command[-1], "pipe:0")
            self.assertEqual(command[command.index("-protocol_whitelist") + 1], "pipe")
            self.assertEqual(command[command.index("-format_whitelist") + 1], "mov")
            self.assertNotIn(str(media), command)
            self.assertFalse(kwargs["shell"])
            self.assertIsNotNone(kwargs["stdin"])

    def test_probe_timeout_and_tool_error_are_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / "video.mkv"
            media.write_bytes(b"container")

            with patch.object(
                reconciliation.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["ffprobe"], 20),
            ):
                timed_out = reconciliation.probe_media(media, allowed_roots=(root,))
            with patch.object(
                reconciliation.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=1, stdout="", stderr="bad container"),
            ):
                failed = reconciliation.probe_media(media, allowed_roots=(root,))

            self.assertFalse(timed_out["valid"])
            self.assertIn("Timed out", timed_out["error"])
            self.assertFalse(failed["valid"])
            self.assertEqual(failed["error"], "bad container")


class AdoptedMediaDurabilityTests(unittest.TestCase):
    def test_normal_scan_preserves_adoption_across_hidden_directory_move(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            target = root / "target"
            hidden = root / "hidden"
            host = root / "host"
            db_path = root / "data" / "videos.db"
            for directory in (source, target, hidden):
                directory.mkdir(parents=True)
            source_file = source / f"Source [{VIDEO_ID}].mp4"
            source_file.write_bytes(b"source")
            adopted = target / "Channel" / "2026-01-01 - Title" / "video.mp4"
            adopted.parent.mkdir(parents=True)
            original = b"adopted bytes"
            adopted.write_bytes(original)

            with patch.object(db, "DB_PATH", db_path):
                db.init_db()
                with db.get_db() as conn:
                    conn.execute(
                        """
                        INSERT INTO videos
                        (video_id, title, channel, published, symlink, status, adopted_real)
                        VALUES (?, 'Title', 'Channel', '2026-01-01', ?, 'adopted', 1)
                        """,
                        (VIDEO_ID, str(adopted)),
                    )
                    conn.execute("INSERT INTO hidden_channels (channel_name) VALUES ('Channel')")
                    conn.commit()

                # The terminated video is deliberately absent from current TA
                # metadata. Adoption path reconciliation must still happen.
                metadata = {}
                with (
                    patch.object(symlinks, "SOURCE_DIR", source),
                    patch.object(symlinks, "TARGET_DIR", target),
                    patch.object(symlinks, "HIDDEN_DIR", hidden),
                    patch.object(symlinks, "HOST_SOURCE_BASE", host),
                    patch.object(symlinks, "fetch_all_metadata", return_value=metadata),
                ):
                    result = symlinks.process_videos()

                moved = hidden / adopted.relative_to(target)
                self.assertIsNone(result)
                self.assertTrue(moved.is_file())
                self.assertFalse(moved.is_symlink())
                self.assertEqual(moved.read_bytes(), original)
                with db.get_db() as conn:
                    row = conn.execute(
                        "SELECT symlink, status, adopted_real FROM videos WHERE video_id = ?",
                        (VIDEO_ID,),
                    ).fetchone()
                self.assertEqual(row["symlink"], str(moved))
                self.assertEqual(row["status"], "adopted")
                self.assertTrue(row["adopted_real"])

    def test_orphan_scan_does_not_overwrite_adopted_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            hidden = root / "hidden"
            source = root / "source"
            db_path = root / "data" / "videos.db"
            for directory in (target, hidden, source):
                directory.mkdir(parents=True)
            adopted = target / "Adopted" / "video.mp4"
            adopted.parent.mkdir()
            adopted.write_bytes(b"keep")
            broken = hidden / "Channel" / "2026 - Duplicate" / "video.mp4"
            broken.parent.mkdir(parents=True)
            broken.symlink_to(source / f"missing [{VIDEO_ID}].mp4")

            with patch.object(db, "DB_PATH", db_path):
                db.init_db()
                with db.get_db() as conn:
                    conn.execute(
                        """
                        INSERT INTO videos
                        (video_id, title, symlink, status, adopted_real)
                        VALUES (?, 'Adopted title', ?, 'adopted', 1)
                        """,
                        (VIDEO_ID, str(adopted)),
                    )
                    conn.commit()
                with (
                    patch.object(symlinks, "TARGET_DIR", target),
                    patch.object(symlinks, "HIDDEN_DIR", hidden),
                    patch.object(symlinks, "SOURCE_DIR", source),
                ):
                    symlinks.check_orphaned_links()
                with db.get_db() as conn:
                    row = conn.execute(
                        "SELECT title, symlink, status, adopted_real FROM videos WHERE video_id = ?",
                        (VIDEO_ID,),
                    ).fetchone()

            self.assertEqual(row["title"], "Adopted title")
            self.assertEqual(row["symlink"], str(adopted))
            self.assertEqual(row["status"], "adopted")
            self.assertTrue(row["adopted_real"])


if __name__ == "__main__":
    unittest.main()
