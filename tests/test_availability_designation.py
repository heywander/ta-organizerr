from __future__ import annotations

import errno
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ta_organizerr import (
    availability,
    availability_designation,
    channel_designation,
    db,
)
from ta_organizerr.media_reservations import _reset_reservations_for_tests


CHANNEL_ID = "UCtbyZ5js_2m1LLi4nrrnFfw"
VIDEO_ID = "g58pQud8er0"
SECOND_VIDEO_ID = "abcdefghijk"


class AvailabilityDesignationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.target = self.base / "target"
        self.hidden = self.base / "hidden"
        self.data = self.base / "data"
        self.target.mkdir()
        self.hidden.mkdir()
        self.data.mkdir()
        self.patchers = (
            patch.object(db, "DB_PATH", self.data / "videos.db"),
            patch.object(availability_designation, "TARGET_DIR", self.target),
            patch.object(availability_designation, "HIDDEN_DIR", self.hidden),
            patch.object(availability_designation, "DATA_DIR", self.data),
        )
        for patcher in self.patchers:
            patcher.start()
        db.init_db()
        _reset_reservations_for_tests()

    def tearDown(self) -> None:
        _reset_reservations_for_tests()
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary_directory.cleanup()

    def _identity(
        self,
        *,
        component: str = "The Oversphere [Recovered]",
        provenance: str = "recovered",
        revision: int = 2,
    ) -> None:
        with db.get_db() as conn:
            conn.execute(
                """
                INSERT INTO channel_identities
                    (channel_id, display_name, provenance, storage_component,
                     current_state, current_reason_code, revision)
                VALUES (?, 'The Oversphere', ?, ?, 'terminated_explicit',
                        'account_terminated', ?)
                """,
                (CHANNEL_ID, provenance, component, revision),
            )
            for job_id, observed_at in (
                ("first", "2026-07-15T00:00:00+00:00"),
                ("second", "2026-07-16T01:00:00+00:00"),
            ):
                conn.execute(
                    """
                    INSERT INTO availability_observations
                        (job_id, subject_type, subject_id, channel_id,
                         normalized_state, reason_code, provider, observed_at)
                    VALUES (?, 'channel', ?, ?, 'terminated_explicit',
                            'account_terminated', 'test', ?)
                    """,
                    (job_id, CHANNEL_ID, CHANNEL_ID, observed_at),
                )
            conn.commit()

    def _apply(self, plan: dict) -> dict:
        return availability_designation.apply_designation(
            CHANNEL_ID,
            plan["plan_token"],
            f"TERMINATE {CHANNEL_ID}",
            actor="tester",
        )

    def _make_minimum_separation_override_available(self) -> None:
        raw_reason = (
            f"ERROR: [youtube:tab] {CHANNEL_ID}: YouTube said: This channel was "
            "removed because it violated our Community Guidelines."
        )
        evidence_json = json.dumps({
            "classifier_version": "availability-v2",
            "api_resource_exists": False,
            "youtube_api": None,
            "yt_dlp": {"returncode": 1, "raw_reason": raw_reason},
        })
        with db.get_db() as conn:
            conn.execute(
                "DELETE FROM availability_observations WHERE channel_id = ?",
                (CHANNEL_ID,),
            )
            conn.execute(
                """
                UPDATE channel_identities
                SET current_state = 'terminated_explicit',
                    current_reason_code = 'channel_terminated'
                WHERE channel_id = ?
                """,
                (CHANNEL_ID,),
            )
            for observation_id, job_id, observed_at in (
                (1, "override-first", "2026-07-17T02:34:00+00:00"),
                (2, "override-second", "2026-07-17T02:38:00+00:00"),
            ):
                conn.execute(
                    """
                    INSERT INTO availability_observations
                        (observation_id, job_id, subject_type, subject_id,
                         channel_id, normalized_state, reason_code, provider,
                         raw_reason, evidence_json, observed_at)
                    VALUES (?, ?, 'channel', ?, ?, 'terminated_explicit',
                            'channel_terminated', 'yt-dlp', ?, ?, ?)
                    """,
                    (
                        observation_id,
                        job_id,
                        CHANNEL_ID,
                        CHANNEL_ID,
                        raw_reason,
                        evidence_json,
                        observed_at,
                    ),
                )
            conn.commit()

    def test_unpublished_recovered_case_gets_metadata_only_future_component(self) -> None:
        component = "The Oversphere [Recovered]"
        self._identity(component=component)
        with db.get_db() as conn:
            conn.execute(
                """
                INSERT INTO recovered_channel_cases
                    (case_id, source_channel_id, target_channel_name,
                     target_component, created_by)
                VALUES ('case', ?, 'The Oversphere', ?, 'tester')
                """,
                (CHANNEL_ID, component),
            )
            conn.commit()

        plan = availability_designation.build_designation_plan(CHANNEL_ID)
        result = self._apply(plan)

        self.assertEqual(plan["mode"], "metadata_only")
        self.assertTrue(plan["safe_to_apply"])
        self.assertEqual(result["mode"], "metadata_only")
        self.assertFalse(any(self.target.iterdir()))
        with db.get_db() as conn:
            identity = conn.execute(
                "SELECT * FROM channel_identities WHERE channel_id = ?", (CHANNEL_ID,)
            ).fetchone()
            case = conn.execute(
                "SELECT * FROM recovered_channel_cases WHERE case_id = 'case'"
            ).fetchone()
            event = conn.execute(
                "SELECT event FROM recovered_channel_events WHERE case_id = 'case'"
            ).fetchone()
        expected = "The Oversphere [Recovered] [Terminated]"
        self.assertEqual(identity["storage_component"], expected)
        self.assertEqual(identity["designation"], "terminated")
        self.assertEqual(identity["designation_confirmed_by"], "tester")
        self.assertEqual(case["target_component"], expected)
        self.assertEqual(event["event"], "channel_designated_terminated")

    def test_metadata_only_rechecks_that_no_folder_appeared_before_commit(self) -> None:
        component = "The Oversphere [Recovered]"
        self._identity(component=component)
        plan = availability_designation.build_designation_plan(CHANNEL_ID)
        original_commit = availability_designation._commit_designation

        def race(details, actor, **kwargs):
            appeared = self.target / component
            appeared.mkdir()
            (appeared / "unrelated.mp4").write_bytes(b"do not claim")
            return original_commit(details, actor, **kwargs)

        with (
            patch.object(
                availability_designation,
                "_commit_designation",
                side_effect=race,
            ),
            self.assertRaisesRegex(
                availability.AvailabilityError, "appeared after the dry run"
            ),
        ):
            self._apply(plan)

        with db.get_db() as conn:
            identity = conn.execute(
                "SELECT designation, storage_component FROM channel_identities "
                "WHERE channel_id = ?",
                (CHANNEL_ID,),
            ).fetchone()
        self.assertEqual(identity["designation"], "none")
        self.assertEqual(identity["storage_component"], component)
        self.assertEqual(
            (self.target / component / "unrelated.mp4").read_bytes(),
            b"do not claim",
        )

    def test_existing_terminated_component_can_be_confirmed_without_rename(self) -> None:
        component = "Channel [Terminated]"
        self._identity(component=component, provenance="normal")
        folder = self.target / component
        folder.mkdir()
        media = folder / f"{VIDEO_ID}.mp4"
        media.write_bytes(b"real media")
        identity_before = (folder.stat().st_dev, folder.stat().st_ino)
        with db.get_db() as conn:
            conn.execute(
                """
                INSERT INTO videos
                    (video_id, channel_id, symlink, status, adopted_real)
                VALUES (?, ?, ?, 'adopted', 1)
                """,
                (VIDEO_ID, CHANNEL_ID, str(media)),
            )
            conn.commit()

        plan = availability_designation.build_designation_plan(CHANNEL_ID)
        result = self._apply(plan)

        self.assertEqual(plan["mode"], "metadata_only_existing")
        self.assertTrue(plan["safe_to_apply"])
        self.assertEqual(result["mode"], "metadata_only_existing")
        self.assertEqual((folder.stat().st_dev, folder.stat().st_ino), identity_before)
        self.assertEqual(media.read_bytes(), b"real media")
        with db.get_db() as conn:
            identity = conn.execute(
                "SELECT designation, storage_component FROM channel_identities "
                "WHERE channel_id = ?",
                (CHANNEL_ID,),
            ).fetchone()
        self.assertEqual(identity["designation"], "terminated")
        self.assertEqual(identity["storage_component"], component)

    def test_physical_rename_updates_owned_paths_and_preserves_media(self) -> None:
        component = "The Oversphere [Recovered]"
        self._identity(component=component)
        source = self.target / component
        source.mkdir()
        real = source / f"{VIDEO_ID}.mp4"
        real.write_bytes(b"real media")
        link = source / f"{SECOND_VIDEO_ID}.mp4"
        raw_target = f"/archive/{SECOND_VIDEO_ID}.mp4"
        link.symlink_to(raw_target)
        real_identity = (real.stat().st_dev, real.stat().st_ino)
        with db.get_db() as conn:
            conn.execute(
                """
                INSERT INTO videos
                    (video_id, channel, channel_id, symlink, status, adopted_real)
                VALUES (?, ?, ?, ?, 'adopted', 1)
                """,
                (VIDEO_ID, component, CHANNEL_ID, str(real)),
            )
            conn.execute(
                """
                INSERT INTO videos
                    (video_id, channel, channel_id, symlink, status)
                VALUES (?, ?, ?, ?, 'recovered')
                """,
                (SECOND_VIDEO_ID, component, CHANNEL_ID, str(link)),
            )
            conn.commit()

        plan = availability_designation.build_designation_plan(CHANNEL_ID)
        result = self._apply(plan)

        destination = self.target / f"{component} [Terminated]"
        self.assertEqual(plan["mode"], "rename")
        self.assertTrue(result["success"])
        self.assertFalse(source.exists())
        self.assertEqual((destination / real.name).read_bytes(), b"real media")
        moved_stat = (destination / real.name).stat()
        self.assertEqual((moved_stat.st_dev, moved_stat.st_ino), real_identity)
        self.assertEqual(os.readlink(destination / link.name), raw_target)
        with db.get_db() as conn:
            rows = conn.execute(
                "SELECT video_id, symlink, channel FROM videos ORDER BY video_id"
            ).fetchall()
            identity = conn.execute(
                "SELECT designation, storage_component FROM channel_identities WHERE channel_id = ?",
                (CHANNEL_ID,),
            ).fetchone()
            event = conn.execute(
                "SELECT actor, details_json FROM availability_designation_events "
                "WHERE channel_id = ?",
                (CHANNEL_ID,),
            ).fetchone()
        self.assertTrue(all(Path(row["symlink"]).parent == destination for row in rows))
        recovered = next(row for row in rows if row["video_id"] == SECOND_VIDEO_ID)
        self.assertEqual(recovered["channel"], destination.name)
        self.assertEqual(identity["designation"], "terminated")
        self.assertEqual(identity["storage_component"], destination.name)
        self.assertEqual(event["actor"], "tester")
        self.assertFalse(
            json.loads(event["details_json"])["minimum_separation_overridden"]
        )

    def test_administrator_override_bypasses_only_minimum_separation(self) -> None:
        component = "The Oversphere [Recovered]"
        self._identity(component=component)
        self._make_minimum_separation_override_available()
        source = self.target / component
        source.mkdir()
        real = source / f"{VIDEO_ID}.mp4"
        real.write_bytes(b"real media")
        link = source / f"{SECOND_VIDEO_ID}.mp4"
        raw_target = f"/archive/{SECOND_VIDEO_ID}.mp4"
        link.symlink_to(raw_target)
        real_identity = (real.stat().st_dev, real.stat().st_ino)
        with db.get_db() as conn:
            conn.execute(
                """
                INSERT INTO videos
                    (video_id, channel_id, symlink, status, adopted_real)
                VALUES (?, ?, ?, 'adopted', 1)
                """,
                (VIDEO_ID, CHANNEL_ID, str(real)),
            )
            conn.execute(
                """
                INSERT INTO videos
                    (video_id, channel_id, symlink, status)
                VALUES (?, ?, ?, 'recovered')
                """,
                (SECOND_VIDEO_ID, CHANNEL_ID, str(link)),
            )
            conn.commit()

        normal_plan = availability_designation.build_designation_plan(CHANNEL_ID)
        override_plan = availability_designation.build_designation_plan(
            CHANNEL_ID,
            override_minimum_separation=True,
            actor="tester",
        )

        self.assertFalse(normal_plan["safe_to_apply"])
        self.assertIn(
            "termination_evidence_incomplete",
            {item["code"] for item in normal_plan["blockers"]},
        )
        self.assertTrue(override_plan["safe_to_apply"])
        self.assertNotEqual(normal_plan["plan_token"], override_plan["plan_token"])
        self.assertTrue(override_plan["minimum_separation_overridden"])
        self.assertEqual(
            override_plan["confirmation"],
            f"BYPASS 24H AND TERMINATE {CHANNEL_ID}",
        )
        self.assertTrue(any(
            "24-hour evidence separation" in warning
            for warning in override_plan["warnings"]
        ))
        with self.assertRaises(availability.AvailabilityError):
            availability_designation.apply_designation(
                CHANNEL_ID,
                override_plan["plan_token"],
                f"TERMINATE {CHANNEL_ID}",
                actor="tester",
                override_minimum_separation=True,
            )
        with self.assertRaises(availability.AvailabilityError):
            availability_designation.apply_designation(
                CHANNEL_ID,
                override_plan["plan_token"],
                f"TERMINATE {CHANNEL_ID}",
                actor="tester",
            )
        with self.assertRaises(availability.AvailabilityError):
            availability_designation.apply_designation(
                CHANNEL_ID,
                override_plan["plan_token"],
                f"BYPASS 24H AND TERMINATE {CHANNEL_ID}",
                actor="different-admin",
                override_minimum_separation=True,
            )

        with patch.object(
            channel_designation,
            "_rename_noreplace",
            side_effect=OSError(errno.EINVAL, os.strerror(errno.EINVAL)),
        ):
            result = availability_designation.apply_designation(
                CHANNEL_ID,
                override_plan["plan_token"],
                f"BYPASS 24H AND TERMINATE {CHANNEL_ID}",
                actor="tester",
                override_minimum_separation=True,
            )

        destination = self.target / f"{component} [Terminated]"
        self.assertTrue(result["minimum_separation_overridden"])
        self.assertEqual(result["rename_method"], "renameat_compat")
        self.assertFalse(source.exists())
        self.assertEqual((destination / real.name).read_bytes(), b"real media")
        moved_stat = (destination / real.name).stat()
        self.assertEqual((moved_stat.st_dev, moved_stat.st_ino), real_identity)
        self.assertEqual(os.readlink(destination / link.name), raw_target)
        with db.get_db() as conn:
            event = conn.execute(
                """
                SELECT actor, event, details_json
                FROM availability_designation_events
                WHERE channel_id = ?
                """,
                (CHANNEL_ID,),
            ).fetchone()
            identity = conn.execute(
                "SELECT designation_confirmed_by FROM channel_identities "
                "WHERE channel_id = ?",
                (CHANNEL_ID,),
            ).fetchone()
        details = json.loads(event["details_json"])
        self.assertEqual(event["actor"], "tester")
        self.assertEqual(event["event"], "channel_designated_terminated")
        self.assertTrue(details["minimum_separation_overridden"])
        self.assertEqual(details["rename_method"], "renameat_compat")
        self.assertEqual(len(details["override_basis"]), 2)
        self.assertEqual(len(details["plan_token_sha256"]), 64)
        self.assertEqual(identity["designation_confirmed_by"], "tester")

    def test_override_rejects_one_response_and_keeps_other_blockers(self) -> None:
        component = "Channel"
        self._identity(component=component, provenance="normal")
        self._make_minimum_separation_override_available()
        source = self.target / component
        source.mkdir()
        with db.get_db() as conn:
            conn.execute(
                "DELETE FROM availability_observations WHERE observation_id = 2"
            )
            conn.execute(
                """
                INSERT INTO availability_jobs (job_id, kind, channel_id, status)
                VALUES ('active', 'channel', ?, 'running')
                """,
                (CHANNEL_ID,),
            )
            conn.commit()

        plan = availability_designation.build_designation_plan(
            CHANNEL_ID,
            override_minimum_separation=True,
            actor="tester",
        )

        blocker_codes = {item["code"] for item in plan["blockers"]}
        self.assertIn("minimum_separation_override_unavailable", blocker_codes)
        self.assertIn("active_jobs", blocker_codes)
        self.assertFalse(plan["safe_to_apply"])
        self.assertTrue(source.is_dir())

    def test_override_plan_is_invalidated_when_exact_evidence_changes(self) -> None:
        component = "Channel"
        self._identity(component=component, provenance="normal")
        self._make_minimum_separation_override_available()
        source = self.target / component
        source.mkdir()
        plan = availability_designation.build_designation_plan(
            CHANNEL_ID,
            override_minimum_separation=True,
            actor="tester",
        )
        with db.get_db() as conn:
            conn.execute(
                """
                UPDATE availability_observations
                SET raw_reason = 'generic unavailable'
                WHERE observation_id = 2
                """
            )
            conn.commit()

        with self.assertRaises(availability.AvailabilityError):
            availability_designation.apply_designation(
                CHANNEL_ID,
                plan["plan_token"],
                f"BYPASS 24H AND TERMINATE {CHANNEL_ID}",
                actor="tester",
                override_minimum_separation=True,
            )

        self.assertTrue(source.is_dir())
        with db.get_db() as conn:
            event_count = conn.execute(
                "SELECT COUNT(*) AS count FROM availability_designation_events"
            ).fetchone()["count"]
        self.assertEqual(event_count, 0)

    def test_override_rejects_a_replaced_basis_during_final_transaction(self) -> None:
        component = "Channel"
        self._identity(component=component, provenance="normal")
        self._make_minimum_separation_override_available()
        source = self.target / component
        source.mkdir()
        media = source / f"{VIDEO_ID}.mp4"
        media.write_bytes(b"real media")
        with db.get_db() as conn:
            conn.execute(
                """
                INSERT INTO videos
                    (video_id, channel_id, symlink, status, adopted_real)
                VALUES (?, ?, ?, 'adopted', 1)
                """,
                (VIDEO_ID, CHANNEL_ID, str(media)),
            )
            template = conn.execute(
                "SELECT * FROM availability_observations WHERE observation_id = 1"
            ).fetchone()
            conn.commit()
        plan = availability_designation.build_designation_plan(
            CHANNEL_ID,
            override_minimum_separation=True,
            actor="tester",
        )
        original_commit = availability_designation._commit_designation

        def replace_basis(details, actor, **kwargs):
            with db.get_db() as conn:
                conn.execute(
                    "UPDATE availability_observations SET raw_reason = 'invalidated' "
                    "WHERE observation_id IN (1, 2)"
                )
                for observation_id, job_id, observed_at in (
                    (3, "replacement-first", "2026-07-17T03:00:00+00:00"),
                    (4, "replacement-second", "2026-07-17T03:04:00+00:00"),
                ):
                    conn.execute(
                        """
                        INSERT INTO availability_observations
                            (observation_id, job_id, subject_type, subject_id,
                             channel_id, normalized_state, reason_code, provider,
                             raw_reason, evidence_json, observed_at)
                        VALUES (?, ?, 'channel', ?, ?, 'terminated_explicit',
                                'channel_terminated', 'yt-dlp', ?, ?, ?)
                        """,
                        (
                            observation_id,
                            job_id,
                            CHANNEL_ID,
                            CHANNEL_ID,
                            template["raw_reason"],
                            template["evidence_json"],
                            observed_at,
                        ),
                    )
                conn.commit()
            return original_commit(details, actor, **kwargs)

        with (
            patch.object(
                availability_designation,
                "_commit_designation",
                side_effect=replace_basis,
            ),
            self.assertRaisesRegex(
                availability.AvailabilityError,
                "override evidence changed",
            ),
        ):
            availability_designation.apply_designation(
                CHANNEL_ID,
                plan["plan_token"],
                f"BYPASS 24H AND TERMINATE {CHANNEL_ID}",
                actor="tester",
                override_minimum_separation=True,
            )

        destination = self.target / f"{component} [Terminated]"
        self.assertTrue(source.is_dir())
        self.assertFalse(destination.exists())
        self.assertEqual(media.read_bytes(), b"real media")
        with db.get_db() as conn:
            identity = conn.execute(
                "SELECT designation, storage_component FROM channel_identities "
                "WHERE channel_id = ?",
                (CHANNEL_ID,),
            ).fetchone()
            event_count = conn.execute(
                "SELECT COUNT(*) AS count FROM availability_designation_events"
            ).fetchone()["count"]
        self.assertEqual(identity["designation"], "none")
        self.assertEqual(identity["storage_component"], component)
        self.assertEqual(event_count, 0)

    def test_audit_failure_rolls_back_registry_and_folder_rename(self) -> None:
        component = "Channel"
        self._identity(component=component, provenance="normal")
        source = self.target / component
        source.mkdir()
        media = source / f"{VIDEO_ID}.mp4"
        media.write_bytes(b"real media")
        with db.get_db() as conn:
            conn.execute(
                """
                INSERT INTO videos
                    (video_id, channel_id, symlink, status, adopted_real)
                VALUES (?, ?, ?, 'adopted', 1)
                """,
                (VIDEO_ID, CHANNEL_ID, str(media)),
            )
            conn.execute(
                """
                CREATE TRIGGER reject_designation_audit
                BEFORE INSERT ON availability_designation_events
                BEGIN SELECT RAISE(ABORT, 'forced audit failure'); END
                """
            )
            conn.commit()
        plan = availability_designation.build_designation_plan(CHANNEL_ID)

        with self.assertRaises(availability.AvailabilityError):
            self._apply(plan)

        destination = self.target / f"{component} [Terminated]"
        self.assertTrue(source.is_dir())
        self.assertFalse(destination.exists())
        self.assertEqual(media.read_bytes(), b"real media")
        with db.get_db() as conn:
            identity = conn.execute(
                "SELECT designation, storage_component FROM channel_identities "
                "WHERE channel_id = ?",
                (CHANNEL_ID,),
            ).fetchone()
            stored_path = conn.execute(
                "SELECT symlink FROM videos WHERE video_id = ?",
                (VIDEO_ID,),
            ).fetchone()["symlink"]
            event_count = conn.execute(
                "SELECT COUNT(*) AS count FROM availability_designation_events"
            ).fetchone()["count"]
        self.assertEqual(identity["designation"], "none")
        self.assertEqual(identity["storage_component"], component)
        self.assertEqual(stored_path, str(media))
        self.assertEqual(event_count, 0)

    def test_verified_existing_destination_consolidates_and_keeps_existing_paths(self) -> None:
        component = "Channel"
        destination_component = "Channel [Terminated]"
        self._identity(component=component, provenance="normal")
        source = self.target / component
        destination = self.target / destination_component
        source.mkdir()
        destination.mkdir()
        old_media = source / "old.mp4"
        existing_media = destination / "existing.mp4"
        old_media.write_bytes(b"old")
        existing_media.write_bytes(b"existing")
        with db.get_db() as conn:
            conn.execute(
                """
                INSERT INTO videos
                    (video_id, channel_id, symlink, status, adopted_real)
                VALUES (?, ?, ?, 'adopted', 1)
                """,
                (VIDEO_ID, CHANNEL_ID, str(old_media)),
            )
            conn.execute(
                """
                INSERT INTO videos
                    (video_id, channel_id, symlink, status, adopted_real)
                VALUES (?, ?, ?, 'adopted', 1)
                """,
                (SECOND_VIDEO_ID, CHANNEL_ID, str(existing_media)),
            )
            conn.commit()

        plan = availability_designation.build_designation_plan(CHANNEL_ID)
        self._apply(plan)

        self.assertEqual(plan["mode"], "consolidate")
        self.assertFalse(source.exists())
        self.assertEqual((destination / "old.mp4").read_bytes(), b"old")
        self.assertEqual(existing_media.read_bytes(), b"existing")
        with db.get_db() as conn:
            paths = {
                row["video_id"]: row["symlink"]
                for row in conn.execute("SELECT video_id, symlink FROM videos")
            }
        self.assertEqual(paths[VIDEO_ID], str(destination / "old.mp4"))
        self.assertEqual(paths[SECOND_VIDEO_ID], str(existing_media))

    def test_plan_is_invalid_after_new_probe_revision(self) -> None:
        component = "Channel"
        self._identity(component=component, provenance="normal")
        (self.target / component).mkdir()
        plan = availability_designation.build_designation_plan(CHANNEL_ID)
        with db.get_db() as conn:
            conn.execute(
                "UPDATE channel_identities SET revision = revision + 1 WHERE channel_id = ?",
                (CHANNEL_ID,),
            )
            conn.commit()

        with self.assertRaises(availability.AvailabilityError) as raised:
            self._apply(plan)

        self.assertEqual(raised.exception.status, 409)
        self.assertTrue((self.target / component).exists())

    def test_active_job_and_wrong_confirmation_block_without_mutation(self) -> None:
        component = "Channel"
        self._identity(component=component, provenance="normal")
        source = self.target / component
        source.mkdir()
        with db.get_db() as conn:
            conn.execute(
                """
                INSERT INTO availability_jobs
                    (job_id, kind, channel_id, status)
                VALUES ('job', 'channel', ?, 'running')
                """,
                (CHANNEL_ID,),
            )
            conn.commit()

        plan = availability_designation.build_designation_plan(CHANNEL_ID)
        self.assertIn("active_jobs", {item["code"] for item in plan["blockers"]})
        with self.assertRaises(availability.AvailabilityError):
            availability_designation.apply_designation(
                CHANNEL_ID,
                plan["plan_token"],
                "yes",
                actor="tester",
            )
        self.assertTrue(source.exists())

    def test_failed_startup_recovery_leaves_all_designation_actions_blocked(self) -> None:
        component = "Channel"
        self._identity(component=component, provenance="normal")
        source = self.target / component
        source.mkdir()
        original = availability_designation.build_designation_plan(CHANNEL_ID)
        journal_dir = self.data / "channel-designation-journals"
        journal_dir.mkdir(mode=0o700, exist_ok=True)
        pending = channel_designation._journal_from_plan(
            availability_designation._build_plan_details(CHANNEL_ID)["engine_plan"]
        )
        channel_designation._write_journal(journal_dir, pending)

        blocked = availability_designation.build_designation_plan(CHANNEL_ID)

        self.assertIn(
            "unresolved_designation_journal",
            {item["code"] for item in blocked["blockers"]},
        )
        with self.assertRaises(availability.AvailabilityError):
            self._apply(original)
        self.assertTrue(source.is_dir())

        with (
            patch.object(
                availability_designation,
                "_database_state_for",
                return_value="mixed",
            ),
            self.assertRaises(channel_designation.ChannelDesignationError) as raised,
        ):
            availability_designation.recover_pending_designations()
        self.assertEqual(raised.exception.code, "recovery_blocked")

    def test_unpublished_component_never_claims_an_unrelated_physical_folder(self) -> None:
        component = "The Oversphere [Recovered]"
        self._identity(component=component)
        unrelated = self.hidden / component
        unrelated.mkdir()
        (unrelated / "unrelated-real-file.mp4").write_bytes(b"do not relabel")
        with db.get_db() as conn:
            conn.execute(
                """
                INSERT INTO recovered_channel_cases
                    (case_id, source_channel_id, target_channel_name,
                     target_component, created_by)
                VALUES ('case', ?, 'The Oversphere', ?, 'tester')
                """,
                (CHANNEL_ID, component),
            )
            conn.commit()

        plan = availability_designation.build_designation_plan(CHANNEL_ID)

        self.assertFalse(plan["safe_to_apply"])
        self.assertIn(
            "source_identity_ambiguous",
            {item["code"] for item in plan["blockers"]},
        )
        self.assertTrue(unrelated.is_dir())
        self.assertFalse(
            (self.hidden / f"{component} [Terminated]").exists()
        )

    def test_stale_database_path_cannot_claim_unrelated_physical_folder(self) -> None:
        component = "The Oversphere [Recovered]"
        self._identity(component=component)
        unrelated = self.hidden / component
        unrelated.mkdir()
        (unrelated / "unrelated-real-file.mp4").write_bytes(b"do not relabel")
        stale = unrelated / f"{VIDEO_ID}.mp4"
        with db.get_db() as conn:
            conn.execute(
                """
                INSERT INTO videos
                    (video_id, channel_id, symlink, status)
                VALUES (?, ?, ?, 'recovered')
                """,
                (VIDEO_ID, CHANNEL_ID, str(stale)),
            )
            conn.commit()

        plan = availability_designation.build_designation_plan(CHANNEL_ID)

        self.assertFalse(plan["safe_to_apply"])
        self.assertIn(
            "source_identity_ambiguous",
            {item["code"] for item in plan["blockers"]},
        )
        self.assertTrue(unrelated.is_dir())


if __name__ == "__main__":
    unittest.main()
