from __future__ import annotations

import tempfile
import unittest
import sqlite3
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from flask import Flask

from ta_organizerr import media_reservations, recovery
from ta_organizerr.routes import recovery as recovery_routes
from ta_organizerr.utils import path_in_allowed_tree, path_within, sanitize


VIDEO_ID = "abcdefghijk"


class PathEntrySafetyTests(unittest.TestCase):
    def test_final_symlink_entry_is_allowed_but_target_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            allowed = root / "allowed"
            outside = root / "outside"
            allowed.mkdir()
            outside.mkdir()
            target = outside / "video.mp4"
            target.write_bytes(b"media")
            link = allowed / "video.mp4"
            link.symlink_to(target)

            self.assertTrue(path_in_allowed_tree(link, (allowed,)))
            self.assertFalse(path_within(link, (allowed,)))

    def test_symlinked_ancestor_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            allowed = root / "allowed"
            outside = root / "outside"
            allowed.mkdir()
            outside.mkdir()
            (allowed / "jump").symlink_to(outside, target_is_directory=True)

            candidate = allowed / "jump" / "video.mp4"

            self.assertFalse(path_in_allowed_tree(candidate, (allowed,)))

    def test_sanitize_keeps_ordinary_names_and_blocks_unsafe_components(self) -> None:
        self.assertEqual(sanitize("Ordinary Channel - 2026"), "Ordinary Channel - 2026")
        self.assertEqual(sanitize("line\nbreak\x00"), "linebreak")
        self.assertEqual(sanitize("."), "untitled")
        self.assertEqual(sanitize(".."), "untitled")
        self.assertEqual(sanitize("频道"), "untitled")
        self.assertEqual(len(sanitize("a" * 500)), 200)


class AtomicRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        media_reservations._reset_reservations_for_tests()

    def tearDown(self) -> None:
        media_reservations._reset_reservations_for_tests()

    def test_same_source_and_destination_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / f"Title [{VIDEO_ID}].mp4"
            media.write_bytes(b"original")

            staged_path, method = recovery._prepare_media_stage(media, media)

            self.assertIsNone(staged_path)
            self.assertEqual(method, "already staged")
            self.assertEqual(media.read_bytes(), b"original")

    def test_replace_failure_preserves_source_and_cleans_temp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "source"
            import_dir = root / "import"
            source_dir.mkdir()
            import_dir.mkdir()
            source = source_dir / f"Title [{VIDEO_ID}].mp4"
            destination = import_dir / source.name
            metadata_destination = import_dir / f"Title [{VIDEO_ID}].info.json"
            source.write_bytes(b"original")
            destination.write_bytes(b"existing destination")
            metadata_destination.write_text("old metadata", encoding="utf-8")
            media_temp, _method = recovery._prepare_media_stage(source, destination)
            metadata_temp = recovery._write_json_temp(
                metadata_destination,
                {"id": VIDEO_ID},
            )
            real_replace = recovery.os.replace

            def fail_media_publish(staged, final):
                if Path(final) == destination:
                    raise OSError("boom")
                return real_replace(staged, final)

            with patch.object(
                recovery.os,
                "replace",
                side_effect=fail_media_publish,
            ):
                with self.assertRaises(OSError):
                    recovery._publish_import_pair(
                        metadata_temp,
                        metadata_destination,
                        media_temp,
                        destination,
                    )

            self.assertEqual(source.read_bytes(), b"original")
            self.assertEqual(destination.read_bytes(), b"existing destination")
            self.assertEqual(
                metadata_destination.read_text(encoding="utf-8"),
                "old metadata",
            )
            self.assertEqual(
                sorted(path.name for path in import_dir.iterdir()),
                sorted([destination.name, metadata_destination.name]),
            )

    def test_zero_exit_without_metadata_does_not_stage_media(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "source"
            import_dir = root / "import"
            source_dir.mkdir()
            import_dir.mkdir()
            source = source_dir / f"Title [{VIDEO_ID}].mp4"
            source.write_bytes(b"original")
            completed = SimpleNamespace(returncode=0, stdout="", stderr="")

            with (
                patch.object(recovery, "IMPORT_DIR", import_dir),
                patch.object(recovery.subprocess, "run", return_value=completed),
                patch.object(recovery, "_record_lost_media") as record_lost,
            ):
                success, message = recovery.recover_video_metadata(str(source))

            self.assertFalse(success)
            self.assertIn("without producing metadata", message)
            self.assertEqual(source.read_bytes(), b"original")
            self.assertFalse((import_dir / source.name).exists())
            record_lost.assert_called_once_with(VIDEO_ID, source)

    def test_recovery_commits_generated_metadata_and_media(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "source"
            import_dir = root / "import"
            source_dir.mkdir()
            import_dir.mkdir()
            source = source_dir / f"Title [{VIDEO_ID}].mp4"
            source.write_bytes(b"original")

            def fake_run(command, **_kwargs):
                output_template = Path(command[command.index("-o") + 1])
                Path(f"{output_template}.info.json").write_text(
                    '{"id": "abcdefghijk"}', encoding="utf-8"
                )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with (
                patch.object(recovery, "IMPORT_DIR", import_dir),
                patch.object(recovery.subprocess, "run", side_effect=fake_run),
            ):
                success, _message = recovery.recover_video_metadata(str(source))

            self.assertTrue(success)
            self.assertEqual(source.read_bytes(), b"original")
            self.assertEqual((import_dir / source.name).read_bytes(), b"original")
            self.assertTrue((import_dir / f"Title [{VIDEO_ID}].info.json").is_file())
            self.assertEqual(list(import_dir.glob(".recovery-*.info.json")), [])

    def test_metadata_recovery_in_import_dir_keeps_media(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            import_dir = Path(tmp)
            source = import_dir / f"Title [{VIDEO_ID}].mp4"
            source.write_bytes(b"original")

            def fake_run(command, **_kwargs):
                output_template = Path(command[command.index("-o") + 1])
                Path(f"{output_template}.info.json").write_text(
                    '{"id": "abcdefghijk"}', encoding="utf-8"
                )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with (
                patch.object(recovery, "IMPORT_DIR", import_dir),
                patch.object(recovery.subprocess, "run", side_effect=fake_run),
            ):
                success, _message = recovery.recover_video_metadata(str(source))

            self.assertTrue(success)
            self.assertEqual(source.read_bytes(), b"original")
            self.assertTrue((import_dir / f"Title [{VIDEO_ID}].info.json").is_file())

    def test_force_import_metadata_is_readable_by_other_services(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "metadata.info.json"

            metadata_temp = recovery._write_json_temp(destination, {"id": VIDEO_ID})
            recovery._publish_import_pair(
                metadata_temp,
                destination,
                None,
                Path(tmp) / "unused-media.mp4",
            )

            self.assertEqual(destination.stat().st_mode & 0o777, 0o644)

    def test_json_staging_cleans_temp_when_serialization_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "metadata.info.json"

            with self.assertRaises(TypeError):
                recovery._write_json_temp(destination, {"bad": object()})

            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_import_publishes_metadata_before_media(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            metadata_temp = root / ".metadata.tmp"
            media_temp = root / ".media.tmp"
            metadata_destination = root / "video.info.json"
            media_destination = root / "video.mp4"
            metadata_temp.write_text("{}", encoding="utf-8")
            media_temp.write_bytes(b"media")
            destinations: list[Path] = []
            real_replace = recovery.os.replace

            def tracked_replace(source, destination):
                destinations.append(Path(destination))
                return real_replace(source, destination)

            with patch.object(recovery.os, "replace", side_effect=tracked_replace):
                recovery._publish_import_pair(
                    metadata_temp,
                    metadata_destination,
                    media_temp,
                    media_destination,
                )

            self.assertLess(
                destinations.index(metadata_destination),
                destinations.index(media_destination),
            )

    def test_force_import_in_import_dir_keeps_media(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            import_dir = Path(tmp)
            source = import_dir / f"Title [{VIDEO_ID}].mp4"
            source.write_bytes(b"original")
            connection = Mock()

            with (
                patch.object(recovery, "IMPORT_DIR", import_dir),
                patch.object(recovery, "get_db", return_value=nullcontext(connection)),
            ):
                success, _message = recovery.force_import(str(source))

            self.assertTrue(success)
            self.assertEqual(source.read_bytes(), b"original")
            self.assertTrue((import_dir / f"Title [{VIDEO_ID}].info.json").is_file())

    def test_reserved_source_is_copied_without_hardlink_ctime_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source" / f"Title [{VIDEO_ID}].mp4"
            destination = root / "import" / source.name
            source.parent.mkdir()
            destination.parent.mkdir()
            source.write_bytes(b"original")
            before = source.stat()
            reservation = media_reservations.reserve_media(
                (media_reservations.media_identity(before),),
                paths=(source,),
                owner="media salvage job test",
            )
            try:
                staged, method = recovery._prepare_media_stage(source, destination)
            finally:
                media_reservations.release_media(reservation)

            try:
                after = source.stat()
                self.assertEqual(method, "copied")
                self.assertEqual(after.st_nlink, before.st_nlink)
                self.assertEqual(after.st_ctime_ns, before.st_ctime_ns)
                self.assertEqual(staged.read_bytes(), b"original")
            finally:
                staged.unlink(missing_ok=True)

    def test_import_refuses_reserved_destination_and_cleans_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media_destination = root / "video.mp4"
            metadata_destination = root / "video.info.json"
            media_temp = root / ".media.tmp"
            metadata_temp = root / ".metadata.tmp"
            media_destination.write_bytes(b"keep media")
            metadata_destination.write_text("keep metadata", encoding="utf-8")
            media_temp.write_bytes(b"replacement")
            metadata_temp.write_text("{}", encoding="utf-8")
            reservation = media_reservations.reserve_media(
                (media_reservations.media_identity(media_destination.stat()),),
                paths=(media_destination,),
                owner="media salvage job test",
            )
            try:
                with self.assertRaises(media_reservations.MediaReservationConflict):
                    recovery._publish_import_pair(
                        metadata_temp,
                        metadata_destination,
                        media_temp,
                        media_destination,
                    )
            finally:
                media_reservations.release_media(reservation)

            self.assertEqual(media_destination.read_bytes(), b"keep media")
            self.assertEqual(
                metadata_destination.read_text(encoding="utf-8"),
                "keep metadata",
            )
            self.assertFalse(media_temp.exists())
            self.assertFalse(metadata_temp.exists())


class UnindexedChannelAttributionTests(unittest.TestCase):
    def test_valid_nested_source_channel_id_is_derived(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            channel_id = "UC" + "A" * 22
            media = source / channel_id / "nested" / f"{VIDEO_ID}.mp4"

            self.assertEqual(
                recovery._source_channel_id(media, source),
                channel_id,
            )

    def test_invalid_channel_folder_and_direct_root_file_are_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"

            self.assertIsNone(
                recovery._source_channel_id(
                    source / "Channel Name" / f"{VIDEO_ID}.mp4",
                    source,
                )
            )
            self.assertIsNone(
                recovery._source_channel_id(source / f"{VIDEO_ID}.mp4", source)
            )

    def test_scan_preserves_two_distinct_source_channel_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            target = root / "target"
            hidden = root / "hidden"
            for directory in (source, target, hidden):
                directory.mkdir()
            first_channel = "UC" + "A" * 22
            second_channel = "UC" + "B" * 22
            first = source / first_channel / f"{VIDEO_ID}.mp4"
            second = source / second_channel / "ABCDEFGHIJK.mkv"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            connection = Mock()
            connection.execute.return_value.fetchall.return_value = []

            with (
                patch.object(recovery, "SOURCE_DIR", source),
                patch.object(recovery, "TARGET_DIR", target),
                patch.object(recovery, "HIDDEN_DIR", hidden),
                patch.object(recovery, "fetch_all_metadata", return_value={}),
                patch.object(recovery, "get_db", return_value=nullcontext(connection)),
            ):
                results = recovery.scan_for_unindexed_videos()

            by_video_id = {row["video_id"]: row for row in results["unindexed"]}
            self.assertEqual(by_video_id[VIDEO_ID]["source_channel_id"], first_channel)
            self.assertEqual(
                by_video_id["ABCDEFGHIJK"]["source_channel_id"],
                second_channel,
            )

    def test_target_real_file_has_no_source_channel_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            target = root / "target"
            hidden = root / "hidden"
            for directory in (source, target, hidden):
                directory.mkdir()
            media = target / ("UC" + "A" * 22) / f"{VIDEO_ID}.mp4"
            media.parent.mkdir()
            media.write_bytes(b"target copy")
            connection = Mock()
            connection.execute.return_value.fetchall.return_value = []

            with (
                patch.object(recovery, "SOURCE_DIR", source),
                patch.object(recovery, "TARGET_DIR", target),
                patch.object(recovery, "HIDDEN_DIR", hidden),
                patch.object(recovery, "fetch_all_metadata", return_value={}),
                patch.object(recovery, "get_db", return_value=nullcontext(connection)),
            ):
                results = recovery.scan_for_unindexed_videos()

            self.assertEqual(len(results["unindexed"]), 1)
            self.assertIsNone(results["unindexed"][0]["source_channel_id"])


class DeleteBatchSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        media_reservations._reset_reservations_for_tests()

    def tearDown(self) -> None:
        media_reservations._reset_reservations_for_tests()

    def test_parent_cleanup_never_removes_source_or_import_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            import_dir = root / "import"
            target = root / "target"
            hidden = root / "hidden"
            for directory in (source, import_dir, target, hidden):
                directory.mkdir()

            with (
                patch.object(recovery_routes, "TARGET_DIR", target),
                patch.object(recovery_routes, "HIDDEN_DIR", hidden),
            ):
                recovery_routes._cleanup_empty_organized_parent(source)
                recovery_routes._cleanup_empty_organized_parent(import_dir)

            self.assertTrue(source.is_dir())
            self.assertTrue(import_dir.is_dir())

    def test_single_delete_reports_database_reconciliation_failure(self) -> None:
        app = Flask(__name__)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            target.mkdir()
            media = target / f"Title [{VIDEO_ID}].mp4"
            media.write_bytes(b"target")

            with (
                app.test_request_context(json={"filepath": str(media)}),
                patch.object(recovery_routes, "RECOVERY_ALLOWED_BASES", (source, target)),
                patch.object(recovery_routes, "SOURCE_DIR", source),
                patch.object(
                    recovery_routes,
                    "get_db",
                    side_effect=sqlite3.OperationalError("locked"),
                ),
            ):
                response, status = recovery_routes.api_recovery_delete.__wrapped__()

            self.assertEqual(status, 500)
            self.assertTrue(response.get_json()["filesystem_deleted"])
            self.assertFalse(media.exists())

    def test_batch_continues_with_explicit_database_sync_failure(self) -> None:
        app = Flask(__name__)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            target.mkdir()
            media = target / f"Title [{VIDEO_ID}].mp4"
            media.write_bytes(b"target")

            with (
                app.test_request_context(json={
                    "filepaths": [str(media)],
                    "destruct_mode": False,
                }),
                patch.object(recovery_routes, "RECOVERY_ALLOWED_BASES", (source, target)),
                patch.object(recovery_routes, "SOURCE_DIR", source),
                patch.object(
                    recovery_routes,
                    "get_db",
                    side_effect=sqlite3.OperationalError("locked"),
                ),
            ):
                response = recovery_routes.api_recovery_delete_batch.__wrapped__()

            payload = response.get_json()
            self.assertEqual(payload["success_count"], 0)
            self.assertEqual(payload["fail_count"], 1)
            self.assertIn("db_sync_failed", payload["errors"][0])
            self.assertFalse(media.exists())

    def test_single_delete_returns_conflict_for_salvage_requested_path(self) -> None:
        app = Flask(__name__)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            target.mkdir()
            media = target / f"Title [{VIDEO_ID}].mp4"
            media.write_bytes(b"target")
            reservation = media_reservations.reserve_media(
                (media_reservations.media_identity(media.stat()),),
                paths=(media,),
                owner="media salvage job test",
            )
            try:
                with (
                    app.test_request_context(json={"filepath": str(media)}),
                    patch.object(
                        recovery_routes,
                        "RECOVERY_ALLOWED_BASES",
                        (source, target),
                    ),
                    patch.object(recovery_routes, "SOURCE_DIR", source),
                ):
                    response, status = recovery_routes.api_recovery_delete.__wrapped__()
            finally:
                media_reservations.release_media(reservation)

            self.assertEqual(status, 409)
            self.assertIn("currently in use", response.get_json()["error"])
            self.assertEqual(media.read_bytes(), b"target")

    def test_source_lookup_walks_archive_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            class CountingPath(type(Path())):
                calls = 0

                def rglob(self, pattern):
                    type(self).calls += 1
                    return super().rglob(pattern)

            root = CountingPath(tmp)
            first = root / f"First [{VIDEO_ID}].mp4"
            second_id = "ABCDEFGHIJK"
            second = root / f"Second [{second_id}].mkv"
            first.write_bytes(b"first")
            second.write_bytes(b"second")

            lookup = recovery.make_source_lookup(root)
            self.assertEqual(lookup(VIDEO_ID), first)
            self.assertEqual(lookup(second_id), second)

            self.assertEqual(CountingPath.calls, 1)

    def test_valid_media_source_passes_route_confinement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "source"
            source_dir.mkdir()
            source = source_dir / f"Title [{VIDEO_ID}].mp4"
            source.write_bytes(b"original")

            with patch.object(
                recovery_routes,
                "RECOVERY_ALLOWED_BASES",
                (source_dir,),
            ):
                resolved, error = recovery_routes._validated_media_source(str(source))

        self.assertIsNone(error)
        self.assertEqual(resolved, source)

    def test_payload_requires_strict_boolean_and_path_strings(self) -> None:
        _paths, _destruct, error = recovery._parse_delete_batch_payload([])
        self.assertEqual(error, "Request body must be a JSON object")

        _paths, _destruct, error = recovery._parse_delete_batch_payload(
            {"filepaths": ["/tmp/video.mp4"], "destruct_mode": "false"}
        )
        self.assertEqual(error, "destruct_mode must be a boolean")

        _paths, _destruct, error = recovery._parse_delete_batch_payload(
            {"filepaths": [123], "destruct_mode": False}
        )
        self.assertIsNotNone(error)

        _paths, _destruct, error = recovery._parse_delete_batch_payload(
            {"filepaths": ["/tmp/video.mp4"] * 501, "destruct_mode": False}
        )
        self.assertEqual(error, "filepaths may contain at most 500 paths")

    def test_normal_mode_rejects_source_and_missing_is_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "source"
            target_dir = root / "target"
            hidden_dir = root / "hidden"
            import_dir = root / "import"
            for directory in (source_dir, target_dir, hidden_dir, import_dir):
                directory.mkdir()
            source = source_dir / f"Title [{VIDEO_ID}].mp4"
            source.write_bytes(b"original")
            missing = target_dir / "missing.mp4"

            allowed_bases = (source_dir, target_dir, hidden_dir, import_dir)
            self.assertEqual(
                recovery._delete_candidate_error(
                    source,
                    destruct=False,
                    allowed_bases=allowed_bases,
                    source_dir=source_dir,
                ),
                "Source files require destruct mode",
            )
            self.assertIsNone(
                recovery._delete_candidate_error(
                    source,
                    destruct=True,
                    allowed_bases=allowed_bases,
                    source_dir=source_dir,
                )
            )
            self.assertEqual(
                recovery._delete_candidate_error(
                    missing,
                    destruct=False,
                    allowed_bases=allowed_bases,
                    source_dir=source_dir,
                ),
                "File not found",
            )
            self.assertEqual(source.read_bytes(), b"original")

    def test_directories_and_configured_roots_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "source"
            target_dir = root / "target"
            hidden_dir = root / "hidden"
            import_dir = root / "import"
            child_dir = target_dir / "channel"
            for directory in (source_dir, target_dir, hidden_dir, import_dir, child_dir):
                directory.mkdir()

            allowed_bases = (source_dir, target_dir, hidden_dir, import_dir)
            self.assertEqual(
                recovery._delete_candidate_error(
                    target_dir,
                    destruct=True,
                    allowed_bases=allowed_bases,
                    source_dir=source_dir,
                ),
                "Configured root directories cannot be deleted",
            )
            self.assertEqual(
                recovery._delete_candidate_error(
                    child_dir,
                    destruct=True,
                    allowed_bases=allowed_bases,
                    source_dir=source_dir,
                ),
                "Directory deletion is not allowed",
            )

    def test_recursive_fallback_matches_exact_video_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "source"
            source_dir.mkdir()
            substring = source_dir / f"prefix-{VIDEO_ID}-suffix.mp4"
            substring.write_bytes(b"wrong")
            target = Path(tmp) / "target" / "video.mp4"

            self.assertIsNone(
                recovery._resolve_destruct_source(
                    target,
                    VIDEO_ID,
                    {},
                    source_dir=source_dir,
                )
            )
            exact = source_dir / f"Title [{VIDEO_ID}].mkv"
            exact.write_bytes(b"right")
            self.assertEqual(
                recovery._resolve_destruct_source(
                    target,
                    VIDEO_ID,
                    {},
                    source_dir=source_dir,
                ),
                exact,
            )


if __name__ == "__main__":
    unittest.main()
