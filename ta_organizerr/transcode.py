"""Hardware/software transcoding via ffmpeg."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .logging_util import tlog


def detect_encoder() -> str:
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError) as exc:
        tlog(f"ffmpeg not available, falling back to libx264: {exc}")
        return "libx264"
    encoders = result.stdout
    if "h264_nvenc" in encoders:
        return "h264_nvenc"
    if "h264_vaapi" in encoders:
        return "h264_vaapi"
    if "h264_videotoolbox" in encoders:
        return "h264_videotoolbox"
    return "libx264"


def probe_codecs(filepath: str) -> tuple[str | None, str | None]:
    def _probe(stream: str) -> str | None:
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-select_streams", stream,
                    "-show_entries", "stream=codec_name", "-of", "csv=p=0", filepath,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        except (FileNotFoundError, OSError) as exc:
            tlog(f"ffprobe failure on {filepath}: {exc}")
            return None
        return result.stdout.strip() or None

    return _probe("v:0"), _probe("a:0")


def _resolve_symlink_to_container(filepath: str) -> str | None:
    original = Path(filepath)
    if original.is_symlink():
        try:
            actual = Path(os.readlink(original)).resolve()
        except OSError as exc:
            tlog(f"Error resolving symlink: {exc}")
            return None
        tlog(f"Following symlink: {filepath} -> {actual}")
        actual_str = str(actual)
        if actual_str.startswith("/mnt/user/tubearchives/bp"):
            container = actual_str.replace("/mnt/user/tubearchives/bp", "/app/source", 1)
            tlog(f"Translated path: {actual} -> {container}")
            return container
        return actual_str
    if not original.exists():
        tlog(f"File not found: {filepath}")
        return None
    return filepath


def _build_cmd(filepath: str, temp_file: str, video_codec: str | None, encoder: str) -> list[str]:
    base = ["ffmpeg", "-v", "error", "-stats"]
    audio_args = ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-y", temp_file]
    if video_codec == "h264":
        return [*base, "-i", filepath, "-c:v", "copy", *audio_args]
    if encoder == "h264_nvenc":
        return [
            *base, "-i", filepath,
            "-c:v", "h264_nvenc", "-preset", "fast", "-cq", "23",
            *audio_args,
        ]
    if encoder == "h264_vaapi":
        return [
            *base,
            "-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi",
            "-i", filepath,
            "-vf", "format=nv12,hwupload",
            "-c:v", "h264_vaapi", "-b:v", "5M",
            *audio_args,
        ]
    return [*base, "-i", filepath, "-c:v", "libx264", "-crf", "23", "-preset", "medium", *audio_args]


def transcode_video(filepath: str, encoder: str = "libx264") -> bool:
    resolved = _resolve_symlink_to_container(filepath)
    if not resolved:
        return False
    filepath = resolved
    if not Path(filepath).exists():
        tlog(f"Source file not found: {filepath}")
        return False

    video_codec, audio_codec = probe_codecs(filepath)
    if video_codec == "h264" and audio_codec == "aac":
        tlog(f"Already H.264/AAC: {filepath}")
        return True

    temp_file = f"{filepath}.temp.mp4"
    try:
        cmd = _build_cmd(filepath, temp_file, video_codec, encoder)
        if encoder.startswith("h264_") and encoder != "libx264":
            tlog(f"Full transcode using {encoder}: {filepath}")
        elif video_codec == "h264":
            tlog(f"Audio-only transcode: {filepath}")
        else:
            tlog(f"Full transcode using libx264: {filepath}")
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            Path(filepath).unlink()
            Path(temp_file).rename(filepath)
            tlog(f"✅ Success: {filepath}")
            return True

        gpu_failed = encoder in {"h264_nvenc", "h264_vaapi", "h264_videotoolbox"} and (
            "libcuda" in result.stderr or "Cannot load" in result.stderr
        )
        if gpu_failed:
            tlog("⚠️ GPU encoding failed, retrying with CPU (libx264)...")
            cpu_cmd = _build_cmd(filepath, temp_file, video_codec, "libx264")
            cpu_result = subprocess.run(cpu_cmd, capture_output=True, text=True, check=False)
            if cpu_result.returncode == 0:
                Path(filepath).unlink()
                Path(temp_file).rename(filepath)
                tlog(f"✅ Success (CPU): {filepath}")
                return True
            tlog(f"❌ Failed (CPU): {filepath}\nError: {cpu_result.stderr}")
        else:
            tlog(f"❌ Failed: {filepath}\nError: {result.stderr}")
    except (OSError, subprocess.SubprocessError) as exc:
        tlog(f"❌ Exception: {exc}")
    finally:
        tmp = Path(temp_file)
        if tmp.exists() and Path(filepath).exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return False
