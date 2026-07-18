from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ta_organizerr import db, media_reservations, recovered_channels, reference_repair, salvage


CHANNEL_ID = "UCtbyZ5js_2m1LLi4nrrnFfw"
DAMAGED_ID = "abcdefghijk"
REFERENCE_ID = "lmnopqrstuv"
OTHER_ID = "0123456789A"


class ReferenceRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.target = self.root / "target"
        self.salvage = self.root / "data" / "media-salvage"
        for path in (self.source, self.target, self.salvage.parent):
            path.mkdir(parents=True, exist_ok=True)
        self.channel = self.source / CHANNEL_ID
        self.channel.mkdir()
        self.damaged = self.channel / f"{DAMAGED_ID}.mp4"
        self.reference = self.channel / f"{REFERENCE_ID}.mp4"
        self.other = self.channel / f"{OTHER_ID}.mp4"
        self.damaged.write_bytes(b"damaged-original-bytes")
        self.damaged.chmod(0o640)
        self.reference.write_bytes(b"healthy-reference-bytes")
        self.other.write_bytes(b"another-reference")
        self.patchers = (
            patch.object(db, "DB_PATH", self.root / "data" / "videos.db"),
            patch.object(recovered_channels, "SOURCE_DIR", self.source),
            patch.object(recovered_channels, "TARGET_DIR", self.target),
            patch.object(reference_repair, "SOURCE_DIR", self.source),
            patch.object(reference_repair, "SALVAGE_DIR", self.salvage),
            patch.object(salvage, "SALVAGE_DIR", self.salvage),
        )
        for patcher in self.patchers:
            patcher.start()
        db.init_db()
        media_reservations._reset_reservations_for_tests()
        recovered_channels.reset_runtime_for_tests()
        reference_repair.reset_runtime_for_tests()
        case = recovered_channels.create_case(CHANNEL_ID, "Recovered Test", actor="tester")
        self.case_id = case["case_id"]
        self.items = {
            item["video_id"]: item
            for item in recovered_channels.list_items(self.case_id, per_page=500)["items"]
        }
        self._seed_item(DAMAGED_ID, "serious", streams=self._streams())
        self._seed_item(REFERENCE_ID, "clear", streams=self._streams())
        self._seed_item(OTHER_ID, "minimal", streams=[{"type": "video", "codec": "h265"}])

    def tearDown(self) -> None:
        reference_repair.reset_runtime_for_tests()
        recovered_channels.reset_runtime_for_tests()
        media_reservations._reset_reservations_for_tests()
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    @staticmethod
    def _streams():
        return [
            {"index": 0, "type": "video", "codec": "h264", "profile": "High",
             "pixel_format": "yuv420p", "width": 1280, "height": 720,
             "frame_rate": "30/1", "time_base": "1/30000"},
            {"index": 1, "type": "audio", "codec": "aac", "sample_rate": "48000",
             "channels": 2, "channel_layout": "stereo"},
        ]

    def _seed_item(self, video_id: str, health: str, *, streams):
        item = self.items[video_id]
        path = Path(item["source_path"])
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        diagnosis = {
            "assessment": "healthy" if health in {"clear", "minimal"} else "damaged",
            "source": {"sha256": checksum, "streams": streams, "warnings": []},
        }
        with db.get_db() as conn:
            conn.execute(
                """UPDATE recovered_channel_items SET inspection_status='completed',
                   health_tier=?,sha256=?,diagnosis_json=? WHERE item_id=?""",
                (health, checksum, json.dumps(diagnosis), item["item_id"]),
            )
            conn.commit()

    def _insert_completed_repair(self) -> tuple[str, Path, str, str]:
        repair_id = "repair000000000000000001"
        directory = self.salvage / "reference-repairs" / repair_id
        directory.mkdir(parents=True)
        candidate = directory / "candidate.mp4"
        candidate.write_bytes(b"repaired-candidate-content")
        source_hash = hashlib.sha256(self.damaged.read_bytes()).hexdigest()
        reference_hash = hashlib.sha256(self.reference.read_bytes()).hexdigest()
        candidate_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
        before = {"probeable": False, "streams": [], "health_tier": "inconclusive",
                  "warnings": ["No streams"]}
        after = {"probeable": True, "streams": self._streams(), "health_tier": "clear",
                 "decode_clean": True, "duration_seconds": 120, "warnings": [],
                 "sha256": candidate_hash}
        damaged_item = self.items[DAMAGED_ID]
        reference_item = self.items[REFERENCE_ID]
        with db.get_db() as conn:
            conn.execute(
                """INSERT INTO recovered_media_repairs
                   (repair_id,case_id,damaged_item_id,reference_item_id,status,phase,progress,
                    source_sha256,reference_sha256,candidate_sha256,before_json,after_json,
                    assessment,candidate_path,decision,created_by,completed_at)
                   VALUES (?,?,?,?, 'completed','review',100,?,?,?,?,?,'better',?,'pending','tester',CURRENT_TIMESTAMP)""",
                (repair_id, self.case_id, damaged_item["item_id"], reference_item["item_id"],
                 source_hash, reference_hash, candidate_hash, json.dumps(before),
                 json.dumps(after), str(candidate)),
            )
            conn.commit()
        return repair_id, candidate, source_hash, candidate_hash

    def test_ranked_references_prefers_matching_stream_signature(self) -> None:
        result = reference_repair.ranked_references(
            self.case_id, self.items[DAMAGED_ID]["item_id"]
        )
        self.assertFalse(result["compatibility_uncertain"])
        self.assertEqual(result["references"][0]["video_id"], REFERENCE_ID)
        self.assertEqual(result["references"][0]["compatibility"], "strong")
        self.assertGreater(result["references"][0]["score"], result["references"][1]["score"])

    def test_missing_streams_makes_ranking_explicitly_uncertain(self) -> None:
        self._seed_item(DAMAGED_ID, "serious", streams=[])
        result = reference_repair.ranked_references(
            self.case_id, self.items[DAMAGED_ID]["item_id"]
        )
        self.assertTrue(result["compatibility_uncertain"])
        self.assertTrue(all(item["compatibility"] == "uncertain" for item in result["references"]))

    def test_same_exact_video_id_is_not_a_structural_reference(self) -> None:
        duplicate = self.channel / f"copy_{DAMAGED_ID}.mp4"
        duplicate.write_bytes(b"alternate")
        duplicate_stat = duplicate.stat()
        duplicate_item_id = "duplicateitem00000000001"
        with db.get_db() as conn:
            conn.execute(
                """INSERT INTO recovered_channel_items
                   (item_id,case_id,video_id,source_path,suffix,size_bytes,device,inode,mtime_ns,ctime_ns)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (duplicate_item_id,self.case_id,DAMAGED_ID,str(duplicate),".mp4",
                 duplicate_stat.st_size,duplicate_stat.st_dev,duplicate_stat.st_ino,
                 duplicate_stat.st_mtime_ns,duplicate_stat.st_ctime_ns),
            )
            conn.commit()
        duplicate_item = {"item_id": duplicate_item_id, "source_path": str(duplicate)}
        self.items[DAMAGED_ID + "-copy"] = duplicate_item
        self._seed_raw(duplicate_item, "clear", self._streams())
        result = reference_repair.ranked_references(self.case_id, self.items[DAMAGED_ID]["item_id"])
        self.assertNotIn(duplicate_item["item_id"], {item["item_id"] for item in result["references"]})

    def _seed_raw(self, item, health, streams):
        path = Path(item["source_path"])
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        with db.get_db() as conn:
            conn.execute(
                "UPDATE recovered_channel_items SET inspection_status='completed',health_tier=?,sha256=?,diagnosis_json=? WHERE item_id=?",
                (health, checksum, json.dumps({"source": {"sha256": checksum, "streams": streams}}), item["item_id"]),
            )
            conn.commit()

    def test_start_persists_job_without_accepting_client_paths(self) -> None:
        with patch.object(reference_repair, "_launch") as launch:
            job = reference_repair.start_repair(
                self.case_id, self.items[DAMAGED_ID]["item_id"],
                self.items[REFERENCE_ID]["item_id"], actor="tester",
            )
        self.assertEqual(job["status"], "queued")
        launch.assert_called_once_with(job["repair_id"])

    def test_only_one_global_repair_can_be_active(self) -> None:
        with patch.object(reference_repair, "_launch"):
            reference_repair.start_repair(
                self.case_id, self.items[DAMAGED_ID]["item_id"],
                self.items[REFERENCE_ID]["item_id"], actor="tester",
            )
            with self.assertRaisesRegex(reference_repair.ReferenceRepairError, "already active"):
                reference_repair.start_repair(
                    self.case_id, self.items[DAMAGED_ID]["item_id"],
                    self.items[REFERENCE_ID]["item_id"], actor="tester",
                )

    def test_private_job_directory_is_created_on_fresh_install(self) -> None:
        self.assertFalse(self.salvage.exists())
        directory = reference_repair._prepare_dir("freshjob")
        self.assertTrue(directory.is_dir())
        self.assertEqual(directory.parent.name, "reference-repairs")

    def test_comparison_labels_improvement_regression_and_uncertainty(self) -> None:
        no_streams = {"probeable": False, "streams": [], "warnings": ["bad"]}
        clean = {"probeable": True, "streams": [{"type": "video"}],
                 "decode_clean": True, "duration_seconds": 10, "warnings": []}
        self.assertEqual(reference_repair._compare(no_streams, clean), "better")
        self.assertEqual(reference_repair._compare(clean, no_streams), "worse")
        self.assertEqual(reference_repair._compare(clean, dict(clean)), "uncertain")

    def test_adopt_creates_verified_backup_and_preserves_target_symlink(self) -> None:
        repair_id, candidate, source_hash, candidate_hash = self._insert_completed_repair()
        link_dir = self.target / "Recovered Test [Recovered]"
        link_dir.mkdir()
        link = link_dir / f"{DAMAGED_ID}.mp4"
        link.symlink_to(self.damaged)
        unrelated_real = link_dir / "unrelated-real.mp4"
        unrelated_real.write_bytes(b"must-remain-untouched")
        unrelated_before = unrelated_real.stat()
        before_link = os.lstat(link)
        with db.get_db() as conn:
            conn.execute(
                "UPDATE recovered_channel_items SET organized_path=?,decision='approved' WHERE item_id=?",
                (str(link), self.items[DAMAGED_ID]["item_id"]),
            )
            conn.commit()
        with patch.object(reference_repair, "_tool_version", return_value="untrunc-test"):
            plan = reference_repair.build_action_plan(self.case_id, repair_id, "adopt", actor="tester")
        result = reference_repair.apply_action(
            self.case_id, repair_id, "adopt", plan["plan_token"], plan["confirmation"], actor="tester"
        )
        self.assertEqual(self.damaged.read_bytes(), candidate.read_bytes())
        self.assertEqual(stat_mode(self.damaged), 0o640)
        with db.get_db() as conn:
            row = conn.execute("SELECT * FROM recovered_media_repairs WHERE repair_id=?", (repair_id,)).fetchone()
            item = conn.execute("SELECT * FROM recovered_channel_items WHERE item_id=?", (self.items[DAMAGED_ID]["item_id"],)).fetchone()
        self.assertEqual(Path(row["backup_path"]).read_bytes(), b"damaged-original-bytes")
        self.assertEqual(row["backup_sha256"], source_hash)
        self.assertEqual(item["sha256"], candidate_hash)
        self.assertEqual(item["decision"], "approved")
        self.assertEqual((before_link.st_dev, before_link.st_ino), (os.lstat(link).st_dev, os.lstat(link).st_ino))
        self.assertEqual(os.readlink(link), str(self.damaged))
        unrelated_after = unrelated_real.stat()
        self.assertEqual(unrelated_real.read_bytes(), b"must-remain-untouched")
        self.assertEqual(
            (unrelated_before.st_dev, unrelated_before.st_ino),
            (unrelated_after.st_dev, unrelated_after.st_ino),
        )

    def test_restore_requires_new_confirmation_and_restores_exact_original(self) -> None:
        repair_id, _candidate, source_hash, _candidate_hash = self._insert_completed_repair()
        with patch.object(reference_repair, "_tool_version", return_value="test"):
            adopt = reference_repair.build_action_plan(self.case_id, repair_id, "adopt", actor="tester")
        reference_repair.apply_action(self.case_id, repair_id, "adopt", adopt["plan_token"], adopt["confirmation"], actor="tester")
        with patch.object(reference_repair, "_tool_version", return_value="test"):
            restore = reference_repair.build_action_plan(self.case_id, repair_id, "restore", actor="tester")
        result = reference_repair.apply_action(self.case_id, repair_id, "restore", restore["plan_token"], restore["confirmation"], actor="tester")
        self.assertEqual(self.damaged.read_bytes(), b"damaged-original-bytes")
        self.assertEqual(result["repair"]["status"], "restored")
        with db.get_db() as conn:
            item = conn.execute("SELECT sha256 FROM recovered_channel_items WHERE item_id=?", (self.items[DAMAGED_ID]["item_id"],)).fetchone()
        self.assertEqual(item["sha256"], source_hash)

    def test_wrong_confirmation_never_changes_source(self) -> None:
        repair_id, _candidate, _source_hash, _candidate_hash = self._insert_completed_repair()
        with patch.object(reference_repair, "_tool_version", return_value="test"):
            plan = reference_repair.build_action_plan(self.case_id, repair_id, "adopt", actor="tester")
        with self.assertRaisesRegex(reference_repair.ReferenceRepairError, "confirmation"):
            reference_repair.apply_action(self.case_id, repair_id, "adopt", plan["plan_token"], "WRONG", actor="tester")
        self.assertEqual(self.damaged.read_bytes(), b"damaged-original-bytes")
        self.assertEqual(reference_repair.get_repair(self.case_id, repair_id)["status"], "completed")

    def test_source_change_after_plan_is_never_overwritten(self) -> None:
        repair_id, _candidate, _source_hash, _candidate_hash = self._insert_completed_repair()
        with patch.object(reference_repair, "_tool_version", return_value="test"):
            plan = reference_repair.build_action_plan(self.case_id, repair_id, "adopt", actor="tester")
        self.damaged.write_bytes(b"external-change")
        with self.assertRaises(reference_repair.ReferenceRepairError):
            reference_repair.apply_action(self.case_id, repair_id, "adopt", plan["plan_token"], plan["confirmation"], actor="tester")
        self.assertEqual(self.damaged.read_bytes(), b"external-change")

    def test_startup_reconciliation_requires_verified_backup(self) -> None:
        repair_id, candidate, _source_hash, candidate_hash = self._insert_completed_repair()
        self.damaged.write_bytes(candidate.read_bytes())
        st = self.damaged.stat()
        with db.get_db() as conn:
            conn.execute(
                "UPDATE recovered_media_repairs SET status='adopting',phase='adopt',backup_path=?,backup_sha256=? WHERE repair_id=?",
                (str(candidate.parent / "missing-backup.mp4"), hashlib.sha256(b"old").hexdigest(), repair_id),
            )
            conn.execute(
                "UPDATE recovered_channel_items SET size_bytes=?,device=?,inode=?,mtime_ns=?,ctime_ns=? WHERE item_id=?",
                (st.st_size, st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns, self.items[DAMAGED_ID]["item_id"]),
            )
            conn.commit()
        reference_repair.recover_interrupted_repairs()
        repair = reference_repair.get_repair(self.case_id, repair_id)
        self.assertEqual(repair["status"], "recovery_blocked")
        self.assertEqual(repair["candidate_sha256"], candidate_hash)

    def test_recorded_backup_is_reused_after_safe_staging_failure(self) -> None:
        repair_id, _candidate, _source_hash, _candidate_hash = self._insert_completed_repair()
        with patch.object(reference_repair, "_tool_version", return_value="test"):
            plan = reference_repair.build_action_plan(self.case_id, repair_id, "adopt", actor="tester")
        with patch.object(
            reference_repair, "_copy_fd_at", side_effect=OSError("simulated staging failure")
        ):
            with self.assertRaisesRegex(OSError, "staging failure"):
                reference_repair.apply_action(
                    self.case_id, repair_id, "adopt", plan["plan_token"],
                    plan["confirmation"], actor="tester",
                )
        repair = reference_repair.get_repair(self.case_id, repair_id)
        self.assertEqual(repair["status"], "completed")
        self.assertTrue(repair["has_backup"])
        with patch.object(reference_repair, "_tool_version", return_value="test"):
            retry_plan = reference_repair.build_action_plan(self.case_id, repair_id, "adopt", actor="tester")
        result = reference_repair.apply_action(
            self.case_id, repair_id, "adopt", retry_plan["plan_token"],
            retry_plan["confirmation"], actor="tester",
        )
        self.assertEqual(result["repair"]["status"], "adopted")

    def test_planned_but_missing_backup_is_created_safely(self) -> None:
        repair_id, candidate, _source_hash, _candidate_hash = self._insert_completed_repair()
        planned = candidate.parent / "original-backup.mp4"
        with db.get_db() as conn:
            conn.execute(
                "UPDATE recovered_media_repairs SET backup_path=? WHERE repair_id=?",
                (str(planned), repair_id),
            )
            conn.commit()
        self.assertFalse(planned.exists())
        plan = reference_repair.build_action_plan(
            self.case_id, repair_id, "adopt", actor="tester"
        )
        result = reference_repair.apply_action(
            self.case_id, repair_id, "adopt", plan["plan_token"],
            plan["confirmation"], actor="tester",
        )
        self.assertEqual(result["repair"]["status"], "adopted")
        self.assertEqual(planned.read_bytes(), b"damaged-original-bytes")

    def test_failed_backup_copy_removes_private_partial(self) -> None:
        repair_id, _candidate, _source_hash, _candidate_hash = self._insert_completed_repair()
        plan = reference_repair.build_action_plan(
            self.case_id, repair_id, "adopt", actor="tester"
        )

        def fail_backup(_source_fd, destination, _mode):
            destination.write_bytes(b"partial-backup")
            raise OSError("simulated backup disk failure")

        with patch.object(reference_repair, "_copy_fd", side_effect=fail_backup):
            with self.assertRaisesRegex(OSError, "backup disk failure"):
                reference_repair.apply_action(
                    self.case_id, repair_id, "adopt", plan["plan_token"],
                    plan["confirmation"], actor="tester",
                )
        job_dir = self.salvage / "reference-repairs" / repair_id
        self.assertEqual(list(job_dir.glob(".backup-*.partial*")), [])
        self.assertEqual(self.damaged.read_bytes(), b"damaged-original-bytes")

    def test_cancelled_queued_worker_cannot_overwrite_cancellation(self) -> None:
        with patch.object(reference_repair, "_launch"):
            queued = reference_repair.start_repair(
                self.case_id, self.items[DAMAGED_ID]["item_id"],
                self.items[REFERENCE_ID]["item_id"], actor="tester",
            )
        reference_repair.cancel_repair(self.case_id, queued["repair_id"], actor="tester")
        reference_repair._runtimes[queued["repair_id"]] = reference_repair._Runtime()
        reference_repair._run_worker(queued["repair_id"])
        self.assertEqual(
            reference_repair.get_repair(self.case_id, queued["repair_id"])["status"],
            "cancelled",
        )

    def test_restore_does_not_depend_on_candidate_artifact_survival(self) -> None:
        repair_id, candidate, _source_hash, _candidate_hash = self._insert_completed_repair()
        with patch.object(reference_repair, "_tool_version", return_value="test"):
            adopt = reference_repair.build_action_plan(self.case_id, repair_id, "adopt", actor="tester")
        reference_repair.apply_action(
            self.case_id, repair_id, "adopt", adopt["plan_token"], adopt["confirmation"], actor="tester"
        )
        candidate.unlink()
        with patch.object(reference_repair, "_tool_version", return_value="test"):
            restore = reference_repair.build_action_plan(self.case_id, repair_id, "restore", actor="tester")
        result = reference_repair.apply_action(
            self.case_id, repair_id, "restore", restore["plan_token"], restore["confirmation"], actor="tester"
        )
        self.assertEqual(result["repair"]["status"], "restored")
        self.assertEqual(self.damaged.read_bytes(), b"damaged-original-bytes")

    def test_active_repair_blocks_recovered_item_edits_and_inspection_sync(self) -> None:
        with patch.object(reference_repair, "_launch"):
            reference_repair.start_repair(
                self.case_id, self.items[DAMAGED_ID]["item_id"],
                self.items[REFERENCE_ID]["item_id"], actor="tester",
            )
        with self.assertRaisesRegex(recovered_channels.RecoveredChannelError, "active"):
            recovered_channels.update_item(
                self.case_id, self.items[DAMAGED_ID]["item_id"],
                {"decision": "hold"}, actor="tester",
            )
        with self.assertRaisesRegex(recovered_channels.RecoveredChannelError, "active reference repair"):
            recovered_channels.start_inspection(self.case_id, actor="tester", background=False)

    def test_worker_uses_private_suffix_links_and_persists_valid_candidate(self) -> None:
        with patch.object(reference_repair, "_launch"):
            queued = reference_repair.start_repair(
                self.case_id, self.items[DAMAGED_ID]["item_id"],
                self.items[REFERENCE_ID]["item_id"], actor="tester",
            )
        repair_id = queued["repair_id"]
        seen_command = []

        class FakeProcess:
            pid = 999999
            def __init__(self, command, **_kwargs):
                seen_command.extend(command)
                output = Path(command[command.index("-dst") + 1])
                output.write_bytes(b"worker-candidate")
            def poll(self): return 0
            def wait(self, timeout=None): return 0

        reports = [
            {"probeable": False, "streams": [], "warnings": ["missing moov"],
             "health_tier": "inconclusive", "sha256": hashlib.sha256(self.damaged.read_bytes()).hexdigest()},
            {"probeable": True, "streams": self._streams(), "warnings": [],
             "health_tier": "clear", "decode_clean": True, "duration_seconds": 60,
             "sha256": hashlib.sha256(b"worker-candidate").hexdigest()},
        ]
        runtime = reference_repair._Runtime()
        reference_repair._runtimes[repair_id] = runtime
        with (
            patch.object(reference_repair, "_tool_version", return_value="test-untrunc"),
            patch.object(reference_repair.subprocess, "Popen", FakeProcess),
            patch.object(reference_repair, "_inspect_fd", side_effect=reports),
        ):
            reference_repair._run_worker(repair_id)
        result = reference_repair.get_repair(self.case_id, repair_id)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["assessment"], "better")
        self.assertIn("-n", seen_command)
        self.assertIn("-dst", seen_command)
        self.assertTrue(any(str(value).endswith("reference.mp4") for value in seen_command))
        self.assertTrue(any(str(value).endswith("damaged.mp4") for value in seen_command))

    def _queued_worker(self) -> str:
        with patch.object(reference_repair, "_launch"):
            queued = reference_repair.start_repair(
                self.case_id, self.items[DAMAGED_ID]["item_id"],
                self.items[REFERENCE_ID]["item_id"], actor="tester",
            )
        reference_repair._runtimes[queued["repair_id"]] = reference_repair._Runtime()
        return queued["repair_id"]

    def test_untrunc_nonzero_exit_is_durable_and_preserves_source(self) -> None:
        repair_id = self._queued_worker()

        class FailedProcess:
            pid = 999991
            def __init__(self, _command, **_kwargs): pass
            def poll(self): return 7
            def wait(self, timeout=None): return 7

        before = {"probeable": False, "streams": [], "warnings": ["broken"]}
        with (
            patch.object(reference_repair, "_tool_version", return_value="test"),
            patch.object(reference_repair.subprocess, "Popen", FailedProcess),
            patch.object(reference_repair, "_inspect_fd", return_value=before),
        ):
            reference_repair._run_worker(repair_id)
        result = reference_repair.get_repair(self.case_id, repair_id)
        self.assertEqual(result["status"], "failed")
        self.assertIn("status 7", result["error"])
        self.assertEqual(self.damaged.read_bytes(), b"damaged-original-bytes")

    def test_timeout_and_excessive_output_fail_closed(self) -> None:
        class RunningProcess:
            pid = 999992
            def __init__(self, command, **_kwargs):
                self.command = command
            def poll(self): return None
            def wait(self, timeout=None): return 0

        before = {"probeable": False, "streams": [], "warnings": ["broken"]}
        repair_id = self._queued_worker()
        with (
            patch.object(reference_repair, "_tool_version", return_value="test"),
            patch.object(reference_repair, "UNTRUNC_JOB_TIMEOUT_SECONDS", 0),
            patch.object(reference_repair.subprocess, "Popen", RunningProcess),
            patch.object(reference_repair, "_inspect_fd", return_value=before),
            patch.object(reference_repair.os, "killpg"),
        ):
            reference_repair._run_worker(repair_id)
        self.assertIn(
            "time limit", reference_repair.get_repair(self.case_id, repair_id)["error"]
        )

        repair_id = self._queued_worker()

        class OversizedProcess(RunningProcess):
            def __init__(self, command, **kwargs):
                super().__init__(command, **kwargs)
                Path(command[command.index("-dst") + 1]).write_bytes(b"x" * 32)

        with (
            patch.object(reference_repair, "_tool_version", return_value="test"),
            patch.object(reference_repair, "UNTRUNC_MAX_OUTPUT_BYTES", 8),
            patch.object(reference_repair.subprocess, "Popen", OversizedProcess),
            patch.object(reference_repair, "_inspect_fd", return_value=before),
            patch.object(reference_repair.os, "killpg"),
        ):
            reference_repair._run_worker(repair_id)
        self.assertIn(
            "output exceeded", reference_repair.get_repair(self.case_id, repair_id)["error"]
        )

    def test_insufficient_storage_and_post_output_cancel_remove_candidate(self) -> None:
        before = {"probeable": False, "streams": [], "warnings": ["broken"]}
        repair_id = self._queued_worker()

        class NoSpace:
            free = 0

        with (
            patch.object(reference_repair, "_tool_version", return_value="test"),
            patch.object(reference_repair, "_inspect_fd", return_value=before),
            patch.object(reference_repair.shutil, "disk_usage", return_value=NoSpace()),
        ):
            reference_repair._run_worker(repair_id)
        self.assertIn(
            "Insufficient storage", reference_repair.get_repair(self.case_id, repair_id)["error"]
        )

        repair_id = self._queued_worker()

        class CandidateProcess:
            pid = 999993
            def __init__(self, command, **_kwargs):
                Path(command[command.index("-dst") + 1]).write_bytes(b"candidate-before-cancel")
            def poll(self): return 0
            def wait(self, timeout=None): return 0

        with (
            patch.object(reference_repair, "_tool_version", return_value="test"),
            patch.object(reference_repair.subprocess, "Popen", CandidateProcess),
            patch.object(
                reference_repair, "_inspect_fd",
                side_effect=[before, reference_repair.RepairCancelled("cancelled in final inspection")],
            ),
        ):
            reference_repair._run_worker(repair_id)
        result = reference_repair.get_repair(self.case_id, repair_id)
        self.assertEqual(result["status"], "cancelled")
        candidate = self.salvage / "reference-repairs" / repair_id / "candidate.mp4"
        self.assertFalse(candidate.exists())


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


if __name__ == "__main__":
    unittest.main()
