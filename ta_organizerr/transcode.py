"""Hardware/software transcoding via ffmpeg."""
from __future__ import annotations

import subprocess
from pathlib import Path

from .config import HIDDEN_DIR, IMPORT_DIR, SOURCE_DIR, TARGET_DIR
from .logging_util import tlog
from .utils import path_within, resolve_media_source


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
    resolved_path = resolve_media_source(filepath)
    allowed_bases = (SOURCE_DIR, TARGET_DIR, HIDDEN_DIR, IMPORT_DIR)
    if not resolved_path or not path_within(resolved_path, allowed_bases):
        tlog(f"Rejected path outside allowed directories: {filepath}")
        return False
    if Path(filepath) != resolved_path:
        tlog(f"Resolved media path: {filepath} -> {resolved_path}")
    filepath = str(resolved_path)
    if not resolved_path.exists():
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
