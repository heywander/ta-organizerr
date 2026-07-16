from __future__ import annotations

import hashlib
import io
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from ta_organizerr import media_reservations, salvage
from ta_organizerr.routes import salvage as salvage_routes


class SalvageBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        media_reservations._reset_reservations_for_tests()
        salvage.reset_state_for_tests()

    def tearDown(self) -> None:
        salvage.reset_state_for_tests()
        media_reservations._reset_reservations_for_tests()

    def _roots(self, root: Path):
        return (
            patch.object(salvage, "MEDIA_ROOTS", (root,)),
            patch.object(salvage, "_CANDIDATE_ROOTS", (root,)),
            patch.object(salvage, "SALVAGE_DIR", root / "salvage"),
        )

    def test_expected_duration_is_positive_finite_and_bounded(self) -> None:
        for value in (False, 0, -1, float("inf"), "nope"):
            parsed, error = salvage.parse_expected_duration(value)
            self.assertIsNone(parsed)
            self.assertIsNotNone(error)
        parsed, error = salvage.parse_expected_duration("600.5")
        self.assertEqual(parsed, 600.5)
        self.assertIsNone(error)

    def test_expected_source_checksum_requires_sha256_hex(self) -> None:
        for value in (None, "short", "z" * 64):
            parsed, error = salvage.parse_expected_sha256(value)
            self.assertIsNone(parsed)
            self.assertIsNotNone(error)
        parsed, error = salvage.parse_expected_sha256("A" * 64)
        self.assertEqual(parsed, "a" * 64)
        self.assertIsNone(error)

    def test_progress_reader_never_blocks_when_bounded_queue_is_full(self) -> None:
        progress: queue.Queue[str] = queue.Queue(maxsize=1)
        progress.put("old=1")
        reader = threading.Thread(
            target=salvage._read_pipe,
            args=(io.StringIO("out_time_us=1000000\nprogress=end\n"), progress, None),
        )
        reader.start()
        reader.join(timeout=1)
        self.assertFalse(reader.is_alive())

    def test_open_media_rejects_outside_allowed_roots(self) -> None:
        with tempfile.TemporaryDirectory() as allowed, tempfile.TemporaryDirectory() as outside:
            source = Path(outside) / "abcdefghijk.mp4"
            source.write_bytes(b"media")
            with patch.object(salvage, "MEDIA_ROOTS", (Path(allowed),)):
                with self.assertRaisesRegex(salvage.SalvageError, "outside"):
                    salvage._open_media(str(source))

    def test_open_media_holds_a_regular_read_only_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "abcdefghijk.mp4"
            source.write_bytes(b"media")
            with patch.object(salvage, "MEDIA_ROOTS", (root,)):
                opened = salvage._open_media(str(source))
            try:
                self.assertEqual(os.pread(opened.fd, 5, 0), b"media")
                self.assertEqual(opened.resolved_path, source.resolve())
            finally:
                opened.close()

    def test_video_id_evidence_accepts_either_side_and_rejects_conflicts(self) -> None:
        self.assertEqual(
            salvage._video_id_from_path_evidence(
                "/organized/abcdefghijk.mp4", "/source/opaque.mp4"
            ),
            "abcdefghijk",
        )
        self.assertEqual(
            salvage._video_id_from_path_evidence(
                "/organized/video.mp4", "/source/abcdefghijk.mp4"
            ),
            "abcdefghijk",
        )
        self.assertEqual(
            salvage._video_id_from_path_evidence(
                "/organized/abcdefghijk.mp4", "/source/abcdefghijk.mp4"
            ),
            "abcdefghijk",
        )
        self.assertIsNone(
            salvage._video_id_from_path_evidence(
                "/organized/video.mp4", "/source/opaque.mp4"
            )
        )
        with self.assertRaisesRegex(salvage.SalvageError, "Conflicting"):
            salvage._video_id_from_path_evidence(
                "/organized/abcdefghijk.mp4", "/source/lmnopqrstuv.mp4"
            )

    def test_empty_and_unsupported_inputs_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            empty = root / "abcdefghijk.mp4"
            empty.touch()
            text = root / "abcdefghijk.txt"
            text.write_text("not media")
            with patch.object(salvage, "MEDIA_ROOTS", (root,)):
                with self.assertRaisesRegex(salvage.SalvageError, "empty"):
                    salvage._open_media(str(empty))
                with self.assertRaisesRegex(salvage.SalvageError, "Unsupported"):
                    salvage._open_media(str(text))

    def test_candidate_pair_requires_exact_case_sensitive_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "abcdefghijk.mp4"
            wrong = root / "ABCDEFGHIJK.mp4"
            first.write_bytes(b"one")
            wrong.write_bytes(b"two")
            with (
                patch.object(salvage, "MEDIA_ROOTS", (root,)),
                patch.object(salvage, "_audit"),
            ):
                job, error, status = salvage.start_job(
                    str(first), "diagnose", candidate_path=str(wrong)
                )
            self.assertIsNone(job)
            self.assertEqual(status, 400)
            self.assertIn("exact same", error)

    def test_conflicting_requested_and_resolved_ids_are_rejected_before_job_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "lmnopqrstuv.mp4"
            conflicting = root / "abcdefghijk.mp4"
            target.write_bytes(b"media")
            conflicting.symlink_to(target)
            with (
                patch.object(salvage, "MEDIA_ROOTS", (root,)),
                patch.object(salvage, "_audit"),
                patch.object(threading.Thread, "start") as thread_start,
            ):
                job, error, status = salvage.start_job(
                    str(conflicting), "diagnose"
                )
            self.assertIsNone(job)
            self.assertEqual(status, 400)
            self.assertIn("Conflicting exact video IDs", error)
            thread_start.assert_not_called()

    def test_candidate_pair_rejects_same_inode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "abcdefghijk.mp4"
            alias = root / "copy [abcdefghijk].mp4"
            first.write_bytes(b"one")
            os.link(first, alias)
            with (
                patch.object(salvage, "MEDIA_ROOTS", (root,)),
                patch.object(salvage, "_audit"),
            ):
                job, error, status = salvage.start_job(
                    str(first), "diagnose", candidate_path=str(alias)
                )
            self.assertIsNone(job)
            self.assertEqual(status, 400)
            self.assertIn("same file", error)

    def test_worker_start_failure_releases_reservation_and_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "abcdefghijk.mp4"
            source.write_bytes(b"media")
            with (
                patch.object(salvage, "MEDIA_ROOTS", (root,)),
                patch.object(salvage, "_audit"),
                patch.object(threading.Thread, "start", side_effect=RuntimeError("no thread")),
            ):
                job, error, status = salvage.start_job(str(source), "diagnose")
            self.assertIsNone(job)
            self.assertEqual(status, 500)
            self.assertIn("no thread", error)
            self.assertFalse(salvage._active_identities)
            self.assertFalse(salvage._runtimes)

    def test_duplicate_active_input_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "abcdefghijk.mp4"
            source.write_bytes(b"media")
            with (
                patch.object(salvage, "MEDIA_ROOTS", (root,)),
                patch.object(salvage, "_audit"),
                patch.object(threading.Thread, "start"),
            ):
                first, _, first_status = salvage.start_job(str(source), "diagnose")
                second, error, second_status = salvage.start_job(str(source), "diagnose")
            self.assertIsNotNone(first)
            self.assertEqual(first_status, 202)
            self.assertIsNone(second)
            # With the default single worker, the capacity guard fires first.
            self.assertIn(second_status, {409, 429})
            self.assertTrue(error)

    def test_active_job_reserves_inode_and_requested_symlink_until_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "abcdefghijk.mp4"
            link = root / "video.mp4"
            source.write_bytes(b"media")
            link.symlink_to(source)
            with (
                patch.object(salvage, "MEDIA_ROOTS", (root,)),
                patch.object(salvage, "_audit"),
                patch.object(threading.Thread, "start"),
            ):
                job, error, status = salvage.start_job(str(link), "diagnose")

            self.assertIsNone(error)
            self.assertEqual(status, 202)
            with self.assertRaises(media_reservations.MediaReservationConflict):
                with media_reservations.media_mutation_guard(
                    paths=(link,),
                    action="remove this organized link",
                ):
                    link.unlink()
            with self.assertRaises(media_reservations.MediaReservationConflict):
                with media_reservations.media_mutation_guard(
                    paths=(source,),
                    action="delete this source",
                ):
                    source.unlink()

            salvage._release_runtime(job["id"])
            with media_reservations.media_mutation_guard(
                paths=(link,), action="remove this organized link"
            ):
                link.unlink()
            self.assertFalse(os.path.lexists(link))
            self.assertTrue(source.exists())

    def test_path_swap_during_open_to_reserve_gap_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "abcdefghijk.mp4"
            replacement = root / "replacement.mp4"
            source.write_bytes(b"original")
            replacement.write_bytes(b"replacement")
            real_assert = salvage._assert_source_unchanged

            def swap_after_descriptor_check(media):
                real_assert(media)
                os.replace(replacement, source)

            with (
                patch.object(salvage, "MEDIA_ROOTS", (root,)),
                patch.object(salvage, "_audit"),
                patch.object(
                    salvage,
                    "_assert_source_unchanged",
                    side_effect=swap_after_descriptor_check,
                ),
            ):
                job, error, status = salvage.start_job(str(source), "diagnose")

            self.assertIsNone(job)
            self.assertEqual(status, 409)
            self.assertIn("replaced", error)
            reservation = media_reservations.reserve_media(
                (media_reservations.media_identity(source.stat()),),
                paths=(source,),
                owner="subsequent test",
            )
            media_reservations.release_media(reservation)

    def test_diagnosis_completion_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "abcdefghijk.mp4"
            source.write_bytes(b"original")
            result = {
                "assessment": "healthy",
                "warnings": [],
                "source": {"sha256": "abc"},
            }
            with (
                patch.object(salvage, "MEDIA_ROOTS", (root,)),
                patch.object(salvage, "_audit"),
                patch.object(threading.Thread, "start"),
                patch.object(salvage, "_diagnosis_result", return_value=result),
            ):
                job, _, _ = salvage.start_job(str(source), "diagnose")
                salvage._run_job(job["id"])
            self.assertEqual(source.read_bytes(), b"original")
            completed = salvage.get_job(job["id"])
            self.assertEqual(completed["status"], "completed")
            self.assertIsNone(completed["output_path"])

    def test_cancel_sets_event_and_job_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "abcdefghijk.mp4"
            source.write_bytes(b"media")
            with (
                patch.object(salvage, "MEDIA_ROOTS", (root,)),
                patch.object(salvage, "_audit"),
                patch.object(threading.Thread, "start"),
            ):
                job, _, _ = salvage.start_job(str(source), "diagnose")
                cancelled, error, status = salvage.cancel_job(job["id"])
            self.assertEqual(status, 202)
            self.assertIsNone(error)
            self.assertTrue(cancelled["cancel_requested"])
            self.assertTrue(salvage._runtimes[job["id"]].cancel.is_set())

    def test_output_path_is_private_separate_and_non_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "abcdefghijk.mp4"
            source.write_bytes(b"original")
            with (
                patch.object(salvage, "MEDIA_ROOTS", (root,)),
                patch.object(salvage, "SALVAGE_DIR", root / "salvage"),
            ):
                opened = salvage._open_media(str(source))
                try:
                    fd, dir_fd, partial, final, disk_cap = salvage._safe_output_paths(
                        "0123456789abcdef", opened, "remux"
                    )
                    os.close(fd)
                    os.close(dir_fd)
                finally:
                    opened.close()
            try:
                self.assertNotEqual(final, source)
                self.assertTrue(partial.exists())
                self.assertFalse(final.exists())
                self.assertGreater(disk_cap, 0)
                self.assertTrue(str(final).startswith(str(root / "salvage")))
            finally:
                partial.unlink(missing_ok=True)

    def test_output_name_uses_unambiguous_requested_id_for_opaque_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "opaque.mp4"
            link = root / "abcdefghijk.mp4"
            target.write_bytes(b"media")
            link.symlink_to(target)
            with (
                patch.object(salvage, "MEDIA_ROOTS", (root,)),
                patch.object(salvage, "SALVAGE_DIR", root / "salvage"),
            ):
                opened = salvage._open_media(str(link))
                try:
                    fd, dir_fd, partial, final, _disk_cap = salvage._safe_output_paths(
                        "0123456789abcdef", opened, "remux"
                    )
                    os.close(fd)
                    os.close(dir_fd)
                finally:
                    opened.close()
            try:
                self.assertEqual(final.parent.name, "abcdefghijk")
                self.assertEqual(
                    salvage.extract_id_from_filename(final.name), "abcdefghijk"
                )
            finally:
                partial.unlink(missing_ok=True)

    def test_remux_and_reencode_commands_pin_demuxer_and_fd_paths(self) -> None:
        media = salvage.OpenedMedia(
            "source.mp4", Path("/allowed/abcdefghijk.mp4"), 7,
            (1, 2), 10, 1, 1, "mov", ".mp4",
        )
        probe = {"streams": [
            {"index": 0, "type": "video", "attached_pic": True},
            {"index": 2, "type": "video", "attached_pic": False},
            {"index": 4, "type": "audio", "attached_pic": False},
        ]}
        remux = salvage._build_salvage_command(
            media, 9, "remux", probe, 123456
        )
        encode = salvage._build_salvage_command(media, 9, "reencode", probe)
        self.assertIn("file,pipe", remux)
        self.assertIn("mov", remux)
        self.assertIn("/proc/self/fd/7", remux)
        self.assertIn("/proc/self/fd/9", remux)
        self.assertEqual(remux[remux.index("-c") + 1], "copy")
        self.assertIn("libx264", encode)
        self.assertIn("0:2", remux)
        self.assertIn("0:4", remux)
        self.assertNotIn("0:0", remux)
        self.assertEqual(remux[remux.index("-fs") + 1], "123456")
        self.assertNotIn("shell", " ".join(encode))

    def test_actual_salvage_path_publishes_new_inode_name_and_preserves_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "abcdefghijk.mp4"
            source.write_bytes(b"original-media")
            probe = {
                "format": "mov",
                "declared_duration_seconds": 10.0,
                "streams": [{
                    "index": 0, "type": "video", "codec": "h264",
                    "duration_seconds": 10.0, "attached_pic": False,
                }],
            }
            output_report = {
                "sha256": hashlib.sha256(b"salvaged-media").hexdigest(),
                "declared_duration_seconds": 10.0,
                "decode": {"streams": [{"clean": True}]},
                "warnings": [],
            }

            def fake_process(_job_id, _command, _pass_fds, **kwargs):
                output_fd = kwargs["output_fd"]
                with self.assertRaises(media_reservations.MediaReservationConflict):
                    media_reservations.reserve_media(
                        (media_reservations.media_identity(os.fstat(output_fd)),),
                        owner="second media job",
                    )
                os.ftruncate(output_fd, 0)
                os.pwrite(output_fd, b"salvaged-media", 0)
                return 0, 10.0, []

            with (
                patch.object(salvage, "MEDIA_ROOTS", (root, root / "salvage")),
                patch.object(salvage, "SALVAGE_DIR", root / "salvage"),
                patch.object(salvage, "_audit"),
                patch.object(threading.Thread, "start"),
                patch.object(salvage, "_probe_media", return_value=probe),
                patch.object(salvage, "_run_process", side_effect=fake_process),
                patch.object(salvage, "_media_report", return_value=output_report),
            ):
                source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
                job, _, _ = salvage.start_job(
                    str(source), "remux", expected_source_sha256=source_hash
                )
                runtime = salvage._runtimes[job["id"]]
                result = salvage._salvage_result(
                    job["id"], runtime, "remux", 10.0, source_hash
                )

            output = Path(result["output"]["path"])
            self.assertEqual(source.read_bytes(), b"original-media")
            self.assertEqual(output.read_bytes(), b"salvaged-media")
            self.assertNotEqual(output, source)
            self.assertEqual(salvage.extract_id_from_filename(output.name), "abcdefghijk")
            self.assertFalse(any(output.parent.glob("*.partial.mkv")))

    def test_source_changed_during_final_output_hash_is_not_published(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "abcdefghijk.mp4"
            source.write_bytes(b"original-media")
            probe = {
                "format": "mov",
                "declared_duration_seconds": 10.0,
                "streams": [{
                    "index": 0, "type": "video", "codec": "h264",
                    "duration_seconds": 10.0, "attached_pic": False,
                }],
            }
            output_report = {
                "sha256": hashlib.sha256(b"salvaged-media").hexdigest(),
                "declared_duration_seconds": 10.0,
                "decode": {"streams": [{"clean": True}]},
                "warnings": [],
            }

            def fake_process(_job_id, _command, _pass_fds, **kwargs):
                output_fd = kwargs["output_fd"]
                os.ftruncate(output_fd, 0)
                os.pwrite(output_fd, b"salvaged-media", 0)
                return 0, 10.0, []

            real_hash_media = salvage._hash_media

            def hash_then_change_source(job_id, media, start, end, phase):
                digest = real_hash_media(job_id, media, start, end, phase)
                if phase == "verifying_output_unchanged":
                    source.write_bytes(b"changed-after-output-hash")
                return digest

            with (
                patch.object(salvage, "MEDIA_ROOTS", (root, root / "salvage")),
                patch.object(salvage, "SALVAGE_DIR", root / "salvage"),
                patch.object(salvage, "_audit"),
                patch.object(threading.Thread, "start"),
                patch.object(salvage, "_probe_media", return_value=probe),
                patch.object(salvage, "_run_process", side_effect=fake_process),
                patch.object(salvage, "_media_report", return_value=output_report),
                patch.object(salvage, "_hash_media", side_effect=hash_then_change_source),
            ):
                source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
                job, _, _ = salvage.start_job(
                    str(source), "remux", expected_source_sha256=source_hash
                )
                runtime = salvage._runtimes[job["id"]]
                with self.assertRaisesRegex(salvage.SalvageError, "changed"):
                    salvage._salvage_result(
                        job["id"], runtime, "remux", 10.0, source_hash
                    )

            salvage_root = root / "salvage"
            self.assertFalse(list(salvage_root.rglob("*.mkv")))

    def test_material_shortfall_uses_tolerance(self) -> None:
        self.assertTrue(salvage._materially_short(240, 600))
        self.assertFalse(salvage._materially_short(594, 600))
        self.assertFalse(salvage._materially_short(None, 600))

    def test_audio_video_endpoint_divergence_warns_without_stream_metadata(self) -> None:
        warnings = salvage._endpoint_divergence_warnings({
            "streams": [
                {
                    "index": 0,
                    "type": "video",
                    "decoded_duration_seconds": 600.0,
                    "declared_duration_seconds": None,
                },
                {
                    "index": 1,
                    "type": "audio",
                    "decoded_duration_seconds": 240.0,
                    "declared_duration_seconds": None,
                },
            ],
        })
        self.assertEqual(len(warnings), 1)
        self.assertIn("possible intentional short track", warnings[0])
        self.assertFalse(salvage._endpoint_divergence_warnings({
            "streams": [
                {"index": 0, "type": "video", "decoded_duration_seconds": 600.0},
                {"index": 1, "type": "audio", "decoded_duration_seconds": 594.0},
            ],
        }))

    def test_multiple_primary_videos_are_diagnosed_but_block_salvage(self) -> None:
        media = salvage.OpenedMedia(
            "source.mp4", Path("/allowed/abcdefghijk.mp4"), 7,
            (1, 2), 10, 1, 1, "mov", ".mp4",
        )
        probe = {
            "format": "mov",
            "declared_duration_seconds": 10.0,
            "streams": [
                {
                    "index": 0, "type": "video", "attached_pic": False,
                    "duration_seconds": 10.0,
                },
                {
                    "index": 1, "type": "video", "attached_pic": False,
                    "duration_seconds": 10.0,
                },
            ],
        }
        decode = {
            "streams": [
                {
                    "index": 0, "type": "video", "clean": True,
                    "decoded_duration_seconds": 10.0,
                    "declared_duration_seconds": 10.0,
                },
                {
                    "index": 1, "type": "video", "clean": True,
                    "decoded_duration_seconds": 10.0,
                    "declared_duration_seconds": 10.0,
                },
            ],
            "readable_through_seconds": 10.0,
            "selection_warnings": [],
        }
        with (
            patch.object(salvage, "_hash_media", return_value="a" * 64),
            patch.object(salvage, "_probe_media", return_value=probe),
            patch.object(salvage, "_decode_report", return_value=decode),
            patch.object(salvage, "_assert_source_unchanged"),
        ):
            report = salvage._media_report(
                "job", media, expected=None,
                hash_start=0, hash_end=5, decode_start=5, decode_end=100,
                label="source",
            )
        self.assertFalse(report["salvage_supported"])
        self.assertIn("Multiple primary video streams", report["salvage_blockers"][0])

        with (
            patch.object(salvage, "_media_report", return_value=report),
            patch.object(salvage, "_hash_media", return_value=report["sha256"]),
            patch.object(salvage, "_assert_source_unchanged"),
            patch.object(salvage, "_assert_current_path_same"),
        ):
            diagnosis = salvage._diagnosis_result(
                "job", salvage.JobRuntime([media]), None
            )
        self.assertFalse(diagnosis["salvage_supported"])
        self.assertEqual(diagnosis["assessment"], "warning")
        self.assertEqual(diagnosis["salvage_blockers"], report["salvage_blockers"])

    def test_candidates_match_exact_id_not_case_or_resemblance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exact = root / "abcdefghijk.mp4"
            title = root / "Title [abcdefghijk].mkv"
            wrong_case = root / "ABCDEFGHIJK.mp4"
            resemblance = root / "prefix-abcdefghijk-suffix.mp4"
            for index, path in enumerate((exact, title, wrong_case, resemblance), 1):
                path.write_bytes(bytes([index]))
            fake_probe = {
                "size_bytes": 1, "duration_seconds": 1.0,
                "streams": [{"type": "video"}], "format": "mov",
            }
            with (
                patch.object(salvage, "MEDIA_ROOTS", (root,)),
                patch.object(salvage, "_CANDIDATE_ROOTS", (root,)),
                patch.object(salvage, "_quick_probe_path", return_value=(fake_probe, None)),
            ):
                matches = salvage.exact_id_candidates("abcdefghijk")
            self.assertEqual({Path(item["path"]).name for item in matches}, {
                exact.name, title.name,
            })
            self.assertTrue(all(item["exact_id"] for item in matches))

    def test_candidates_include_generic_symlink_with_exact_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "abcdefghijk.mp4"
            folder = root / "organized"
            folder.mkdir()
            link = folder / "video.mp4"
            source.write_bytes(b"media")
            link.symlink_to(source)
            fake_probe = {
                "resolved_path": str(source.resolve()),
                "size_bytes": 5, "duration_seconds": 1.0,
                "streams": [{"type": "video"}], "format": "mov",
            }
            with (
                patch.object(salvage, "MEDIA_ROOTS", (root,)),
                patch.object(salvage, "_CANDIDATE_ROOTS", (root,)),
                patch.object(salvage, "_quick_probe_path", return_value=(fake_probe, None)),
            ):
                matches = salvage.exact_id_candidates("abcdefghijk")
            # The source and organized symlink are the same inode, so only one
            # copy is reported rather than presenting the alias as a candidate.
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0]["resolved_path"], str(source.resolve()))

    def test_candidate_symlink_swap_cannot_validate_stale_exact_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate_root = root / "organized"
            candidate_root.mkdir()
            exact = root / "abcdefghijk.mp4"
            wrong = root / "lmnopqrstuv.mp4"
            link = candidate_root / "video.mp4"
            exact.write_bytes(b"exact")
            wrong.write_bytes(b"wrong")
            link.symlink_to(exact)
            real_quick_probe = salvage._quick_probe_path

            def swap_then_probe(path, **kwargs):
                link.unlink()
                link.symlink_to(wrong)
                return real_quick_probe(path, **kwargs)

            with (
                patch.object(salvage, "MEDIA_ROOTS", (root,)),
                patch.object(salvage, "_CANDIDATE_ROOTS", (candidate_root,)),
                patch.object(salvage, "_quick_probe_path", side_effect=swap_then_probe),
            ):
                inventory = salvage._exact_id_candidate_inventory("abcdefghijk")
            self.assertEqual(len(inventory["candidates"]), 1)
            candidate = inventory["candidates"][0]
            self.assertFalse(inventory["scan_complete"])
            self.assertIn("rescan", inventory["scan_message"].lower())
            self.assertFalse(candidate["valid"])
            self.assertFalse(candidate["exact_id"])
            self.assertFalse(candidate["exact_video_id_match"])
            self.assertNotIn("resolved_path", candidate)
            self.assertIn("changed", candidate["probe_error"])

    def test_candidate_revalidation_accepts_exact_entry_name_with_generic_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "opaque.mp4"
            link = root / "abcdefghijk.mp4"
            target.write_bytes(b"media")
            link.symlink_to(target)
            target_stat = target.stat()
            with (
                patch.object(salvage, "MEDIA_ROOTS", (root,)),
                patch.object(salvage, "_probe_media", return_value={
                    "format": "mov",
                    "declared_duration_seconds": 1.0,
                    "streams": [{"index": 0, "type": "video"}],
                }),
            ):
                probe, error = salvage._quick_probe_path(
                    link,
                    expected_identity=(target_stat.st_dev, target_stat.st_ino),
                    expected_video_id="abcdefghijk",
                )
            self.assertIsNone(error)
            self.assertIsNotNone(probe)
            self.assertEqual(probe["resolved_path"], str(target.resolve()))

    def test_candidate_revalidation_accepts_generic_entry_with_exact_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "abcdefghijk.mp4"
            link = root / "video.mp4"
            target.write_bytes(b"media")
            link.symlink_to(target)
            target_stat = target.stat()
            with (
                patch.object(salvage, "MEDIA_ROOTS", (root,)),
                patch.object(salvage, "_probe_media", return_value={
                    "format": "mov",
                    "declared_duration_seconds": 1.0,
                    "streams": [{"index": 0, "type": "video"}],
                }),
            ):
                probe, error = salvage._quick_probe_path(
                    link,
                    expected_identity=(target_stat.st_dev, target_stat.st_ino),
                    expected_video_id="abcdefghijk",
                )
            self.assertIsNone(error)
            self.assertIsNotNone(probe)
            self.assertEqual(probe["resolved_path"], str(target.resolve()))

    def test_candidate_inventory_rejects_conflicting_entry_and_target_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate_root = root / "organized"
            candidate_root.mkdir()
            target = root / "lmnopqrstuv.mp4"
            conflicting = candidate_root / "abcdefghijk.mp4"
            target.write_bytes(b"media")
            conflicting.symlink_to(target)
            with (
                patch.object(salvage, "MEDIA_ROOTS", (root,)),
                patch.object(salvage, "_CANDIDATE_ROOTS", (candidate_root,)),
                patch.object(salvage, "_probe_media") as probe_media,
            ):
                inventory = salvage._exact_id_candidate_inventory("abcdefghijk")
            self.assertEqual(inventory["candidates"], [])
            self.assertTrue(inventory["scan_complete"])
            probe_media.assert_not_called()


class SalvageRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = Flask(__name__)

    def test_diagnose_accepts_ui_source_path_and_expected_duration(self) -> None:
        fake_job = {"id": "job", "status": "queued"}
        with (
            self.app.test_request_context(json={
                "source_path": "/app/source/abcdefghijk.mp4",
                "expected_duration_seconds": 600,
            }),
            patch.object(salvage_routes, "start_job", return_value=(fake_job, None, 202)) as start,
        ):
            response, status = salvage_routes.api_salvage_diagnose.__wrapped__()
        self.assertEqual(status, 202)
        self.assertEqual(response.get_json()["job"], fake_job)
        self.assertEqual(start.call_args.kwargs["expected_duration_seconds"], 600)

    def test_start_accepts_operation_and_rejects_unknown_mode(self) -> None:
        with self.app.test_request_context(json={
            "source_path": "/app/source/abcdefghijk.mp4",
            "operation": "combine",
        }):
            with patch.object(
                salvage_routes, "start_job", return_value=(None, "bad mode", 400)
            ):
                response, status = salvage_routes.api_salvage_start.__wrapped__()
        self.assertEqual(status, 400)
        self.assertIn("remux or reencode", response.get_json()["error"])

    def test_start_requires_checksum_from_completed_diagnosis(self) -> None:
        with self.app.test_request_context(json={
            "source_path": "/app/source/abcdefghijk.mp4",
            "operation": "remux",
        }):
            response, status = salvage_routes.api_salvage_start.__wrapped__()
        self.assertEqual(status, 400)
        self.assertIn("expected_source_sha256", response.get_json()["error"])

    def test_candidate_route_rejects_non_exact_ids(self) -> None:
        with self.app.test_request_context("/api/salvage/candidates?video_id=short"):
            response, status = salvage_routes.api_salvage_candidates.__wrapped__()
        self.assertEqual(status, 400)
        self.assertIn("exact", response.get_json()["error"])

    def test_candidate_route_reports_concurrent_scan_as_busy(self) -> None:
        with (
            self.app.test_request_context(
                "/api/salvage/candidates?video_id=abcdefghijk"
            ),
            patch.object(
                salvage_routes,
                "exact_id_candidate_inventory",
                side_effect=salvage.SalvageError(
                    "An exact-ID candidate scan is already running"
                ),
            ),
        ):
            response, status = salvage_routes.api_salvage_candidates.__wrapped__()
        self.assertEqual(status, 429)
        self.assertIn("already running", response.get_json()["error"])

    def test_job_not_found_is_404(self) -> None:
        with (
            self.app.test_request_context(),
            patch.object(salvage_routes, "get_job", return_value=None),
        ):
            response, status = salvage_routes.api_salvage_job.__wrapped__("missing")
        self.assertEqual(status, 404)
        self.assertEqual(response.get_json()["error"], "Job not found")


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "FFmpeg and FFprobe are required for media salvage integration tests",
)
class SalvageFFmpegIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        salvage.reset_state_for_tests()

    def tearDown(self) -> None:
        salvage.reset_state_for_tests()

    def _make_mp4(self, path: Path, *, faststart: bool) -> None:
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=128x72:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
            "-t", "2", "-c:v", "mpeg4", "-q:v", "5", "-c:a", "aac",
        ]
        if faststart:
            command.extend(["-movflags", "+faststart"])
        command.append(str(path))
        subprocess.run(command, check=True, capture_output=True)

    def _wait(self, job_id: str, timeout: float = 45) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = salvage.get_job(job_id)
            if job and job["status"] in {"completed", "failed", "cancelled"}:
                return job
            time.sleep(0.05)
        self.fail(f"job {job_id} did not finish")

    def test_real_diagnosis_truncated_salvage_and_missing_moov(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original_dir = root / "original"
            damaged_dir = root / "damaged"
            original_dir.mkdir()
            damaged_dir.mkdir()
            original = original_dir / "abcdefghijk.mp4"
            damaged = damaged_dir / "abcdefghijk.mp4"
            self._make_mp4(original, faststart=True)
            original_bytes = original.read_bytes()
            damaged.write_bytes(original_bytes[: int(len(original_bytes) * 0.78)])
            damaged_hash = hashlib.sha256(damaged.read_bytes()).hexdigest()

            late_moov = root / "lmnopqrstuv.mp4"
            self._make_mp4(late_moov, faststart=False)
            late_bytes = late_moov.read_bytes()
            late_moov.write_bytes(late_bytes[: len(late_bytes) // 2])

            with (
                patch.object(salvage, "MEDIA_ROOTS", (root,)),
                patch.object(salvage, "_CANDIDATE_ROOTS", (root,)),
                patch.object(salvage, "SALVAGE_DIR", root / "salvage"),
            ):
                diagnosis, error, status = salvage.start_job(
                    str(original), "diagnose", expected_duration_seconds=2
                )
                self.assertEqual((error, status), (None, 202))
                diagnosed = self._wait(diagnosis["id"])
                self.assertEqual(diagnosed["status"], "completed", diagnosed)
                self.assertEqual(diagnosed["diagnosis"]["assessment"], "healthy")
                self.assertEqual(
                    diagnosed["diagnosis"]["input_sha256"],
                    hashlib.sha256(original_bytes).hexdigest(),
                )

                salvage_job, error, status = salvage.start_job(
                    str(damaged), "remux", expected_duration_seconds=2,
                    expected_source_sha256=damaged_hash,
                )
                self.assertEqual((error, status), (None, 202))
                salvaged = self._wait(salvage_job["id"])
                self.assertEqual(salvaged["status"], "completed", salvaged)
                output = Path(salvaged["output_path"])
                self.assertTrue(output.is_file())
                self.assertGreater(output.stat().st_size, 0)
                self.assertEqual(hashlib.sha256(damaged.read_bytes()).hexdigest(), damaged_hash)
                strict = subprocess.run(
                    [
                        "ffmpeg", "-hide_banner", "-v", "error",
                        "-err_detect", "explode", "-i", str(output),
                        "-map", "0:v:0?", "-map", "0:a:0?",
                        "-max_error_rate", "0", "-f", "null", "-",
                    ],
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(strict.returncode, 0, strict.stderr.decode(errors="replace"))

                broken, error, status = salvage.start_job(str(late_moov), "diagnose")
                self.assertEqual((error, status), (None, 202))
                broken_result = self._wait(broken["id"])
                self.assertEqual(broken_result["status"], "failed")
                self.assertIn("moov", broken_result["error"].lower())
                self.assertIn("cannot recover", broken_result["error"].lower())


if __name__ == "__main__":
    unittest.main()
