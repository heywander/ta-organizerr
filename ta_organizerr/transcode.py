"""Hardware/software transcoding via ffmpeg."""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from .config import HIDDEN_DIR, IMPORT_DIR, SOURCE_DIR, TARGET_DIR
from .db import get_db
from .logging_util import tlog
from .utils import path_within, resolve_media_source

# One ffmpeg job at a time: a second click on the same file would otherwise
# race the first and both would try to rename over the original.
_TRANSCODE_LOCK = threading.Lock()
_ACTIVE: set[str] = set()
_ACTIVE_LOCK = threading.Lock()

# Output container per source extension. The original filename is kept so
# TubeArchivist's index (which stores the media path) stays valid; the
# container has to match the extension for players to trust the file.
_CONTAINER_BY_SUFFIX = {
    ".mp4": "mp4",
    ".mov": "mp4",
    ".mkv": "matroska",
}


def transcode_is_running() -> bool:
    return _TRANSCODE_LOCK.locked()


def active_jobs() -> list[str]:
    with _ACTIVE_LOCK:
        return sorted(_ACTIVE)


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


def output_container(filepath: str | Path) -> str:
    """Container format ffmpeg should write for ``filepath``'s extension.

    ``.webm``/``.avi`` cannot hold H.264+AAC sensibly, so they fall back to
    an MP4 stream inside the original name; the caller logs that case.
    """
    return _CONTAINER_BY_SUFFIX.get(Path(filepath).suffix.lower(), "mp4")


def _build_cmd(filepath: str, temp_file: str, video_codec: str | None, encoder: str) -> list[str]:
    base = ["ffmpeg", "-v", "error", "-stats"]
    container = output_container(filepath)
    audio_args = ["-c:a", "aac", "-b:a", "192k"]
    if container == "mp4":
        audio_args += ["-movflags", "+faststart"]
    audio_args += ["-f", container, "-y", temp_file]
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


def _record_codecs(filepath: str, video_codec: str | None, audio_codec: str | None) -> None:
    """Persist probed codecs so the transcode queue can be built without re-probing."""
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE videos SET video_codec = ?, audio_codec = ? WHERE source_path = ?",
                (video_codec, audio_codec, filepath),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 — bookkeeping must never break a transcode
        tlog(f"⚠️ Could not record codecs for {filepath}: {exc}")


def scan_codecs(limit: int | None = None) -> int:
    """Probe linked videos that have no codec info yet and store the result.

    Returns the number of rows probed. Runs in a background thread from the
    API; each probe is a quick ffprobe header read.
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT source_path FROM videos "
            "WHERE status = 'linked' AND source_path IS NOT NULL AND video_codec IS NULL"
            + (f" LIMIT {int(limit)}" if limit else "")
        ).fetchall()
    tlog(f"🔎 Probing codecs for {len(rows)} videos...")
    probed = 0
    for row in rows:
        source = row["source_path"]
        if not Path(source).exists():
            continue
        video_codec, audio_codec = probe_codecs(source)
        if video_codec is None and audio_codec is None:
            continue
        _record_codecs(source, video_codec, audio_codec)
        probed += 1
    tlog(f"✅ Codec scan complete. Probed {probed} videos.")
    return probed


def transcode_video(filepath: str, encoder: str = "libx264") -> bool:
    with _ACTIVE_LOCK:
        if filepath in _ACTIVE:
            tlog(f"⏳ Already queued: {filepath}")
            return False
        _ACTIVE.add(filepath)
    try:
        with _TRANSCODE_LOCK:
            return _transcode_video_locked(filepath, encoder)
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.discard(filepath)


def _transcode_video_locked(filepath: str, encoder: str) -> bool:
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
    _record_codecs(filepath, video_codec, audio_codec)
    if video_codec == "h264" and audio_codec == "aac":
        tlog(f"Already H.264/AAC: {filepath}")
        return True

    container = output_container(filepath)
    if container == "mp4" and resolved_path.suffix.lower() not in (".mp4", ".mov"):
        tlog(
            f"⚠️ {resolved_path.suffix} cannot carry H.264/AAC; writing an MP4 stream "
            f"under the original name so TubeArchivist's index stays valid."
        )
    # Keep the real suffix on the temp file so ffmpeg and any sniffing tools agree.
    temp_file = f"{filepath}.temp{resolved_path.suffix}"
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
            Path(temp_file).replace(filepath)
            _record_codecs(filepath, "h264", "aac")
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
                Path(temp_file).replace(filepath)
                _record_codecs(filepath, "h264", "aac")
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
