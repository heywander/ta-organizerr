from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from ta_organizerr import media_reservations, transcode
from ta_organizerr.routes import transcode as transcode_routes


class TranscodeSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        with transcode._active_transcodes_lock:
            transcode._active_transcodes.clear()
        media_reservations._reset_reservations_for_tests()

    def tearDown(self) -> None:
        with transcode._active_transcodes_lock:
            reservations = list(transcode._active_transcodes.values())
            transcode._active_transcodes.clear()
        for reservation in reservations:
            media_reservations.release_media(reservation)
        media_reservations._reset_reservations_for_tests()

    def _patch_allowed_root(self, root: Path):
        return patch.object(transcode, "_ALLOWED_BASES", (root,))

    def test_success_atomically_replaces_source_using_same_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "video.mkv"
            source.write_bytes(b"original")
            source.chmod(0o640)
            output_paths: list[Path] = []

            def fake_run(cmd, **_kwargs):
                output = Path(cmd[-1])
                output_paths.append(output)
                output.write_bytes(b"transcoded")
                return SimpleNamespace(returncode=0, stderr="", stdout="")

            with (
                self._patch_allowed_root(Path(tmp)),
                patch.object(
                    transcode,
                    "probe_streams",
                    return_value=[
                        transcode.MediaStream("video", "vp9"),
                        transcode.MediaStream("audio", "opus"),
                    ],
                ),
                patch.object(transcode.subprocess, "run", side_effect=fake_run),
            ):
                self.assertTrue(transcode.transcode_video(str(source)))

            self.assertEqual(source.read_bytes(), b"transcoded")
            self.assertEqual(source.stat().st_mode & 0o777, 0o640)
            self.assertEqual(output_paths[0].suffix, ".mkv")

    def test_replace_failure_never_unlinks_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "video.mp4"
            source.write_bytes(b"original")

            def fake_run(cmd, **_kwargs):
                Path(cmd[-1]).write_bytes(b"transcoded")
                return SimpleNamespace(returncode=0, stderr="", stdout="")

            with (
                self._patch_allowed_root(Path(tmp)),
                patch.object(
                    transcode,
                    "probe_streams",
                    return_value=[
                        transcode.MediaStream("video", "vp9"),
                        transcode.MediaStream("audio", "opus"),
                    ],
                ),
                patch.object(transcode.subprocess, "run", side_effect=fake_run),
                patch.object(os, "replace", side_effect=OSError("replace failed")),
            ):
                self.assertFalse(transcode.transcode_video(str(source)))

            self.assertEqual(source.read_bytes(), b"original")

    def test_same_source_cannot_be_reserved_twice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = (Path(tmp) / "video.mp4").resolve()
            source.write_bytes(b"original")
            first, _ = transcode._reserve(source)
            second, message = transcode._reserve(source)
            transcode._release(source)

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertIn("already", message)

    def test_commit_does_not_recreate_deleted_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "video.mp4"
            temp_output = Path(tmp) / ".video.transcoding.mp4"
            source.write_bytes(b"original")
            original_stat = source.stat()
            temp_output.write_bytes(b"transcoded")
            source.unlink()

            self.assertFalse(
                transcode._commit_output(temp_output, source, original_stat)
            )
            self.assertFalse(source.exists())

    def test_destruct_delete_refuses_active_transcode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = (Path(tmp) / "video.mp4").resolve()
            source.write_bytes(b"original")
            reserved, _ = transcode._reserve(source)
            try:
                with self.assertRaisesRegex(OSError, "currently being transcoded"):
                    transcode.delete_source_if_idle(source)
            finally:
                transcode._release(source)

            self.assertTrue(reserved)
            self.assertEqual(source.read_bytes(), b"original")

    def test_transcode_refuses_file_reserved_by_media_salvage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = (Path(tmp) / "video.mp4").resolve()
            source.write_bytes(b"original")
            reservation = media_reservations.reserve_media(
                (media_reservations.media_identity(source.stat()),),
                paths=(source,),
                owner="media salvage job test",
            )
            try:
                started, message = transcode._reserve(source)
            finally:
                media_reservations.release_media(reservation)

            self.assertFalse(started)
            self.assertIn("currently in use", message)

    def test_transcode_temporary_output_is_reserved_until_job_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = (Path(tmp) / "video.mp4").resolve()
            source.write_bytes(b"original")
            reserved, _message = transcode._reserve(source)
            self.assertTrue(reserved)
            output = transcode._temporary_output(source)
            try:
                with self.assertRaises(media_reservations.MediaReservationConflict):
                    media_reservations.reserve_media(
                        (media_reservations.media_identity(output.stat()),),
                        paths=(output,),
                        owner="media salvage job test",
                    )
            finally:
                output.unlink(missing_ok=True)
                transcode._release(source)

    def test_webm_is_rejected_instead_of_mislabeled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "video.webm"
            source.write_bytes(b"original")
            with self._patch_allowed_root(Path(tmp)):
                resolved, error = transcode._resolve_source(str(source))

        self.assertIsNone(resolved)
        self.assertIn("cannot safely hold H.264/AAC", error)

    def test_ffmpeg_command_preserves_all_input_streams(self) -> None:
        command = transcode._build_cmd(
            "/archive/video.mkv",
            "/archive/.video.transcoding.mkv",
            None,
            "libx264",
        )

        self.assertIn("-map", command)
        self.assertEqual(command[command.index("-map") + 1], "0")
        self.assertEqual(command[command.index("-c") + 1], "copy")
        self.assertIn("-map_metadata", command)
        self.assertIn("-map_chapters", command)

        vaapi = transcode._build_cmd(
            "/archive/video.mkv",
            "/archive/.video.transcoding.mkv",
            None,
            "h264_vaapi",
        )
        self.assertLess(vaapi.index("-hwaccel"), vaapi.index("-i"))

    def test_attached_thumbnail_is_copied_not_video_transcoded(self) -> None:
        command = transcode._build_cmd(
            "/archive/video.mkv",
            "/archive/.video.transcoding.mkv",
            None,
            "libx264",
            video_stream_index=1,
        )

        self.assertEqual(command[command.index("-c") + 1], "copy")
        self.assertIn("-c:v:1", command)
        self.assertNotIn("-c:v:0", command)

    def test_probe_failure_refuses_transcode_without_running_ffmpeg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "video.mp4"
            source.write_bytes(b"original")
            with (
                self._patch_allowed_root(Path(tmp)),
                patch.object(transcode, "probe_streams", return_value=None),
                patch.object(transcode, "_temporary_output") as temporary_output,
            ):
                self.assertFalse(transcode.transcode_video(str(source)))

            temporary_output.assert_not_called()

    def test_route_rejects_nul_and_oversize_paths(self) -> None:
        app = Flask(__name__)
        for filepath in ("/app/source/bad\x00.mp4", "/" + "a" * 4096):
            with app.test_request_context(json={"filepath": filepath}):
                _response, status = (
                    transcode_routes.api_transcode_start.__wrapped__()
                )
            self.assertEqual(status, 400)

    def test_route_reports_shared_media_reservation_as_conflict(self) -> None:
        app = Flask(__name__)
        with (
            app.test_request_context(json={"filepath": "/app/source/video.mp4"}),
            patch.object(transcode_routes, "path_in_allowed_tree", return_value=True),
            patch.object(transcode_routes, "detect_encoder", return_value="libx264"),
            patch.object(
                transcode_routes,
                "start_transcode_job",
                return_value=(
                    False,
                    "Cannot reserve this media; media is currently in use by media salvage",
                ),
            ),
        ):
            response, status = transcode_routes.api_transcode_start.__wrapped__()

        self.assertEqual(status, 409)
        self.assertIn("currently in use", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
