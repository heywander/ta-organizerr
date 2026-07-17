from __future__ import annotations

import subprocess
import json
import threading
import tempfile
import unittest
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

    def test_generic_unavailable_is_never_termination(self) -> None:
        result = availability._classify_failure("channel", "ERROR: This channel is not available")
        self.assertEqual(result["state"], "unavailable_unknown")
        self.assertNotEqual(result["reason_code"], "channel_terminated")

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
