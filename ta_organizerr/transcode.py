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
from .db import get_db
from .logging_util import tlog
from .library_lock import LIBRARY_MUTATION_LOCK
from .media_reservations import (
    MediaReservation,
    MediaReservationConflict,
    extend_media_reservation,
    media_identity,
    media_mutation_guard,
    release_media,
    reserve_media,
)
from .utils import path_within, resolve_media_source


_ALLOWED_BASES = (SOURCE_DIR, TARGET_DIR, HIDDEN_DIR, IMPORT_DIR)
_SUPPORTED_CONTAINERS = {".mp4", ".mkv", ".mov"}
_active_transcodes: dict[Path, MediaReservation] = {}
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


def summarize_codecs(streams: list[MediaStream]) -> tuple[str, str]:
    """Collapse a stream inventory to the (video, audio) pair the queue tracks.

    Mirrors the decision in :func:`_transcode_resolved`: a file is done when
    its single primary video stream is H.264 and every audio stream (possibly
    none) is AAC. ``none``/``multiple`` keep oddly shaped files visible in the
    queue instead of hiding them behind an unknown-codec placeholder.
    """
    primary = [s for s in streams if s.stream_type == "video" and not s.attached_pic]
    audio = [s.codec for s in streams if s.stream_type == "audio"]
    if not primary:
        video_codec = "none"
    elif len(primary) > 1:
        video_codec = "multiple"
    else:
        video_codec = primary[0].codec or "unknown"
    if not audio:
        audio_codec = "none"
    elif all(codec == "aac" for codec in audio):
        audio_codec = "aac"
    else:
        audio_codec = next(codec for codec in audio if codec != "aac") or "unknown"
    return video_codec, audio_codec


def needs_transcode(video_codec: str | None, audio_codec: str | None) -> bool:
    return video_codec != "h264" or audio_codec not in {"aac", "none"}


# SQL fragment matching rows :func:`needs_transcode` would flag, including
# rows that have never been probed. Shared with the transcode routes.
NEEDS_TRANSCODE_SQL = (
    "(video_codec IS NULL OR video_codec != 'h264' "
    "OR audio_codec IS NULL OR audio_codec NOT IN ('aac', 'none'))"
)


def _record_codecs(where: str, key: str | None, video_codec: str, audio_codec: str) -> None:
    """Persist probed codecs so the queue can be built without re-probing."""
    if not key:
        return
    try:
        with get_db() as conn:
            conn.execute(
                f"UPDATE videos SET video_codec = ?, audio_codec = ? WHERE {where} = ?",
                (video_codec, audio_codec, key),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 — bookkeeping must never break a transcode
        tlog(f"⚠️ Could not record codecs for {key}: {exc}")


def scan_codecs(limit: int | None = None) -> int:
    """Probe linked videos with no codec info yet and store the result.

    Returns the number of rows probed. Each probe is a quick ffprobe header
    read; nothing is reserved or modified, so it is safe to run while
    transcodes or scans are active.
    """
    query = (
        "SELECT video_id, symlink FROM videos "
        "WHERE status = 'linked' AND symlink IS NOT NULL AND video_codec IS NULL "
        "ORDER BY channel, published DESC"
    )
    if limit:
        query += f" LIMIT {int(limit)}"
    with get_db() as conn:
        rows = conn.execute(query).fetchall()
    tlog(f"🔎 Probing codecs for {len(rows)} videos...")
    probed = 0
    for row in rows:
        source, _error = _resolve_source(row["symlink"])
        if source is None:
            continue
        streams = probe_streams(str(source))
        if streams is None:
            continue
        video_codec, audio_codec = summarize_codecs(streams)
        _record_codecs("video_id", row["video_id"], video_codec, audio_codec)
        probed += 1
    tlog(f"✅ Codec scan complete. Probed {probed} videos.")
    return probed


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
    # Reservation establishment follows the global lock order: library,
    # transcode bookkeeping, then the shared media registry.  The library
    # lock is released before FFmpeg starts.
    with LIBRARY_MUTATION_LOCK, _active_transcodes_lock:
        try:
            source_stat = source.stat()
        except OSError as exc:
            return False, f"Source file became unavailable: {exc}"
        if source in _active_transcodes:
            return False, "A transcode is already running for this file"
        if len(_active_transcodes) >= MAX_CONCURRENT_TRANSCODES:
            return False, "The transcode worker is busy; try again later"
        try:
            reservation = reserve_media(
                (media_identity(source_stat),),
                paths=(source,),
                owner=f"transcode job for {source.name}",
            )
        except MediaReservationConflict as exc:
            return False, str(exc)
        _active_transcodes[source] = reservation
    return True, "Transcode started"


def _release(source: Path) -> None:
    with LIBRARY_MUTATION_LOCK:
        with _active_transcodes_lock:
            reservation = _active_transcodes.pop(source, None)
        release_media(reservation)


def delete_source_if_idle(source: Path) -> None:
    """Delete a source entry atomically with respect to transcode reservation."""
    canonical = source.resolve(strict=True)
    try:
        with media_mutation_guard(
            paths=(source, canonical),
            action="delete this source file",
        ):
            source.unlink()
    except MediaReservationConflict as exc:
        raise OSError(
            f"Source is currently being transcoded or used by media salvage: {source}"
        ) from exc


def _temporary_output(source: Path) -> Path:
    """Create a unique same-directory output with the original container suffix."""
    with LIBRARY_MUTATION_LOCK:
        fd, name = tempfile.mkstemp(
            prefix=f".{source.stem}.transcoding-",
            suffix=source.suffix,
            dir=source.parent,
        )
        os.close(fd)
        output = Path(name)
        try:
            with _active_transcodes_lock:
                reservation = _active_transcodes.get(source)
            if reservation is not None:
                extend_media_reservation(
                    reservation,
                    (media_identity(output.stat()),),
                    paths=(output,),
                )
            return output
        except Exception:
            output.unlink(missing_ok=True)
            raise


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
                own_reservation = _active_transcodes.get(source)
            with media_mutation_guard(
                paths=(source,),
                except_reservation=own_reservation,
                action="publish this transcoded replacement",
            ):
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


def _commit_and_record(
    temp_file: Path,
    source: Path,
    original_stat: os.stat_result,
    requested_path: str | None,
) -> bool:
    committed = _commit_output(temp_file, source, original_stat)
    if committed:
        _record_codecs("symlink", requested_path, "h264", "aac")
    return committed


def _transcode_resolved(
    source: Path,
    encoder: str,
    requested_path: str | None = None,
) -> bool:
    filepath = str(source)
    streams = probe_streams(filepath)
    if streams is None:
        tlog(f"❌ Refusing transcode because stream inventory failed: {filepath}")
        return False
    _record_codecs("symlink", requested_path, *summarize_codecs(streams))
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
            return _commit_and_record(temp_file, source, original_stat, requested_path)

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
                return _commit_and_record(temp_file, source, original_stat, requested_path)
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
        return _transcode_resolved(source, encoder, requested_path=filepath)
    finally:
        _release(source)


def _run_reserved(source: Path, encoder: str, requested_path: str | None = None) -> None:
    try:
        _transcode_resolved(source, encoder, requested_path=requested_path)
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
            args=(source, encoder, filepath),
            daemon=True,
            name=f"transcode-{source.name}",
        )
        thread.start()
    except RuntimeError as exc:
        _release(source)
        return False, f"Could not start transcode worker: {exc}"
    return True, message
