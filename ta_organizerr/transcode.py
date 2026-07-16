"""Hardware/software transcoding via ffmpeg."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from .config import (
    HIDDEN_DIR,
    IMPORT_DIR,
    MAX_CONCURRENT_TRANSCODES,
    SOURCE_DIR,
    TARGET_DIR,
)
from .logging_util import tlog
from .library_lock import LIBRARY_MUTATION_LOCK
from .utils import path_within, resolve_media_source


_ALLOWED_BASES = (SOURCE_DIR, TARGET_DIR, HIDDEN_DIR, IMPORT_DIR)
_SUPPORTED_CONTAINERS = {".mp4", ".mkv", ".mov"}
_active_transcodes: dict[Path, tuple[int, int]] = {}
_active_transcodes_lock = threading.Lock()


@dataclass(frozen=True)
class MediaStream:
    stream_type: str
    codec: str | None
    attached_pic: bool = False


def detect_encoder() -> str:
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
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


def probe_streams(filepath: str) -> list[MediaStream] | None:
    """Return every stream's type/codec, or ``None`` when probing is unsafe."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries",
                "stream=codec_type,codec_name:stream_disposition=attached_pic",
                "-of", "json", filepath,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if result.returncode != 0:
            tlog(f"ffprobe failed on {filepath}: {(result.stderr or '').strip()}")
            return None
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise ValueError("ffprobe response was not an object")
        streams = payload.get("streams")
        if not isinstance(streams, list):
            raise ValueError("missing streams list")
        parsed: list[MediaStream] = []
        for stream in streams:
            if not isinstance(stream, dict) or not isinstance(stream.get("codec_type"), str):
                raise ValueError("invalid stream entry")
            codec = stream.get("codec_name")
            disposition = stream.get("disposition")
            attached_pic = bool(
                isinstance(disposition, dict) and disposition.get("attached_pic") == 1
            )
            parsed.append(MediaStream(
                stream["codec_type"],
                codec if isinstance(codec, str) else None,
                attached_pic,
            ))
        return parsed
    except (
        FileNotFoundError,
        OSError,
        ValueError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as exc:
        tlog(f"ffprobe failure on {filepath}: {exc}")
        return None


def _build_cmd(
    filepath: str,
    temp_file: str,
    video_codec: str | None,
    encoder: str,
    video_stream_index: int = 0,
) -> list[str]:
    # Start by copying every stream, metadata block, and chapter. Video/audio
    # overrides below then affect all streams of those types. If the output
    # container cannot represent an ancillary stream, ffmpeg fails and the
    # original remains untouched instead of silently dropping user data.
    prefix = ["ffmpeg", "-v", "error", "-stats"]
    mapping = [
        "-map", "0", "-map_metadata", "0", "-map_chapters", "0", "-c", "copy",
    ]
    base = [*prefix, "-i", filepath, *mapping]
    audio_args = ["-c:a", "aac", "-b:a", "192k"]
    if Path(temp_file).suffix.lower() in {".mp4", ".mov", ".m4v"}:
        audio_args.extend(["-movflags", "+faststart"])
    audio_args.extend(["-y", temp_file])
    video_codec_option = f"-c:v:{video_stream_index}"
    if video_codec == "h264":
        return [*base, video_codec_option, "copy", *audio_args]
    if encoder == "h264_nvenc":
        return [
            *base,
            video_codec_option, "h264_nvenc", "-preset", "fast", "-cq", "23",
            *audio_args,
        ]
    if encoder == "h264_vaapi":
        return [
            *prefix,
            "-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi",
            "-i", filepath,
            *mapping,
            f"-filter:v:{video_stream_index}", "format=nv12,hwupload",
            video_codec_option, "h264_vaapi", "-b:v", "5M",
            *audio_args,
        ]
    if encoder == "h264_videotoolbox":
        return [
            *base,
            video_codec_option, "h264_videotoolbox", "-b:v", "5M",
            *audio_args,
        ]
    return [
        *base,
        video_codec_option, "libx264", "-crf", "23", "-preset", "medium",
        *audio_args,
    ]


def _resolve_source(filepath: str) -> tuple[Path | None, str | None]:
    resolved_path = resolve_media_source(filepath)
    if not resolved_path or not path_within(resolved_path, _ALLOWED_BASES):
        return None, "Path is outside allowed media directories"
    if not resolved_path.exists() or not resolved_path.is_file():
        return None, "Source file does not exist"
    try:
        source = resolved_path.resolve(strict=True)
    except OSError as exc:
        return None, f"Could not resolve source file: {exc}"
    if source.suffix.lower() not in _SUPPORTED_CONTAINERS:
        supported = ", ".join(sorted(_SUPPORTED_CONTAINERS))
        return None, (
            f"Container {source.suffix or '(none)'} cannot safely hold H.264/AAC; "
            f"supported containers: {supported}"
        )
    return source, None


def _reserve(source: Path) -> tuple[bool, str]:
    with _active_transcodes_lock:
        try:
            source_stat = source.stat()
        except OSError as exc:
            return False, f"Source file became unavailable: {exc}"
        identity = (source_stat.st_dev, source_stat.st_ino)
        if source in _active_transcodes or identity in _active_transcodes.values():
            return False, "A transcode is already running for this file"
        if len(_active_transcodes) >= MAX_CONCURRENT_TRANSCODES:
            return False, "The transcode worker is busy; try again later"
        _active_transcodes[source] = identity
    return True, "Transcode started"


def _release(source: Path) -> None:
    with _active_transcodes_lock:
        _active_transcodes.pop(source, None)


def delete_source_if_idle(source: Path) -> None:
    """Delete a source entry atomically with respect to transcode reservation."""
    # Global ordering for mutating paths is library lock, then transcode lock.
    with LIBRARY_MUTATION_LOCK:
        canonical = source.resolve(strict=True)
        source_stat = source.stat()
        identity = (source_stat.st_dev, source_stat.st_ino)
        with _active_transcodes_lock:
            if canonical in _active_transcodes or identity in _active_transcodes.values():
                raise OSError(f"Source is currently being transcoded: {source}")
            source.unlink()


def _temporary_output(source: Path) -> Path:
    """Create a unique same-directory output with the original container suffix."""
    fd, name = tempfile.mkstemp(
        prefix=f".{source.stem}.transcoding-",
        suffix=source.suffix,
        dir=source.parent,
    )
    os.close(fd)
    return Path(name)


def _commit_output(
    temp_file: Path,
    source: Path,
    original_stat: os.stat_result,
) -> bool:
    try:
        if not temp_file.exists() or temp_file.stat().st_size == 0:
            tlog(f"❌ ffmpeg produced no output for: {source}")
            return False
        temp_file.chmod(stat.S_IMODE(original_stat.st_mode))
        with LIBRARY_MUTATION_LOCK:
            with _active_transcodes_lock:
                current_stat = source.stat()
                original_identity = (
                    original_stat.st_dev,
                    original_stat.st_ino,
                    original_stat.st_size,
                    original_stat.st_mtime_ns,
                )
                current_identity = (
                    current_stat.st_dev,
                    current_stat.st_ino,
                    current_stat.st_size,
                    current_stat.st_mtime_ns,
                )
                if current_identity != original_identity:
                    tlog(f"❌ Source changed during transcode; refusing replacement: {source}")
                    return False
                os.replace(temp_file, source)
    except OSError as exc:
        tlog(f"❌ Could not atomically replace {source}: {exc}")
        return False
    tlog(f"✅ Success: {source}")
    return True


def _transcode_resolved(source: Path, encoder: str) -> bool:
    filepath = str(source)
    streams = probe_streams(filepath)
    if streams is None:
        tlog(f"❌ Refusing transcode because stream inventory failed: {filepath}")
        return False
    video_streams = [stream for stream in streams if stream.stream_type == "video"]
    primary_videos = [stream for stream in video_streams if not stream.attached_pic]
    audio_codecs = [
        stream.codec for stream in streams if stream.stream_type == "audio"
    ]
    if len(primary_videos) != 1:
        tlog(
            "❌ Refusing transcode unless exactly one non-thumbnail video "
            f"stream is present: {filepath}"
        )
        return False
    primary_video = primary_videos[0]
    primary_video_index = video_streams.index(primary_video)
    video_codec = "h264" if primary_video.codec == "h264" else None
    if video_codec == "h264" and all(codec == "aac" for codec in audio_codecs):
        tlog(f"Already H.264/AAC: {filepath}")
        return True

    try:
        original_stat = source.stat()
        temp_file = _temporary_output(source)
    except OSError as exc:
        tlog(f"❌ Could not create transcode output beside {source}: {exc}")
        return False

    try:
        cmd = _build_cmd(
            filepath,
            str(temp_file),
            video_codec,
            encoder,
            primary_video_index,
        )
        if video_codec == "h264":
            tlog(f"Audio-only transcode: {filepath}")
        elif encoder != "libx264":
            tlog(f"Full transcode using {encoder}: {filepath}")
        else:
            tlog(f"Full transcode using libx264: {filepath}")

        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return _commit_output(temp_file, source, original_stat)

        hardware_encoder = encoder in {
            "h264_nvenc",
            "h264_vaapi",
            "h264_videotoolbox",
        }
        if hardware_encoder:
            tlog(f"⚠️ {encoder} failed, retrying with CPU (libx264)...")
            cpu_cmd = _build_cmd(
                filepath,
                str(temp_file),
                video_codec,
                "libx264",
                primary_video_index,
            )
            cpu_result = subprocess.run(
                cpu_cmd,
                capture_output=True,
                text=True,
                check=False,
            )
            if cpu_result.returncode == 0:
                return _commit_output(temp_file, source, original_stat)
            tlog(f"❌ Failed (CPU): {filepath}\nError: {cpu_result.stderr}")
        else:
            tlog(f"❌ Failed: {filepath}\nError: {result.stderr}")
    except (OSError, subprocess.SubprocessError) as exc:
        tlog(f"❌ Exception: {exc}")
    finally:
        try:
            temp_file.unlink(missing_ok=True)
        except OSError:
            pass
    return False


def transcode_video(filepath: str, encoder: str = "libx264") -> bool:
    """Synchronously transcode one file, refusing duplicate/concurrent work."""
    source, error = _resolve_source(filepath)
    if not source:
        tlog(f"Rejected transcode request for {filepath}: {error}")
        return False
    if Path(filepath) != source:
        tlog(f"Resolved media path: {filepath} -> {source}")

    reserved, message = _reserve(source)
    if not reserved:
        tlog(message)
        return False
    try:
        return _transcode_resolved(source, encoder)
    finally:
        _release(source)


def _run_reserved(source: Path, encoder: str) -> None:
    try:
        _transcode_resolved(source, encoder)
    finally:
        _release(source)


def start_transcode_job(filepath: str, encoder: str = "libx264") -> tuple[bool, str]:
    """Start one bounded background job after atomically reserving its source."""
    source, error = _resolve_source(filepath)
    if not source:
        return False, error or "Invalid source file"

    reserved, message = _reserve(source)
    if not reserved:
        return False, message
    try:
        thread = threading.Thread(
            target=_run_reserved,
            args=(source, encoder),
            daemon=True,
            name=f"transcode-{source.name}",
        )
        thread.start()
    except RuntimeError as exc:
        _release(source)
        return False, f"Could not start transcode worker: {exc}"
    return True, message
