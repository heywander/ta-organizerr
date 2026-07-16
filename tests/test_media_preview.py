from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from flask import Flask

from ta_organizerr import utils
from ta_organizerr.routes import recovery as recovery_routes


class RecoveryMediaPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = Flask(__name__, static_folder=None)
        self.app.secret_key = "preview-test-secret"
        self.app.register_blueprint(recovery_routes.bp)
        self.client = self.app.test_client()

    def _login(self) -> None:
        with self.client.session_transaction() as session:
            session["logged_in"] = True

    def test_preview_requires_authentication(self) -> None:
        response = self.client.get(
            "/api/recovery/preview",
            query_string={"filepath": "/tmp/video.mp4"},
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.headers["Cross-Origin-Resource-Policy"],
            "same-origin",
        )

    def test_get_head_and_single_ranges_stream_symlink_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "source"
            target_dir = root / "target"
            source_dir.mkdir()
            target_dir.mkdir()
            source = source_dir / "Recovered video.mp4"
            source.write_bytes(b"0123456789abcdef")
            link = target_dir / source.name
            link.symlink_to(source)
            self._login()

            with patch.object(
                recovery_routes,
                "RECOVERY_ALLOWED_BASES",
                (source_dir, target_dir),
            ):
                response = self.client.get(
                    "/api/recovery/preview",
                    query_string={"filepath": str(link)},
                    buffered=True,
                )
                ranged = self.client.get(
                    "/api/recovery/preview",
                    query_string={"filepath": str(link)},
                    headers={"Range": "bytes=2-5"},
                    buffered=True,
                )
                suffix = self.client.get(
                    "/api/recovery/preview",
                    query_string={"filepath": str(link)},
                    headers={"Range": "bytes=-4"},
                    buffered=True,
                )
                head = self.client.head(
                    "/api/recovery/preview",
                    query_string={"filepath": str(link)},
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b"0123456789abcdef")
            self.assertEqual(response.headers["Content-Type"], "video/mp4")
            self.assertEqual(response.headers["Content-Length"], "16")
            self.assertEqual(response.headers["Accept-Ranges"], "bytes")
            self.assertEqual(response.headers["Content-Disposition"], "inline")
            self.assertIn("no-store", response.headers["Cache-Control"])
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(
                response.headers["Cross-Origin-Resource-Policy"],
                "same-origin",
            )

            self.assertEqual(ranged.status_code, 206)
            self.assertEqual(ranged.data, b"2345")
            self.assertEqual(ranged.headers["Content-Range"], "bytes 2-5/16")
            self.assertEqual(ranged.headers["Content-Length"], "4")

            self.assertEqual(suffix.status_code, 206)
            self.assertEqual(suffix.data, b"cdef")
            self.assertEqual(suffix.headers["Content-Range"], "bytes 12-15/16")

            self.assertEqual(head.status_code, 200)
            self.assertEqual(head.data, b"")
            self.assertEqual(head.headers["Content-Length"], "16")
            self.assertEqual(head.headers["Content-Type"], "video/mp4")

    def test_host_path_symlink_is_translated_before_streaming(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "source"
            target_dir = root / "target"
            host_dir = root / "host-source"
            relative_path = Path("channel") / "Recovered video.webm"
            (source_dir / relative_path.parent).mkdir(parents=True)
            target_dir.mkdir()
            source = source_dir / relative_path
            source.write_bytes(b"webm-media")
            link = target_dir / "Recovered video.webm"
            # The host path intentionally does not exist in this namespace. The
            # app must map it to the configured container source mount.
            link.symlink_to(host_dir / relative_path)
            self._login()

            with (
                patch.object(
                    recovery_routes,
                    "RECOVERY_ALLOWED_BASES",
                    (source_dir, target_dir),
                ),
                patch.object(utils, "HOST_SOURCE_BASE", host_dir),
                patch.object(utils, "SOURCE_DIR", source_dir),
            ):
                response = self.client.get(
                    "/api/recovery/preview",
                    query_string={"filepath": str(link)},
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b"webm-media")
            self.assertEqual(response.headers["Content-Type"], "video/webm")

    def test_rejects_outside_paths_and_symlinked_ancestors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            allowed = root / "allowed"
            outside = root / "outside"
            allowed.mkdir()
            outside.mkdir()
            outside_media = outside / "private.mp4"
            outside_media.write_bytes(b"private")
            final_link = allowed / "final.mp4"
            final_link.symlink_to(outside_media)
            (allowed / "jump").symlink_to(outside, target_is_directory=True)
            self._login()

            with patch.object(recovery_routes, "RECOVERY_ALLOWED_BASES", (allowed,)):
                direct = self.client.get(
                    "/api/recovery/preview",
                    query_string={"filepath": str(outside_media)},
                )
                final_symlink = self.client.get(
                    "/api/recovery/preview",
                    query_string={"filepath": str(final_link)},
                )
                ancestor_symlink = self.client.get(
                    "/api/recovery/preview",
                    query_string={"filepath": str(allowed / "jump" / "private.mp4")},
                )

            self.assertEqual(direct.status_code, 400)
            self.assertEqual(final_symlink.status_code, 400)
            self.assertEqual(ancestor_symlink.status_code, 400)

    def test_rejects_missing_directories_unsupported_and_bad_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            allowed = Path(tmp) / "allowed"
            allowed.mkdir()
            directory = allowed / "folder.mp4"
            directory.mkdir()
            unsupported = allowed / "secrets.txt"
            unsupported.write_text("not media", encoding="utf-8")
            disguised = allowed / "disguised.mp4"
            disguised.symlink_to(unsupported)
            missing = allowed / "missing.mp4"
            self._login()

            with patch.object(recovery_routes, "RECOVERY_ALLOWED_BASES", (allowed,)):
                directory_response = self.client.get(
                    "/api/recovery/preview",
                    query_string={"filepath": str(directory)},
                )
                unsupported_response = self.client.get(
                    "/api/recovery/preview",
                    query_string={"filepath": str(unsupported)},
                )
                disguised_response = self.client.get(
                    "/api/recovery/preview",
                    query_string={"filepath": str(disguised)},
                )
                missing_response = self.client.get(
                    "/api/recovery/preview",
                    query_string={"filepath": str(missing)},
                )
                nul_response = self.client.get(
                    "/api/recovery/preview",
                    query_string={"filepath": f"{allowed}/bad\x00.mp4"},
                )
                oversized_response = self.client.get(
                    "/api/recovery/preview",
                    query_string={"filepath": "/" + "a" * 4096},
                )

            self.assertEqual(directory_response.status_code, 400)
            self.assertEqual(unsupported_response.status_code, 415)
            self.assertEqual(disguised_response.status_code, 415)
            self.assertEqual(missing_response.status_code, 404)
            self.assertEqual(nul_response.status_code, 400)
            self.assertEqual(oversized_response.status_code, 400)

    def test_unsatisfiable_and_multiple_ranges_return_416(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            allowed = Path(tmp) / "allowed"
            allowed.mkdir()
            media = allowed / "video.mp4"
            media.write_bytes(b"1234")
            self._login()

            with patch.object(recovery_routes, "RECOVERY_ALLOWED_BASES", (allowed,)):
                past_end = self.client.get(
                    "/api/recovery/preview",
                    query_string={"filepath": str(media)},
                    headers={"Range": "bytes=10-20"},
                )
                multiple = self.client.get(
                    "/api/recovery/preview",
                    query_string={"filepath": str(media)},
                    headers={"Range": "bytes=0-1,3-3"},
                )

            self.assertEqual(past_end.status_code, 416)
            self.assertEqual(past_end.headers["Content-Range"], "bytes */4")
            self.assertEqual(multiple.status_code, 416)
            self.assertEqual(multiple.headers["Content-Range"], "bytes */4")

    def test_raw_preview_guard_releases_once_when_response_closes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            allowed = Path(tmp) / "allowed"
            allowed.mkdir()
            media = allowed / "video.mp4"
            media.write_bytes(b"stream remains open")
            guard = Mock()
            guard.acquire.return_value = True
            self._login()

            with (
                patch.object(recovery_routes, "RECOVERY_ALLOWED_BASES", (allowed,)),
                patch.object(recovery_routes, "_RAW_PREVIEW_GUARD", guard),
            ):
                response = self.client.get(
                    "/api/recovery/preview",
                    query_string={"filepath": str(media)},
                    buffered=False,
                )
                guard.acquire.assert_called_once_with(blocking=False)
                response.close()
                response.close()

            guard.release.assert_called_once_with()

    def test_raw_preview_guard_returns_retryable_429_when_full(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            allowed = Path(tmp) / "allowed"
            allowed.mkdir()
            media = allowed / "video.mp4"
            media.write_bytes(b"media")
            guard = Mock()
            guard.acquire.return_value = False
            self._login()

            with (
                patch.object(recovery_routes, "RECOVERY_ALLOWED_BASES", (allowed,)),
                patch.object(recovery_routes, "_RAW_PREVIEW_GUARD", guard),
            ):
                response = self.client.get(
                    "/api/recovery/preview",
                    query_string={"filepath": str(media)},
                )

            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.headers["Retry-After"], "2")
            guard.release.assert_not_called()

    def test_audio_preview_requires_auth_and_confines_before_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            allowed = Path(tmp) / "allowed"
            outside = Path(tmp) / "outside"
            allowed.mkdir()
            outside.mkdir()
            media = outside / "private.mp4"
            media.write_bytes(b"media")

            unauthorized = self.client.get(
                "/api/recovery/preview-audio",
                query_string={"filepath": str(media)},
            )
            self._login()
            with (
                patch.object(recovery_routes, "RECOVERY_ALLOWED_BASES", (allowed,)),
                patch.object(recovery_routes.subprocess, "run") as run,
                patch.object(recovery_routes.subprocess, "Popen") as popen,
            ):
                outside_response = self.client.get(
                    "/api/recovery/preview-audio",
                    query_string={"filepath": str(media)},
                )

            self.assertEqual(unauthorized.status_code, 401)
            self.assertEqual(
                unauthorized.headers["Cross-Origin-Resource-Policy"],
                "same-origin",
            )
            self.assertEqual(outside_response.status_code, 400)
            run.assert_not_called()
            popen.assert_not_called()

    def test_audio_preview_passes_validated_fd_and_streams_mp3(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            allowed = Path(tmp) / "allowed"
            allowed.mkdir()
            media = allowed / "video.mkv"
            media.write_bytes(b"container bytes")
            guard = Mock()
            guard.acquire.return_value = True
            process = Mock()
            process.stdout = io.BytesIO(b"ID3-compatible-audio")
            process.poll.return_value = 0
            process.wait.return_value = 0
            probe_result = SimpleNamespace(returncode=0, stdout="1\n")
            self._login()

            with (
                patch.object(recovery_routes, "RECOVERY_ALLOWED_BASES", (allowed,)),
                patch.object(recovery_routes, "_AUDIO_PREVIEW_GUARD", guard),
                patch.object(
                    recovery_routes.subprocess,
                    "run",
                    return_value=probe_result,
                ) as run,
                patch.object(
                    recovery_routes.subprocess,
                    "Popen",
                    return_value=process,
                ) as popen,
            ):
                response = self.client.get(
                    "/api/recovery/preview-audio",
                    query_string={"filepath": str(media)},
                    buffered=True,
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b"ID3-compatible-audio")
            self.assertEqual(response.headers["Content-Type"], "audio/mpeg")
            self.assertEqual(response.headers["Content-Disposition"], "inline")
            self.assertIn("no-store", response.headers["Cache-Control"])
            self.assertEqual(
                response.headers["Cross-Origin-Resource-Policy"],
                "same-origin",
            )

            probe_command = run.call_args.args[0]
            probe_kwargs = run.call_args.kwargs
            transcode_command = popen.call_args.args[0]
            transcode_kwargs = popen.call_args.kwargs
            probe_stdin = probe_kwargs["stdin"]
            transcode_stdin = transcode_kwargs["stdin"]
            self.assertIs(probe_stdin, transcode_stdin)
            self.assertTrue(probe_stdin.closed)
            self.assertEqual(probe_command[-1], "pipe:0")
            self.assertEqual(
                transcode_command[transcode_command.index("-i") + 1],
                "pipe:0",
            )
            self.assertNotIn(str(media), probe_command)
            self.assertNotIn(str(media), transcode_command)
            self.assertEqual(
                probe_command[probe_command.index("-protocol_whitelist") + 1],
                "pipe",
            )
            self.assertEqual(
                transcode_command[
                    transcode_command.index("-protocol_whitelist") + 1
                ],
                "pipe",
            )
            self.assertEqual(
                probe_command[probe_command.index("-format_whitelist") + 1],
                "matroska",
            )
            self.assertEqual(
                transcode_command[
                    transcode_command.index("-format_whitelist") + 1
                ],
                "matroska",
            )
            self.assertNotIn("pass_fds", probe_kwargs)
            self.assertNotIn("pass_fds", transcode_kwargs)
            self.assertFalse(probe_kwargs["shell"])
            self.assertFalse(transcode_kwargs["shell"])
            self.assertIn("0:a:0", transcode_command)
            self.assertIn("libmp3lame", transcode_command)
            self.assertEqual(transcode_command[-2:], ["mp3", "pipe:1"])
            guard.acquire.assert_called_once_with(blocking=False)
            guard.release.assert_called_once_with()
            process.terminate.assert_not_called()

    @unittest.skipUnless(
        shutil.which("ffprobe") and shutil.which("ffmpeg"),
        "FFmpeg tools are required for the playlist confinement check",
    )
    def test_audio_preview_rejects_disguised_hls_referencing_outside_file(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            allowed = root / "allowed"
            outside = root / "outside"
            allowed.mkdir()
            outside.mkdir()
            outside_audio = outside / "private.mp3"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=0.1",
                    "-codec:a",
                    "libmp3lame",
                    "-y",
                    str(outside_audio),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            disguised = allowed / "disguised.mp4"
            disguised.write_text(
                "#EXTM3U\n"
                "#EXT-X-VERSION:3\n"
                "#EXT-X-TARGETDURATION:1\n"
                "#EXTINF:0.1,\n"
                f"{outside_audio.as_uri()}\n"
                "#EXT-X-ENDLIST\n",
                encoding="utf-8",
            )
            self._login()

            with patch.object(
                recovery_routes,
                "RECOVERY_ALLOWED_BASES",
                (allowed,),
            ):
                response = self.client.get(
                    "/api/recovery/preview-audio",
                    query_string={"filepath": str(disguised)},
                )

            self.assertEqual(response.status_code, 422)
            self.assertIn("could not be inspected", response.get_json()["error"])

    def test_audio_preview_busy_no_audio_and_tool_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            allowed = Path(tmp) / "allowed"
            allowed.mkdir()
            media = allowed / "video.mp4"
            media.write_bytes(b"media")
            self._login()

            busy_guard = Mock()
            busy_guard.acquire.return_value = False
            with (
                patch.object(recovery_routes, "RECOVERY_ALLOWED_BASES", (allowed,)),
                patch.object(recovery_routes, "_AUDIO_PREVIEW_GUARD", busy_guard),
                patch.object(recovery_routes.subprocess, "run") as busy_run,
            ):
                busy = self.client.get(
                    "/api/recovery/preview-audio",
                    query_string={"filepath": str(media)},
                )

            self.assertEqual(busy.status_code, 429)
            self.assertEqual(busy.headers["Retry-After"], "2")
            busy_run.assert_not_called()
            busy_guard.release.assert_not_called()

            no_audio_guard = Mock()
            no_audio_guard.acquire.return_value = True
            with (
                patch.object(recovery_routes, "RECOVERY_ALLOWED_BASES", (allowed,)),
                patch.object(
                    recovery_routes,
                    "_AUDIO_PREVIEW_GUARD",
                    no_audio_guard,
                ),
                patch.object(
                    recovery_routes.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=0, stdout=""),
                ),
                patch.object(recovery_routes.subprocess, "Popen") as no_audio_popen,
            ):
                no_audio = self.client.get(
                    "/api/recovery/preview-audio",
                    query_string={"filepath": str(media)},
                )

            self.assertEqual(no_audio.status_code, 422)
            self.assertIn("no audio", no_audio.get_json()["error"].lower())
            no_audio_popen.assert_not_called()
            no_audio_guard.release.assert_called_once_with()

            unavailable_guard = Mock()
            unavailable_guard.acquire.return_value = True
            with (
                patch.object(recovery_routes, "RECOVERY_ALLOWED_BASES", (allowed,)),
                patch.object(
                    recovery_routes,
                    "_AUDIO_PREVIEW_GUARD",
                    unavailable_guard,
                ),
                patch.object(
                    recovery_routes.subprocess,
                    "run",
                    side_effect=FileNotFoundError("ffprobe"),
                ),
            ):
                unavailable = self.client.get(
                    "/api/recovery/preview-audio",
                    query_string={"filepath": str(media)},
                )

            self.assertEqual(unavailable.status_code, 503)
            self.assertEqual(unavailable.headers["Retry-After"], "5")
            unavailable_guard.release.assert_called_once_with()

    def test_audio_preview_disconnect_terminates_reaps_and_releases_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            allowed = Path(tmp) / "allowed"
            allowed.mkdir()
            media = allowed / "video.mp4"
            media.write_bytes(b"media")
            guard = Mock()
            guard.acquire.return_value = True
            process = Mock()
            process.stdout = io.BytesIO(b"first audio chunk")
            process.poll.return_value = None
            process.wait.return_value = -15
            self._login()

            with (
                patch.object(recovery_routes, "RECOVERY_ALLOWED_BASES", (allowed,)),
                patch.object(recovery_routes, "_AUDIO_PREVIEW_GUARD", guard),
                patch.object(
                    recovery_routes.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=0, stdout="0\n"),
                ),
                patch.object(
                    recovery_routes.subprocess,
                    "Popen",
                    return_value=process,
                ),
            ):
                response = self.client.get(
                    "/api/recovery/preview-audio",
                    query_string={"filepath": str(media)},
                    buffered=False,
                )
                response.close()
                response.close()

            self.assertTrue(process.stdout.closed)
            process.terminate.assert_called_once_with()
            process.wait.assert_called()
            process.kill.assert_not_called()
            guard.release.assert_called_once_with()

    def test_audio_preview_ffmpeg_spawn_failure_releases_slot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            allowed = Path(tmp) / "allowed"
            allowed.mkdir()
            media = allowed / "video.mp4"
            media.write_bytes(b"media")
            guard = Mock()
            guard.acquire.return_value = True
            self._login()

            with (
                patch.object(recovery_routes, "RECOVERY_ALLOWED_BASES", (allowed,)),
                patch.object(recovery_routes, "_AUDIO_PREVIEW_GUARD", guard),
                patch.object(
                    recovery_routes.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=0, stdout="0\n"),
                ),
                patch.object(
                    recovery_routes.subprocess,
                    "Popen",
                    side_effect=FileNotFoundError("ffmpeg"),
                ),
            ):
                response = self.client.get(
                    "/api/recovery/preview-audio",
                    query_string={"filepath": str(media)},
                )

            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.headers["Retry-After"], "5")
            guard.release.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
