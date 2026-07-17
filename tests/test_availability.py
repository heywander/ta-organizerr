from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from ta_organizerr import availability, db


CHANNEL_ID = "UCtbyZ5js_2m1LLi4nrrnFfw"
VIDEO_ID = "8jaoFHJ0SQE"
OTHER_CHANNEL_ID = "UC0000000000000000000000"


class AvailabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "data" / "videos.db"
        self.source = self.root / "source"
        self.target = self.root / "target"
        self.hidden = self.root / "hidden"
        self.source.mkdir()
        self.target.mkdir()
        self.hidden.mkdir()
        self.patches = [
            patch.object(db, "DB_PATH", self.db_path),
            patch.object(availability, "SOURCE_DIR", self.source),
            patch.object(availability, "TARGET_DIR", self.target),
            patch.object(availability, "HIDDEN_DIR", self.hidden),
            patch.object(availability, "AVAILABILITY_PROBE_DELAY_SECONDS", 0),
            patch.object(availability, "AVAILABILITY_RETRY_BACKOFF_SECONDS", 0),
        ]
        for active in self.patches:
            active.start()
        availability._clear_source_inventory_cache_for_tests()
        db.init_db()

    def tearDown(self) -> None:
        availability._clear_source_inventory_cache_for_tests()
        for active in reversed(self.patches):
            active.stop()
        self.tmp.cleanup()

    def _identity(self) -> None:
        with db.get_db() as conn:
            availability.upsert_channel_identity(conn, CHANNEL_ID, provenance="source")
            conn.commit()

    @staticmethod
    def _channel_error(message: str, channel_id: str = CHANNEL_ID) -> str:
        return (
            f"ERROR: [youtube:tab] {channel_id}: YouTube said: {message}"
        )

    def test_generic_unavailable_is_never_termination(self) -> None:
        result = availability._classify_failure("channel", "ERROR: This channel is not available")
        self.assertEqual(result["state"], "unavailable_unknown")
        self.assertNotEqual(result["reason_code"], "channel_terminated")

    def test_exact_channel_removal_provider_message_is_explicit_termination(self) -> None:
        messages = (
            "This channel was removed because it violated our Community Guidelines.",
            "This account has been removed for violating YouTube's Community Guidelines.",
            "This account has been terminated for violating our Community Guidelines.",
            "This channel has been suspended.",
        )
        for message in messages:
            with self.subTest(message=message):
                result = availability._classify_failure(
                    "channel",
                    self._channel_error(message),
                    requested_subject_id=CHANNEL_ID,
                )
                self.assertEqual(result["state"], "terminated_explicit")
                self.assertEqual(
                    result["reason_code"],
                    "channel_suspended" if "suspended" in message else "channel_terminated",
                )

    def test_channel_termination_requires_anchored_exact_requested_id(self) -> None:
        message = "This channel was removed because it violated our Community Guidelines."
        cases = (
            self._channel_error(message, OTHER_CHANNEL_ID),
            f"prefix {self._channel_error(message)}",
            f"ERROR: [youtube] {CHANNEL_ID}: YouTube said: {message}",
            f"ERROR: [youtube:tab] {CHANNEL_ID}: A video on this channel was removed "
            "because it violated our Community Guidelines.",
        )
        for reason in cases:
            with self.subTest(reason=reason):
                result = availability._classify_failure(
                    "channel", reason, requested_subject_id=CHANNEL_ID
                )
                self.assertEqual(result["state"], "unavailable_unknown")

    def test_channel_identity_match_is_case_sensitive(self) -> None:
        message = "This channel was removed because it violated our Community Guidelines."
        case_changed = "UCTbyZ5js_2m1LLi4nrrnFfw"
        result = availability._classify_failure(
            "channel",
            self._channel_error(message, case_changed),
            requested_subject_id=CHANNEL_ID,
        )
        self.assertEqual(result["state"], "unavailable_unknown")

    def test_negated_or_uncertain_enforcement_is_not_explicit(self) -> None:
        messages = (
            "This channel has not been terminated or suspended.",
            "This channel may be suspended.",
            "A linked account has been terminated.",
            "This channel discusses terminated accounts.",
        )
        for message in messages:
            with self.subTest(message=message):
                result = availability._classify_failure(
                    "channel",
                    self._channel_error(message),
                    requested_subject_id=CHANNEL_ID,
                )
                self.assertEqual(result["state"], "unavailable_unknown")

    def test_transient_and_restricted_channel_markers_take_precedence(self) -> None:
        explicit = self._channel_error(
            "This channel was removed because it violated our Community Guidelines."
        )
        transient = availability._classify_failure(
            "channel",
            f"HTTP Error 429: Too Many Requests\n{explicit}",
            requested_subject_id=CHANNEL_ID,
        )
        restricted = availability._classify_failure(
            "channel",
            f"This channel is not available in your country\n{explicit}",
            requested_subject_id=CHANNEL_ID,
        )
        self.assertEqual(transient["state"], "transient_failure")
        self.assertEqual(restricted["state"], "restricted")

    def test_geo_block_with_copyright_claim_is_not_policy_removal(self) -> None:
        result = availability._classify_failure(
            "video",
            "This video is blocked in your country because of a copyright claim",
        )
        self.assertEqual(result["state"], "restricted")
        self.assertEqual(result["reason_code"], "access_restricted")

    def test_explicit_termination_requires_two_observations_24_hours_apart(self) -> None:
        self._identity()
        result = {
            "state": "terminated_explicit",
            "reason_code": "channel_terminated",
            "raw_reason": "This account has been terminated",
            "provider": "yt-dlp",
            "evidence": {},
        }
        availability.record_observation(
            "first", "channel", CHANNEL_ID, CHANNEL_ID, result,
            observed_at="2026-07-15T00:00:00+00:00",
        )
        self.assertFalse(availability.termination_eligibility(CHANNEL_ID)["eligible"])
        availability.record_observation(
            "second", "channel", CHANNEL_ID, CHANNEL_ID, result,
            observed_at="2026-07-16T00:00:00+00:00",
        )
        self.assertTrue(availability.termination_eligibility(CHANNEL_ID)["eligible"])

    def test_accessible_observation_between_explicit_results_blocks_candidate(self) -> None:
        self._identity()
        terminated = {
            "state": "terminated_explicit", "reason_code": "channel_terminated",
            "raw_reason": "This account has been terminated", "evidence": {},
        }
        accessible = {
            "state": "accessible", "reason_code": "public_response",
            "raw_reason": "Public metadata was returned", "evidence": {},
        }
        availability.record_observation("one", "channel", CHANNEL_ID, CHANNEL_ID, terminated, observed_at="2026-07-14T00:00:00+00:00")
        availability.record_observation("ok", "channel", CHANNEL_ID, CHANNEL_ID, accessible, observed_at="2026-07-15T00:00:00+00:00")
        availability.record_observation("two", "channel", CHANNEL_ID, CHANNEL_ID, terminated, observed_at="2026-07-16T00:00:00+00:00")
        self.assertFalse(availability.termination_eligibility(CHANNEL_ID)["eligible"])

    def test_legacy_channel_removal_reclassification_is_atomic_and_idempotent(self) -> None:
        self._identity()
        raw_reason = self._channel_error(
            "This channel was removed because it violated our Community Guidelines."
        )
        evidence_json = json.dumps({
            "classifier_version": "availability-v1",
            "api_resource_exists": False,
            "youtube_api": None,
            "yt_dlp": {"returncode": 1},
        })
        with db.get_db() as conn:
            for job_id, observed_at in (
                ("legacy-one", "2026-07-14T00:00:00+00:00"),
                ("legacy-two", "2026-07-15T01:00:00+00:00"),
            ):
                conn.execute(
                    """
                    INSERT INTO availability_observations
                        (job_id, subject_type, subject_id, channel_id,
                         normalized_state, reason_code, provider, raw_reason,
                         evidence_json, tool_version, observed_at)
                    VALUES (?, 'channel', ?, ?, 'unavailable_unknown',
                            'channel_unavailable_unknown',
                            'yt-dlp+youtube-data-api', ?, ?, '2026.03.17', ?)
                    """,
                    (
                        job_id,
                        CHANNEL_ID,
                        CHANNEL_ID,
                        raw_reason,
                        evidence_json,
                        observed_at,
                    ),
                )
            conn.execute(
                """
                UPDATE channel_identities
                SET current_state = 'unavailable_unknown',
                    current_reason_code = 'channel_unavailable_unknown',
                    current_state_at = '2026-07-15T01:00:00+00:00',
                    last_accessible_at = '2026-07-10T00:00:00+00:00',
                    storage_component = 'The Oversphere [Recovered]',
                    designation = 'terminated',
                    designation_confirmed_at = '2026-07-16T00:00:00+00:00',
                    designation_confirmed_by = 'tester', revision = 7
                WHERE channel_id = ?
                """,
                (CHANNEL_ID,),
            )
            conn.commit()

        first = availability.reclassify_legacy_channel_observations()
        second = availability.reclassify_legacy_channel_observations()

        self.assertEqual(first["reclassified_observations"], 2)
        self.assertEqual(first["affected_channels"], 1)
        self.assertEqual(second["reclassified_observations"], 0)
        with db.get_db() as conn:
            observations = conn.execute(
                "SELECT normalized_state, reason_code, raw_reason, evidence_json "
                "FROM availability_observations ORDER BY observation_id"
            ).fetchall()
            identity = conn.execute(
                "SELECT current_state, current_reason_code, current_state_at, "
                "last_accessible_at, storage_component, revision, designation, "
                "designation_confirmed_at, designation_confirmed_by "
                "FROM channel_identities WHERE channel_id = ?",
                (CHANNEL_ID,),
            ).fetchone()
        self.assertTrue(all(row["normalized_state"] == "terminated_explicit" for row in observations))
        self.assertTrue(all(row["reason_code"] == "channel_terminated" for row in observations))
        self.assertTrue(all(row["raw_reason"] == raw_reason for row in observations))
        repaired_evidence = [json.loads(row["evidence_json"]) for row in observations]
        self.assertTrue(all(item["classifier_version"] == "availability-v2" for item in repaired_evidence))
        self.assertTrue(all(item["yt_dlp"] == {"returncode": 1} for item in repaired_evidence))
        self.assertTrue(all(item["classification_history"][-1]["from"]["classifier_version"] == "availability-v1" for item in repaired_evidence))
        self.assertEqual(identity["current_state"], "terminated_explicit")
        self.assertEqual(identity["current_reason_code"], "channel_terminated")
        self.assertEqual(identity["current_state_at"], "2026-07-15T01:00:00+00:00")
        self.assertEqual(identity["revision"], 8)
        self.assertEqual(identity["last_accessible_at"], "2026-07-10T00:00:00+00:00")
        self.assertEqual(identity["storage_component"], "The Oversphere [Recovered]")
        self.assertEqual(identity["designation"], "terminated")
        self.assertEqual(identity["designation_confirmed_at"], "2026-07-16T00:00:00+00:00")
        self.assertEqual(identity["designation_confirmed_by"], "tester")
        eligibility = availability.termination_eligibility(CHANNEL_ID)
        self.assertTrue(eligibility["eligible"])
        self.assertEqual(eligibility["status"], "eligible")
        self.assertEqual(eligibility["threshold_progress"], 2)
        self.assertEqual(len(eligibility["counting_observations"]), 2)
        self.assertFalse(eligibility["api_corroboration_required"])

    def test_legacy_reclassification_rolls_back_every_change_on_failure(self) -> None:
        self._identity()
        raw_reason = self._channel_error(
            "This channel was removed because it violated our Community Guidelines."
        )
        evidence_json = json.dumps({
            "classifier_version": "availability-v1",
            "api_resource_exists": False,
            "youtube_api": None,
        })
        with db.get_db() as conn:
            for job_id in ("first", "second"):
                conn.execute(
                    """
                    INSERT INTO availability_observations
                        (job_id, subject_type, subject_id, channel_id,
                         normalized_state, reason_code, provider, raw_reason,
                         evidence_json, observed_at)
                    VALUES (?, 'channel', ?, ?, 'unavailable_unknown',
                            'channel_unavailable_unknown', 'yt-dlp', ?, ?,
                            '2026-07-15T00:00:00+00:00')
                    """,
                    (job_id, CHANNEL_ID, CHANNEL_ID, raw_reason, evidence_json),
                )
            conn.execute(
                "UPDATE channel_identities SET revision = 4 WHERE channel_id = ?",
                (CHANNEL_ID,),
            )
            conn.execute(
                """
                CREATE TRIGGER fail_second_reclassification
                BEFORE UPDATE ON availability_observations
                WHEN OLD.job_id = 'second'
                BEGIN SELECT RAISE(ABORT, 'forced migration failure'); END
                """
            )
            conn.commit()

        with self.assertRaises(sqlite3.Error):
            availability.reclassify_legacy_channel_observations()

        with db.get_db() as conn:
            rows = conn.execute(
                "SELECT normalized_state, reason_code, evidence_json "
                "FROM availability_observations ORDER BY observation_id"
            ).fetchall()
            revision = conn.execute(
                "SELECT revision FROM channel_identities WHERE channel_id = ?",
                (CHANNEL_ID,),
            ).fetchone()["revision"]
        self.assertTrue(all(row["normalized_state"] == "unavailable_unknown" for row in rows))
        self.assertTrue(all(row["reason_code"] == "channel_unavailable_unknown" for row in rows))
        self.assertTrue(all(row["evidence_json"] == evidence_json for row in rows))
        self.assertEqual(revision, 4)

    def test_concurrent_legacy_reclassification_bumps_revision_once(self) -> None:
        self._identity()
        raw_reason = self._channel_error(
            "This channel was removed because it violated our Community Guidelines."
        )
        evidence_json = json.dumps({
            "classifier_version": "availability-v1",
            "api_resource_exists": False,
            "youtube_api": None,
        })
        with db.get_db() as conn:
            conn.execute(
                """
                INSERT INTO availability_observations
                    (job_id, subject_type, subject_id, channel_id,
                     normalized_state, reason_code, provider, raw_reason,
                     evidence_json, observed_at)
                VALUES ('legacy', 'channel', ?, ?, 'unavailable_unknown',
                        'channel_unavailable_unknown', 'yt-dlp', ?, ?,
                        '2026-07-15T00:00:00+00:00')
                """,
                (CHANNEL_ID, CHANNEL_ID, raw_reason, evidence_json),
            )
            conn.execute(
                "UPDATE channel_identities SET revision = 5 WHERE channel_id = ?",
                (CHANNEL_ID,),
            )
            conn.commit()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(
                lambda _: availability.reclassify_legacy_channel_observations(),
                range(2),
            ))

        self.assertEqual(
            sum(result["reclassified_observations"] for result in results),
            1,
        )
        with db.get_db() as conn:
            identity = conn.execute(
                "SELECT current_state, revision FROM channel_identities WHERE channel_id = ?",
                (CHANNEL_ID,),
            ).fetchone()
        self.assertEqual(identity["current_state"], "terminated_explicit")
        self.assertEqual(identity["revision"], 6)

    def test_oversized_legacy_evidence_keeps_a_bounded_v2_audit_envelope(self) -> None:
        self._identity()
        raw_reason = self._channel_error(
            "This channel was removed because it violated our Community Guidelines."
        )
        evidence_json = json.dumps({
            "classifier_version": "availability-v1",
            "api_resource_exists": False,
            "youtube_api": None,
            "yt_dlp": {"returncode": 1},
            "classification_history": [{
                "legacy_detail": "x" * (availability._MAX_EVIDENCE_JSON - 400),
            }],
        })
        self.assertLess(len(evidence_json), availability._MAX_EVIDENCE_JSON)
        with db.get_db() as conn:
            conn.execute(
                """
                INSERT INTO availability_observations
                    (job_id, subject_type, subject_id, channel_id,
                     normalized_state, reason_code, provider, raw_reason,
                     evidence_json, observed_at)
                VALUES ('legacy-large', 'channel', ?, ?, 'unavailable_unknown',
                        'channel_unavailable_unknown', 'yt-dlp', ?, ?,
                        '2026-07-15T00:00:00+00:00')
                """,
                (CHANNEL_ID, CHANNEL_ID, raw_reason, evidence_json),
            )
            conn.commit()

        availability.reclassify_legacy_channel_observations()

        with db.get_db() as conn:
            row = conn.execute(
                "SELECT normalized_state, reason_code, evidence_json "
                "FROM availability_observations"
            ).fetchone()
        repaired = json.loads(row["evidence_json"])
        self.assertEqual(row["normalized_state"], "terminated_explicit")
        self.assertEqual(row["reason_code"], "channel_terminated")
        self.assertEqual(repaired["classifier_version"], "availability-v2")
        self.assertEqual(
            repaired["classification_history"][-1]["from"]["classifier_version"],
            "availability-v1",
        )
        self.assertFalse(repaired["prior_evidence"]["preserved_inline"])
        self.assertEqual(repaired["prior_evidence"]["size_bytes"], len(evidence_json))
        self.assertEqual(repaired["prior_classification_history"]["entry_count"], 1)
        self.assertLessEqual(len(row["evidence_json"]), availability._MAX_EVIDENCE_JSON)

    def test_malformed_explicit_evidence_gets_a_bounded_v2_audit(self) -> None:
        self._identity()
        malformed_evidence = "not-json"
        with db.get_db() as conn:
            for job_id, message, observed_at in (
                (
                    "still-explicit",
                    "This channel was removed because it violated our Community Guidelines.",
                    "2026-07-14T00:00:00+00:00",
                ),
                (
                    "false-positive",
                    "This channel has not been terminated or suspended.",
                    "2026-07-15T00:00:00+00:00",
                ),
            ):
                conn.execute(
                    """
                    INSERT INTO availability_observations
                        (job_id, subject_type, subject_id, channel_id,
                         normalized_state, reason_code, provider, raw_reason,
                         evidence_json, observed_at)
                    VALUES (?, 'channel', ?, ?, 'terminated_explicit',
                            'channel_terminated', 'yt-dlp', ?, ?, ?)
                    """,
                    (
                        job_id,
                        CHANNEL_ID,
                        CHANNEL_ID,
                        self._channel_error(message),
                        malformed_evidence,
                        observed_at,
                    ),
                )
            conn.commit()

        first = availability.reclassify_legacy_channel_observations()
        second = availability.reclassify_legacy_channel_observations()

        self.assertEqual(first["reclassified_observations"], 2)
        self.assertEqual(second["reclassified_observations"], 0)
        with db.get_db() as conn:
            rows = conn.execute(
                "SELECT job_id, normalized_state, reason_code, evidence_json "
                "FROM availability_observations ORDER BY observation_id"
            ).fetchall()
        self.assertEqual(rows[0]["normalized_state"], "terminated_explicit")
        self.assertEqual(rows[0]["reason_code"], "channel_terminated")
        self.assertEqual(rows[1]["normalized_state"], "unavailable_unknown")
        self.assertEqual(rows[1]["reason_code"], "channel_unavailable_unknown")
        for row in rows:
            repaired = json.loads(row["evidence_json"])
            self.assertEqual(repaired["classifier_version"], "availability-v2")
            self.assertEqual(
                repaired["classification_history"][-1]["from"]["classifier_version"],
                "unknown",
            )
            self.assertTrue(repaired["prior_evidence"]["preserved_inline"])
            self.assertEqual(repaired["prior_evidence"]["raw_text"], malformed_evidence)
            self.assertLessEqual(
                len(row["evidence_json"]), availability._MAX_EVIDENCE_JSON
            )

    def test_legacy_api_presence_becomes_conflicting_evidence(self) -> None:
        self._identity()
        raw_reason = self._channel_error(
            "This channel was removed because it violated our Community Guidelines."
        )
        evidence_json = json.dumps({
            "classifier_version": "availability-v1",
            "api_resource_exists": True,
            "youtube_api": {"state": "accessible"},
        })
        with db.get_db() as conn:
            conn.execute(
                """
                INSERT INTO availability_observations
                    (job_id, subject_type, subject_id, channel_id,
                     normalized_state, reason_code, provider, raw_reason,
                     evidence_json, observed_at)
                VALUES ('legacy', 'channel', ?, ?, 'unavailable_unknown',
                        'channel_unavailable_unknown',
                        'yt-dlp+youtube-data-api', ?, ?,
                        '2026-07-15T00:00:00+00:00')
                """,
                (CHANNEL_ID, CHANNEL_ID, raw_reason, evidence_json),
            )
            conn.execute(
                """
                UPDATE channel_identities
                SET current_state = 'unavailable_unknown',
                    current_reason_code = 'channel_unavailable_unknown'
                WHERE channel_id = ?
                """,
                (CHANNEL_ID,),
            )
            conn.commit()

        summary = availability.reclassify_legacy_channel_observations()

        self.assertEqual(summary["conflicting_observations"], 1)
        with db.get_db() as conn:
            observation = conn.execute(
                "SELECT normalized_state, reason_code, raw_reason, evidence_json "
                "FROM availability_observations"
            ).fetchone()
            identity = conn.execute(
                "SELECT current_state, current_reason_code FROM channel_identities "
                "WHERE channel_id = ?",
                (CHANNEL_ID,),
            ).fetchone()
        self.assertEqual(observation["normalized_state"], "conflicting_evidence")
        self.assertEqual(observation["reason_code"], "api_reports_accessible")
        self.assertEqual(observation["raw_reason"], raw_reason)
        repaired_evidence = json.loads(observation["evidence_json"])
        self.assertEqual(repaired_evidence["classifier_version"], "availability-v2")
        self.assertTrue(repaired_evidence["api_resource_exists"])
        self.assertEqual(
            repaired_evidence["classification_history"][-1]["to"]["state"],
            "conflicting_evidence",
        )
        self.assertEqual(identity["current_state"], "conflicting_evidence")
        self.assertFalse(availability.termination_eligibility(CHANNEL_ID)["eligible"])

    def test_legacy_explicit_rows_are_revalidated_and_suspension_stays_compatible(self) -> None:
        self._identity()
        evidence_json = json.dumps({
            "classifier_version": "availability-v1",
            "api_resource_exists": False,
            "youtube_api": None,
            "yt_dlp": {"returncode": 1},
        })
        with db.get_db() as conn:
            for job_id, message, reason_code, observed_at in (
                (
                    "valid-suspended",
                    "This channel has been suspended.",
                    "channel_suspended",
                    "2026-07-14T00:00:00+00:00",
                ),
                (
                    "false-positive",
                    "This channel has not been terminated or suspended.",
                    "channel_terminated",
                    "2026-07-14T01:00:00+00:00",
                ),
            ):
                conn.execute(
                    """
                    INSERT INTO availability_observations
                        (job_id, subject_type, subject_id, channel_id,
                         normalized_state, reason_code, provider, raw_reason,
                         evidence_json, observed_at)
                    VALUES (?, 'channel', ?, ?, 'terminated_explicit', ?,
                            'yt-dlp', ?, ?, ?)
                    """,
                    (
                        job_id,
                        CHANNEL_ID,
                        CHANNEL_ID,
                        reason_code,
                        self._channel_error(message),
                        evidence_json,
                        observed_at,
                    ),
                )
            conn.commit()

        summary = availability.reclassify_legacy_channel_observations()
        self.assertEqual(summary["reclassified_observations"], 2)
        with db.get_db() as conn:
            rows = conn.execute(
                "SELECT job_id, normalized_state, reason_code, evidence_json "
                "FROM availability_observations ORDER BY observation_id"
            ).fetchall()
        self.assertEqual(rows[0]["normalized_state"], "terminated_explicit")
        self.assertEqual(rows[0]["reason_code"], "channel_suspended")
        self.assertEqual(rows[1]["normalized_state"], "unavailable_unknown")
        self.assertEqual(rows[1]["reason_code"], "channel_unavailable_unknown")
        self.assertTrue(all(
            json.loads(row["evidence_json"])["classifier_version"] == "availability-v2"
            for row in rows
        ))

        availability.record_observation(
            "new-suspended",
            "channel",
            CHANNEL_ID,
            CHANNEL_ID,
            {
                "state": "terminated_explicit",
                "reason_code": "channel_suspended",
                "raw_reason": self._channel_error("This channel has been suspended."),
                "evidence": {"classifier_version": "availability-v2"},
            },
            observed_at="2026-07-16T00:00:00+00:00",
        )
        self.assertTrue(availability.termination_eligibility(CHANNEL_ID)["eligible"])

    def test_eligibility_diagnostics_explain_waiting_and_evidence_reset(self) -> None:
        waiting_rows = [
            {
                "observation_id": 1,
                "normalized_state": "terminated_explicit",
                "reason_code": "channel_terminated",
                "observed_at": "2026-07-17T02:34:00+00:00",
            },
            {
                "observation_id": 2,
                "normalized_state": "terminated_explicit",
                "reason_code": "channel_terminated",
                "observed_at": "2026-07-17T02:38:00+00:00",
            },
        ]
        waiting = availability._termination_eligibility_from_rows(
            "terminated_explicit",
            "channel_terminated",
            waiting_rows,
            now=availability._parse_time("2026-07-17T03:00:00+00:00"),
        )
        self.assertFalse(waiting["eligible"])
        self.assertEqual(waiting["status"], "waiting_for_interval")
        self.assertEqual(waiting["matching_observations"], 2)
        self.assertEqual(waiting["threshold_progress"], 1)
        self.assertEqual(waiting["qualifying_observations_needed"], 1)
        self.assertEqual(
            waiting["next_qualifying_observation_at"],
            "2026-07-18T02:34:00+00:00",
        )

        reset_rows = waiting_rows + [{
            "observation_id": 3,
            "normalized_state": "accessible",
            "reason_code": "public_response",
            "observed_at": "2026-07-17T04:00:00+00:00",
        }]
        reset = availability._termination_eligibility_from_rows(
            "accessible",
            "public_response",
            reset_rows,
            now=availability._parse_time("2026-07-17T05:00:00+00:00"),
        )
        self.assertEqual(reset["status"], "evidence_reset")
        self.assertEqual(reset["threshold_progress"], 0)
        self.assertEqual(reset["matching_observations"], 0)
        self.assertEqual(reset["historical_matching_observations"], 2)
        self.assertEqual(reset["last_blocking_observation"]["observation_id"], 3)

        mixed_offset_reset = availability._termination_eligibility_from_rows(
            "accessible",
            "public_response",
            [
                {
                    "observation_id": 2,
                    "normalized_state": "accessible",
                    "reason_code": "public_response",
                    "observed_at": "2026-07-15T23:30:00-01:00",
                },
                {
                    "observation_id": 1,
                    "normalized_state": "terminated_explicit",
                    "reason_code": "channel_terminated",
                    "observed_at": "2026-07-16T00:00:00+00:00",
                },
            ],
            now=availability._parse_time("2026-07-16T02:00:00+00:00"),
        )
        self.assertEqual(mixed_offset_reset["status"], "evidence_reset")
        self.assertEqual(
            mixed_offset_reset["last_blocking_observation"]["observation_id"],
            2,
        )
        self.assertEqual(
            mixed_offset_reset["last_blocking_observation"]["state"],
            "accessible",
        )

    def test_eligibility_uses_exact_utc_boundary_and_exact_qualifying_pair(self) -> None:
        rows = [
            {
                "observation_id": 1,
                "normalized_state": "terminated_explicit",
                "reason_code": "channel_terminated",
                "observed_at": "2026-07-15T01:00:00+01:00",
            },
            {
                "observation_id": 2,
                "normalized_state": "terminated_explicit",
                "reason_code": "channel_terminated",
                "observed_at": "2026-07-15T01:00:00+00:00",
            },
            {
                "observation_id": 3,
                "normalized_state": "terminated_explicit",
                "reason_code": "channel_terminated",
                "observed_at": "2026-07-15T20:00:00-05:00",
            },
        ]
        result = availability._termination_eligibility_from_rows(
            "terminated_explicit",
            "channel_terminated",
            rows,
            now=availability._parse_time("2026-07-16T01:00:00+00:00"),
        )
        self.assertTrue(result["eligible"])
        self.assertEqual(result["qualified_at"], rows[2]["observed_at"])
        self.assertEqual(
            [item["observation_id"] for item in result["counting_observations"]],
            [1, 3],
        )

    def test_eligibility_rejects_short_future_and_malformed_timestamps(self) -> None:
        baseline = {
            "observation_id": 1,
            "normalized_state": "terminated_explicit",
            "reason_code": "channel_terminated",
            "observed_at": "2026-07-15T00:00:00+00:00",
        }
        too_early = {
            **baseline,
            "observation_id": 2,
            "observed_at": "2026-07-15T23:59:59.999999+00:00",
        }
        short = availability._termination_eligibility_from_rows(
            "terminated_explicit",
            "channel_terminated",
            [baseline, too_early],
            now=availability._parse_time("2026-07-16T00:00:00+00:00"),
        )
        self.assertFalse(short["eligible"])
        self.assertEqual(short["threshold_progress"], 1)

        future = availability._termination_eligibility_from_rows(
            "terminated_explicit",
            "channel_terminated",
            [
                baseline,
                {
                    **baseline,
                    "observation_id": 3,
                    "observed_at": "2026-07-18T00:00:00+00:00",
                },
            ],
            now=availability._parse_time("2026-07-16T00:00:00+00:00"),
        )
        self.assertFalse(future["eligible"])
        self.assertEqual(future["threshold_progress"], 1)
        self.assertEqual(future["invalid_observation_count"], 1)
        self.assertEqual(future["invalid_observations"][0]["issue"], "future_timestamp")

        malformed = availability._termination_eligibility_from_rows(
            "terminated_explicit",
            "channel_terminated",
            [{**baseline, "observed_at": "not-a-time"}],
            now=availability._parse_time("2026-07-16T00:00:00+00:00"),
        )
        self.assertFalse(malformed["eligible"])
        self.assertEqual(malformed["status"], "invalid_timestamp_evidence")
        self.assertEqual(malformed["invalid_observation_count"], 1)

        valid_second = {
            **baseline,
            "observation_id": 4,
            "observed_at": "2026-07-16T01:00:00+00:00",
        }
        for blocker_state, blocker_time in (
            ("accessible", "not-a-time"),
            ("conflicting_evidence", "2026-07-18T00:00:00+00:00"),
        ):
            with self.subTest(blocker_state=blocker_state):
                blocked = availability._termination_eligibility_from_rows(
                    "terminated_explicit",
                    "channel_terminated",
                    [
                        baseline,
                        {
                            "observation_id": 5,
                            "normalized_state": blocker_state,
                            "reason_code": "blocking_result",
                            "observed_at": blocker_time,
                        },
                        valid_second,
                    ],
                    now=availability._parse_time("2026-07-16T02:00:00+00:00"),
                )
                self.assertFalse(blocked["eligible"])
                self.assertEqual(blocked["status"], "invalid_timestamp_evidence")
                self.assertEqual(
                    blocked["last_blocking_observation"]["state"],
                    blocker_state,
                )

    def test_data_api_presence_does_not_override_private_video(self) -> None:
        ytdlp = {
            "state": "private", "reason_code": "video_private",
            "raw_reason": "Private video", "evidence": {},
        }
        api = {
            "state": "available", "reason_code": "api_resource_returned",
            "raw_reason": "API returned it", "evidence": {"item_count": 1},
        }
        with patch.object(availability, "_tool_version", return_value="test"):
            result = availability._combine_probe("video", ytdlp, api)
        self.assertEqual(result["state"], "private")
        self.assertEqual(result["reason_code"], "video_private")
        self.assertEqual(
            result["evidence"]["classifier_version"],
            availability._CLASSIFIER_VERSION,
        )

    def test_channel_api_conflict_preserves_raw_provider_reason(self) -> None:
        raw_reason = self._channel_error(
            "This channel was removed because it violated our Community Guidelines."
        )
        ytdlp = {
            "state": "terminated_explicit",
            "reason_code": "channel_terminated",
            "raw_reason": raw_reason,
            "evidence": {"returncode": 1},
        }
        api = {
            "state": "accessible",
            "reason_code": "api_resource_returned",
            "raw_reason": "YouTube Data API returned the exact resource",
            "evidence": {"returned_id": CHANNEL_ID},
        }
        with patch.object(availability, "_tool_version", return_value="test"):
            result = availability._combine_probe("channel", ytdlp, api)
        self.assertEqual(result["state"], "conflicting_evidence")
        self.assertIn(raw_reason, result["raw_reason"])
        self.assertEqual(result["evidence"]["yt_dlp"]["raw_reason"], raw_reason)

    def test_ytdlp_uses_argv_timeout_and_no_shell(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="ERROR: Private video"
        )
        with patch.object(availability.subprocess, "run", return_value=completed) as run:
            result = availability._yt_dlp_probe("video", VIDEO_ID)
        self.assertEqual(result["state"], "private")
        args, kwargs = run.call_args
        self.assertIsInstance(args[0], list)
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["timeout"], availability.AVAILABILITY_PROBE_TIMEOUT_SECONDS)
        self.assertIn("--ignore-config", args[0])
        self.assertNotIn("--cookies", args[0])

    def test_channel_probe_uses_flat_playlist_isolation(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=f'{{"id":"{CHANNEL_ID}"}}',
            stderr="",
        )
        with patch.object(availability.subprocess, "run", return_value=completed) as run:
            result = availability._yt_dlp_probe("channel", CHANNEL_ID)
        self.assertEqual(result["state"], "accessible")
        argv = run.call_args.args[0]
        self.assertIn("--flat-playlist", argv)
        self.assertEqual(argv[argv.index("--playlist-end") + 1], "1")

    def test_channel_probe_classifies_exact_community_guidelines_removal(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=self._channel_error(
                "This channel was removed because it violated our Community Guidelines."
            ),
        )
        with patch.object(availability.subprocess, "run", return_value=completed):
            result = availability._yt_dlp_probe("channel", CHANNEL_ID)
        self.assertEqual(result["state"], "terminated_explicit")
        self.assertEqual(result["reason_code"], "channel_terminated")
        self.assertEqual(result["raw_reason"], completed.stderr)

    def test_structured_ytdlp_access_markers_and_exact_identity_are_enforced(self) -> None:
        cases = [
            ({"id": VIDEO_ID, "channel_id": CHANNEL_ID, "availability": "private"}, "private"),
            ({"id": VIDEO_ID, "channel_id": CHANNEL_ID, "availability": "needs_auth"}, "restricted"),
            ({"id": VIDEO_ID, "channel_id": CHANNEL_ID, "availability": "premium_only"}, "restricted"),
            ({"id": VIDEO_ID, "channel_id": CHANNEL_ID, "availability": "subscriber_only"}, "restricted"),
            ({"id": VIDEO_ID, "channel_id": CHANNEL_ID, "live_status": "is_upcoming"}, "live_processing"),
            ({"id": VIDEO_ID, "channel_id": CHANNEL_ID, "live_status": "post_live"}, "live_processing"),
        ]
        for payload, expected in cases:
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(payload), stderr=""
            )
            with self.subTest(payload=payload), patch.object(
                availability.subprocess, "run", return_value=completed
            ):
                result = availability._yt_dlp_probe(
                    "video", VIDEO_ID, expected_channel_id=CHANNEL_ID
                )
            self.assertEqual(result["state"], expected)

        mismatched_video = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"id": "AAAAAAAAAAA", "channel_id": CHANNEL_ID}), stderr="",
        )
        with patch.object(availability.subprocess, "run", return_value=mismatched_video):
            result = availability._yt_dlp_probe(
                "video", VIDEO_ID, expected_channel_id=CHANNEL_ID
            )
        self.assertEqual(result["reason_code"], "probe_identity_mismatch")

        wrong_channel = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"id": VIDEO_ID, "channel_id": OTHER_CHANNEL_ID}), stderr="",
        )
        with patch.object(availability.subprocess, "run", return_value=wrong_channel):
            result = availability._yt_dlp_probe(
                "video", VIDEO_ID, expected_channel_id=CHANNEL_ID
            )
        self.assertEqual(result["reason_code"], "probe_channel_identity_mismatch")

    def test_structured_channel_response_rejects_different_canonical_id(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"id": OTHER_CHANNEL_ID, "channel_id": OTHER_CHANNEL_ID}),
            stderr="",
        )
        with patch.object(availability.subprocess, "run", return_value=completed):
            result = availability._yt_dlp_probe("channel", CHANNEL_ID)
        self.assertEqual(result["state"], "transient_failure")
        self.assertEqual(result["reason_code"], "probe_identity_mismatch")

    def test_structured_channel_response_requires_a_canonical_returned_id(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"title": "Channel without an exact identity"}),
            stderr="",
        )
        with patch.object(availability.subprocess, "run", return_value=completed):
            result = availability._yt_dlp_probe("channel", CHANNEL_ID)

        self.assertEqual(result["state"], "transient_failure")
        self.assertEqual(result["reason_code"], "probe_identity_unverified")

    def test_data_api_requires_exact_id_and_403_disables_corroboration(self) -> None:
        mismatch = Mock(status_code=200)
        mismatch.json.return_value = {"items": [{"id": "AAAAAAAAAAA"}]}
        with (
            patch.object(availability, "YOUTUBE_API_KEY", "secret"),
            patch.object(availability.requests, "get", return_value=mismatch),
        ):
            result = availability._api_probe("video", VIDEO_ID)
        self.assertEqual(result["reason_code"], "api_identity_mismatch")

        for status_code in (400, 401, 403):
            forbidden = Mock(status_code=status_code)
            forbidden.json.return_value = {
                "error": {"errors": [{"reason": "quotaExceeded"}]}
            }
            with (
                self.subTest(status_code=status_code),
                patch.object(availability, "YOUTUBE_API_KEY", "secret"),
                patch.object(availability.requests, "get", return_value=forbidden),
            ):
                result = availability._api_probe("video", VIDEO_ID)
            self.assertTrue(result["disable_api"])
            self.assertNotIn("secret", result["raw_reason"])

    def test_api_existence_does_not_turn_unknown_channel_into_accessible(self) -> None:
        ytdlp = {
            "state": "unavailable_unknown", "reason_code": "channel_unavailable_unknown",
            "raw_reason": "Channel unavailable", "evidence": {},
        }
        api = {
            "state": "accessible", "reason_code": "api_resource_returned",
            "raw_reason": "API returned it", "evidence": {"item_count": 1},
        }
        with patch.object(availability, "_tool_version", return_value="test"):
            result = availability._combine_probe("channel", ytdlp, api)
        self.assertEqual(result["state"], "unavailable_unknown")
        self.assertTrue(result["evidence"]["api_resource_exists"])

    def test_video_job_uses_server_derived_inventory_and_persists_result(self) -> None:
        channel_root = self.source / CHANNEL_ID
        channel_root.mkdir()
        media = channel_root / f"{VIDEO_ID}.mp4"
        media.write_bytes(b"media")
        result = {
            "state": "private", "reason_code": "video_private",
            "raw_reason": "Private video", "provider": "yt-dlp", "evidence": {},
        }
        with patch.object(availability, "probe", return_value=result):
            job = availability.start_video_check(CHANNEL_ID, background=False)
        self.assertEqual(job["status"], "completed")
        listing = availability.list_videos(CHANNEL_ID)
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["videos"][0]["state"], "private")
        self.assertEqual(listing["videos"][0]["preview_path"], str(media))

    def test_repeated_video_listing_coalesces_source_tree_walks(self) -> None:
        channel_root = self.source / CHANNEL_ID
        channel_root.mkdir()
        (channel_root / f"{VIDEO_ID}.mp4").write_bytes(b"media")

        with patch.object(
            availability,
            "_scan_source_inventory",
            wraps=availability._scan_source_inventory,
        ) as scan:
            first = availability.list_videos(CHANNEL_ID)
            second = availability.list_videos(CHANNEL_ID, query=VIDEO_ID)

        self.assertEqual(first["total"], 1)
        self.assertEqual(second["total"], 1)
        self.assertEqual(scan.call_count, 1)

    def test_repeated_video_listing_caches_database_path_validation_too(self) -> None:
        channel_root = self.source / CHANNEL_ID
        channel_root.mkdir()
        media = channel_root / f"{VIDEO_ID}.mp4"
        media.write_bytes(b"media")
        organized = self.target / "Channel" / "video.mp4"
        organized.parent.mkdir()
        organized.symlink_to(media)
        with db.get_db() as conn:
            conn.execute(
                "INSERT INTO videos (video_id, channel_id, symlink, status) "
                "VALUES (?, ?, ?, 'linked')",
                (VIDEO_ID, CHANNEL_ID, str(organized)),
            )
            conn.commit()

        with patch.object(
            availability,
            "_safe_preview_path",
            wraps=availability._safe_preview_path,
        ) as validate:
            availability.list_videos(CHANNEL_ID)
            first_count = validate.call_count
            availability.list_videos(CHANNEL_ID, state="unknown", query=VIDEO_ID)

        self.assertGreater(first_count, 0)
        self.assertEqual(validate.call_count, first_count)

    def test_source_inventory_walks_different_channels_concurrently(self) -> None:
        barrier = threading.Barrier(2, timeout=2)
        failures: list[Exception] = []

        def scan(channel_id: str):
            try:
                barrier.wait()
            except Exception as exc:  # surfaced after both threads join
                failures.append(exc)
            return {}

        def load(channel_id: str) -> None:
            try:
                availability._source_inventory(channel_id, force=True)
            except Exception as exc:
                failures.append(exc)

        with patch.object(availability, "_scan_source_inventory", side_effect=scan):
            threads = [
                threading.Thread(target=load, args=(channel_id,))
                for channel_id in (CHANNEL_ID, OTHER_CHANNEL_ID)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])

    def test_starting_video_job_bypasses_stale_inventory_snapshot(self) -> None:
        channel_root = self.source / CHANNEL_ID
        channel_root.mkdir()
        (channel_root / f"{VIDEO_ID}.mp4").write_bytes(b"first")
        availability.list_videos(CHANNEL_ID)
        (channel_root / "AAAAAAAAAAA.mp4").write_bytes(b"second")
        result = {
            "state": "available",
            "reason_code": "public_response",
            "raw_reason": "Available",
            "provider": "test",
            "evidence": {},
        }

        with patch.object(availability, "probe", return_value=result):
            job = availability.start_video_check(CHANNEL_ID, background=False)

        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["total"], 2)

    def test_background_job_is_persisted_before_inventory_discovery(self) -> None:
        main_thread = threading.current_thread()
        discovered = threading.Event()
        result = {
            "state": "available",
            "reason_code": "public_response",
            "raw_reason": "Available",
            "provider": "test",
            "evidence": {},
        }

        def inventory(*_args, **_kwargs):
            self.assertIsNot(threading.current_thread(), main_thread)
            discovered.set()
            return {VIDEO_ID: {"video_id": VIDEO_ID}}

        with (
            patch.object(availability, "_local_inventory", side_effect=inventory),
            patch.object(availability, "probe", return_value=result),
        ):
            job = availability.start_video_check(CHANNEL_ID, background=True)
            self.assertTrue(job["job_id"])
            self.assertTrue(discovered.wait(timeout=2))
            with availability._runtime_lock:
                worker = availability._threads.get(job["job_id"])
            if worker is not None:
                worker.join(timeout=3)

        self.assertEqual(availability.get_job(job["job_id"])["status"], "completed")

    def test_zero_byte_exact_id_is_inventory_even_when_it_cannot_be_previewed(self) -> None:
        channel_root = self.source / CHANNEL_ID
        channel_root.mkdir()
        (channel_root / f"{VIDEO_ID}.mp4").touch()

        listing = availability.list_videos(CHANNEL_ID)

        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["videos"][0]["video_id"], VIDEO_ID)
        self.assertIsNone(listing["videos"][0]["preview_path"])

    def test_exact_id_non_video_filename_is_not_source_inventory(self) -> None:
        channel_root = self.source / CHANNEL_ID
        channel_root.mkdir()
        (channel_root / f"{VIDEO_ID}.txt").write_text("not media")

        listing = availability.list_videos(CHANNEL_ID)

        self.assertEqual(listing["total"], 0)

    def test_incomplete_source_walk_fails_instead_of_checking_partial_inventory(self) -> None:
        (self.source / CHANNEL_ID).mkdir()

        def broken_walk(_root, *, followlinks, onerror):
            self.assertFalse(followlinks)
            onerror(PermissionError("archive mount denied traversal"))
            yield from ()

        with (
            patch.object(availability.os, "walk", side_effect=broken_walk),
            self.assertRaisesRegex(
                availability.AvailabilityError, "complete source inventory"
            ),
        ):
            availability._local_inventory(CHANNEL_ID, force_source_scan=True)

    def test_broken_or_wrong_link_falls_back_to_exact_id_source_preview(self) -> None:
        channel_root = self.source / CHANNEL_ID
        channel_root.mkdir()
        exact_source = channel_root / f"{VIDEO_ID}.mp4"
        exact_source.write_bytes(b"exact media")
        other_source = channel_root / "AAAAAAAAAAA.mp4"
        other_source.write_bytes(b"other media")
        organized = self.target / "Channel" / "video.mp4"
        organized.parent.mkdir()

        for target in (channel_root / "missing.mp4", other_source):
            with self.subTest(target=target):
                if organized.is_symlink():
                    organized.unlink()
                organized.symlink_to(target)
                availability._clear_source_inventory_cache_for_tests()
                with db.get_db() as conn:
                    conn.execute(
                        """
                        INSERT INTO videos (video_id, channel_id, symlink, status)
                        VALUES (?, ?, ?, 'linked')
                        ON CONFLICT(video_id) DO UPDATE SET symlink = excluded.symlink
                        """,
                        (VIDEO_ID, CHANNEL_ID, str(organized)),
                    )
                    conn.commit()

                listing = availability.list_videos(CHANNEL_ID)

                item = next(
                    row for row in listing["videos"]
                    if row["video_id"] == VIDEO_ID
                )
                self.assertEqual(item["preview_path"], str(exact_source))

    def test_valid_exact_id_symlink_remains_the_preferred_preview_entry(self) -> None:
        channel_root = self.source / CHANNEL_ID
        channel_root.mkdir()
        exact_source = channel_root / f"{VIDEO_ID}.mp4"
        exact_source.write_bytes(b"exact media")
        organized = self.target / "Channel" / "video.mp4"
        organized.parent.mkdir()
        organized.symlink_to(exact_source)
        with db.get_db() as conn:
            conn.execute(
                "INSERT INTO videos (video_id, channel_id, symlink, status) VALUES (?, ?, ?, 'linked')",
                (VIDEO_ID, CHANNEL_ID, str(organized)),
            )
            conn.commit()

        listing = availability.list_videos(CHANNEL_ID)

        self.assertEqual(listing["videos"][0]["preview_path"], str(organized))

    def test_linked_row_does_not_preview_idless_real_file_at_expected_link_path(self) -> None:
        occupied = self.target / "Channel" / "video.mp4"
        occupied.parent.mkdir()
        occupied.write_bytes(b"unrelated real media")
        with db.get_db() as conn:
            conn.execute(
                """
                INSERT INTO videos (video_id, channel_id, symlink, status)
                VALUES (?, ?, ?, 'linked')
                """,
                (VIDEO_ID, CHANNEL_ID, str(occupied)),
            )
            conn.commit()

        listing = availability.list_videos(CHANNEL_ID)

        self.assertEqual(listing["total"], 1)
        self.assertIsNone(listing["videos"][0]["preview_path"])

    def test_explicitly_adopted_idless_real_file_can_be_previewed(self) -> None:
        adopted = self.target / "Channel" / "video.mp4"
        adopted.parent.mkdir()
        adopted.write_bytes(b"adopted real media")
        with db.get_db() as conn:
            conn.execute(
                """
                INSERT INTO videos
                    (video_id, channel_id, symlink, status, adopted_real)
                VALUES (?, ?, ?, 'adopted', 1)
                """,
                (VIDEO_ID, CHANNEL_ID, str(adopted)),
            )
            conn.commit()

        listing = availability.list_videos(CHANNEL_ID)

        self.assertEqual(listing["videos"][0]["preview_path"], str(adopted))

    def test_preview_rejects_a_second_symlink_even_when_intermediate_name_matches(self) -> None:
        channel_root = self.source / CHANNEL_ID
        channel_root.mkdir()
        terminal = channel_root / "AAAAAAAAAAA.mp4"
        terminal.write_bytes(b"different video")
        intermediate = channel_root / f"{VIDEO_ID}.mp4"
        intermediate.symlink_to(terminal)
        organized = self.target / "Channel" / "video.mp4"
        organized.parent.mkdir()
        organized.symlink_to(intermediate)

        self.assertIsNone(
            availability._safe_preview_path(
                str(organized), expected_video_id=VIDEO_ID
            )
        )

    def test_check_all_rejects_an_unbounded_video_job(self) -> None:
        oversized = {
            f"{index:011d}": {"video_id": f"{index:011d}"}
            for index in range(availability._MAX_VIDEO_JOB_SUBJECTS + 1)
        }
        with patch.object(
            availability, "_local_inventory", return_value=oversized
        ):
            job = availability.start_video_check(CHANNEL_ID, background=False)

        self.assertEqual(job["status"], "failed")
        self.assertIn("single availability job", job["error"])

    def test_video_check_rejects_non_inventory_id(self) -> None:
        channel_root = self.source / CHANNEL_ID
        channel_root.mkdir()
        (channel_root / f"{VIDEO_ID}.mp4").write_bytes(b"media")
        job = availability.start_video_check(
            CHANNEL_ID, ["AAAAAAAAAAA"], background=False
        )

        self.assertEqual(job["status"], "failed")
        self.assertIn("local channel inventory", job["error"])

    def test_video_check_rejects_unhashable_non_id_before_job_creation(self) -> None:
        with self.assertRaisesRegex(
            availability.AvailabilityError, "exact 11-character"
        ):
            availability.start_video_check(CHANNEL_ID, [{"id": VIDEO_ID}])

    def test_video_availability_is_owned_by_channel_and_cannot_be_stolen(self) -> None:
        for channel_id in (CHANNEL_ID, OTHER_CHANNEL_ID):
            with db.get_db() as conn:
                availability.upsert_channel_identity(conn, channel_id)
                conn.commit()
        private = {
            "state": "private", "reason_code": "video_private",
            "raw_reason": "Private", "evidence": {},
        }
        available = {
            "state": "available", "reason_code": "public_response",
            "raw_reason": "Available", "evidence": {},
        }
        availability.record_observation("one", "video", VIDEO_ID, CHANNEL_ID, private)
        availability.record_observation("two", "video", VIDEO_ID, OTHER_CHANNEL_ID, available)
        with db.get_db() as conn:
            rows = conn.execute(
                "SELECT channel_id, current_state FROM video_availability "
                "WHERE video_id = ? ORDER BY channel_id",
                (VIDEO_ID,),
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["current_state"] for row in rows}, {"private", "available"})

    def test_recovered_display_name_is_not_overwritten_by_normal_metadata(self) -> None:
        with db.get_db() as conn:
            availability.upsert_channel_identity(
                conn, CHANNEL_ID, display_name="Recovered name", provenance="recovered"
            )
            availability.upsert_channel_identity(
                conn, CHANNEL_ID, display_name="TubeArchivist name", provenance="normal"
            )
            conn.commit()
            row = conn.execute(
                "SELECT display_name, provenance FROM channel_identities WHERE channel_id = ?",
                (CHANNEL_ID,),
            ).fetchone()
        self.assertEqual(row["display_name"], "Recovered name")
        self.assertEqual(row["provenance"], "recovered")

    def test_channel_rows_include_actual_observation_count(self) -> None:
        self._identity()
        result = {
            "state": "accessible", "reason_code": "public_response",
            "raw_reason": "Available", "evidence": {},
        }
        availability.record_observation("one", "channel", CHANNEL_ID, CHANNEL_ID, result)
        availability.record_observation("two", "channel", CHANNEL_ID, CHANNEL_ID, result)
        row = next(item for item in availability.list_channels() if item["channel_id"] == CHANNEL_ID)
        self.assertEqual(row["observation_count"], 2)

    def test_query_and_state_filter_apply_before_pagination(self) -> None:
        self._identity()
        rows = []
        for index in range(105):
            video_id = f"A{index:010d}"
            title = "Needle beyond first page" if index == 104 else f"Video {index}"
            rows.append((video_id, title, CHANNEL_ID))
        with db.get_db() as conn:
            conn.executemany(
                "INSERT INTO videos (video_id, title, channel_id, status) VALUES (?, ?, ?, 'linked')",
                rows,
            )
            conn.commit()
        result = availability.list_videos(
            CHANNEL_ID, page=1, per_page=10, query="needle beyond"
        )
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["pages"], 1)
        self.assertEqual(result["videos"][0]["video_id"], "A0000000104")

        private = {
            "state": "private", "reason_code": "video_private",
            "raw_reason": "Private", "evidence": {},
        }
        availability.record_observation(
            "private", "video", "A0000000104", CHANNEL_ID, private
        )
        result = availability.list_videos(
            CHANNEL_ID, page=1, per_page=10, state="private"
        )
        self.assertEqual(result["total"], 1)

    def test_cancellation_during_final_probe_wins_over_completion(self) -> None:
        channel_root = self.source / CHANNEL_ID
        channel_root.mkdir()
        (channel_root / f"{VIDEO_ID}.mp4").write_bytes(b"media")

        def cancel_during_probe(*_args, **_kwargs):
            with db.get_db() as conn:
                conn.execute(
                    "UPDATE availability_jobs SET cancel_requested = 1, "
                    "status = 'cancelling' WHERE status = 'running'"
                )
                conn.commit()
            return {
                "state": "available", "reason_code": "public_response",
                "raw_reason": "Available", "provider": "yt-dlp", "evidence": {},
            }

        with patch.object(availability, "probe", side_effect=cancel_during_probe):
            job = availability.start_video_check(CHANNEL_ID, background=False)
        self.assertEqual(job["status"], "cancelled")

    def test_cancellation_during_inventory_wins_over_inventory_failure(self) -> None:
        def cancel_then_return(*_args, **_kwargs):
            with db.get_db() as conn:
                conn.execute(
                    """
                    UPDATE availability_jobs
                    SET cancel_requested = 1, status = 'cancelling',
                        phase = 'cancelling'
                    WHERE status = 'running'
                    """
                )
                conn.commit()
            return {}

        with patch.object(
            availability, "_local_inventory", side_effect=cancel_then_return
        ):
            job = availability.start_video_check(CHANNEL_ID, background=False)

        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["phase"], "cancelled")

    def test_rejected_api_credentials_disable_api_for_remaining_job_items(self) -> None:
        channel_root = self.source / CHANNEL_ID
        channel_root.mkdir()
        second_id = "AAAAAAAAAAA"
        (channel_root / f"{VIDEO_ID}.mp4").write_bytes(b"media")
        (channel_root / f"{second_id}.mp4").write_bytes(b"media")
        api_flags: list[bool] = []

        def probe_item(*_args, **kwargs):
            api_flags.append(bool(kwargs["include_api"]))
            return {
                "state": "available", "reason_code": "public_response",
                "raw_reason": "Available", "provider": "yt-dlp", "evidence": {},
                "disable_api": len(api_flags) == 1,
            }

        with (
            patch.object(availability, "YOUTUBE_API_KEY", "configured"),
            patch.object(availability, "probe", side_effect=probe_item),
        ):
            job = availability.start_video_check(CHANNEL_ID, background=False)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(api_flags, [True, False])


if __name__ == "__main__":
    unittest.main()
