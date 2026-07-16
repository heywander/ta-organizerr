"""Lost-media recovery: scanning, yt-dlp metadata fetching, force import."""
from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .config import HIDDEN_DIR, IMPORT_DIR, SOURCE_DIR, TARGET_DIR
from .db import get_db
from .logging_util import log
from .media_reservations import MediaReservationConflict, media_mutation_guard
from .ta_api import fetch_all_metadata
from .utils import (
    extract_id_from_filename,
    is_valid_youtube_id,
    is_video,
    path_in_allowed_tree,
    path_within,
    translated_symlink_target,
    translate_ta_path,
)


# Allowed bases for any user-supplied filepath operation. These are the only
# directories the API is permitted to read/write under, regardless of what the
# client asks for. Resolution must end inside one of these.
RECOVERY_ALLOWED_BASES = (SOURCE_DIR, TARGET_DIR, HIDDEN_DIR, IMPORT_DIR)
_MAX_DELETE_BATCH = 500
_MAX_PATH_LENGTH = 4096


def _iter_video_files(root: Path):
    if not root.exists():
        return
    for path in root.rglob("*"):
        if path.is_file() and is_video(path):
            yield path


def _same_directory_entry(source: Path, destination: Path) -> bool:
    """Compare paths without following either final directory entry."""
    try:
        source_entry = source.parent.resolve(strict=False) / source.name
        destination_entry = destination.parent.resolve(strict=False) / destination.name
    except (OSError, RuntimeError):
        return False
    return source_entry == destination_entry


def _copy_to_unique_temp(source: Path, destination: Path) -> Path:
    fd, temp_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=".stage-",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    fd_open = True
    try:
        with os.fdopen(fd, "wb") as destination_fh:
            fd_open = False
            with source.open("rb") as source_fh:
                shutil.copyfileobj(source_fh, destination_fh)
            destination_fh.flush()
            os.fsync(destination_fh.fileno())
        shutil.copystat(source, temp_path)
        return temp_path
    except Exception:
        if fd_open:
            os.close(fd)
        temp_path.unlink(missing_ok=True)
        raise


def _prepare_media_stage(source: Path, destination: Path) -> tuple[Path | None, str]:
    """Prepare private media beside its destination without publishing it."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if _same_directory_entry(source, destination):
        return None, "already staged"

    canonical_source = source.resolve(strict=True)
    if not canonical_source.is_file():
        raise OSError(f"Source is not a regular file: {source}")

    for _ in range(10):
        temp_path = destination.parent / f".stage-{secrets.token_hex(12)}.tmp"
        try:
            # Creating a hard link changes the source inode's ctime.  Do not
            # invalidate a concurrent salvage/transcode integrity check; a
            # descriptor-based copy is non-mutating and remains safe if the
            # source pathname is atomically replaced afterward.
            with media_mutation_guard(
                paths=(canonical_source,),
                action="hard-link this media while it is being processed",
            ):
                os.link(canonical_source, temp_path)
            return temp_path, "hardlinked"
        except FileExistsError:
            continue
        except MediaReservationConflict:
            return _copy_to_unique_temp(canonical_source, destination), "copied"
        except OSError:
            return _copy_to_unique_temp(canonical_source, destination), "copied"
    raise OSError("Could not allocate a unique media staging path")


def _write_json_temp(destination: Path, payload: dict[str, Any]) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=".metadata-",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    fd_open = True
    completed = False
    try:
        mode = 0o644
        try:
            if destination.exists() and not destination.is_symlink():
                mode = stat.S_IMODE(destination.stat().st_mode)
        except OSError:
            pass
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd_open = False
            json.dump(payload, fh, indent=4)
            fh.flush()
            os.fsync(fh.fileno())
        completed = True
        return temp_path
    finally:
        if fd_open:
            os.close(fd)
        if not completed:
            temp_path.unlink(missing_ok=True)


def _publish_import_pair(
    metadata_temp: Path,
    metadata_destination: Path,
    media_temp: Path | None,
    media_destination: Path,
) -> None:
    """Publish metadata first and media last, rolling metadata back on error."""
    protected_paths = (media_destination,) if media_temp is not None else ()
    backup: Path | None = None
    metadata_published = False
    try:
        with media_mutation_guard(
            paths=protected_paths,
            action="replace this imported media file",
        ):
            try:
                if os.path.lexists(metadata_destination):
                    if metadata_destination.is_dir() and not metadata_destination.is_symlink():
                        raise OSError(f"Metadata destination is a directory: {metadata_destination}")
                    backup = metadata_destination.parent / (
                        f".metadata-backup-{secrets.token_hex(12)}.tmp"
                    )
                    os.replace(metadata_destination, backup)
                os.replace(metadata_temp, metadata_destination)
                metadata_published = True
                if media_temp is not None:
                    os.replace(media_temp, media_destination)
                if backup is not None:
                    try:
                        backup.unlink(missing_ok=True)
                    except OSError as exc:
                        log(f"⚠️ Could not remove old metadata backup {backup}: {exc}")
            except OSError:
                if metadata_published:
                    try:
                        metadata_destination.unlink(missing_ok=True)
                    except OSError:
                        pass
                if backup is not None and os.path.lexists(backup):
                    try:
                        os.replace(backup, metadata_destination)
                    except OSError as restore_exc:
                        log(f"❌ Could not restore metadata after import failure: {restore_exc}")
                raise
    finally:
        for staged_path in (metadata_temp, media_temp, backup):
            if staged_path is None:
                continue
            try:
                staged_path.unlink(missing_ok=True)
            except OSError:
                pass


def _record_lost_media(video_id: str, source: Path) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO lost_media (video_id, filepath) VALUES (?, ?)",
            (video_id, str(source)),
        )
        conn.commit()


def scan_for_unindexed_videos() -> dict[str, list[dict[str, Any]]]:
    log("🔍 Scanning for unindexed and legacy files...")
    video_map = fetch_all_metadata()
    known_ids = set(video_map.keys())

    with get_db() as conn:
        lost_rows = conn.execute("SELECT video_id FROM lost_media").fetchall()
    lost_ids = {row["video_id"] for row in lost_rows}

    results: dict[str, list[dict[str, Any]]] = {
        "unindexed": [],
        "redundant": [],
        "rescue": [],
        "lost": [],
    }

    for video_file in _iter_video_files(SOURCE_DIR):
        vid_id = extract_id_from_filename(video_file.name)
        if not vid_id or vid_id in known_ids:
            continue
        info = {
            "path": str(video_file),
            "filename": video_file.name,
            "video_id": vid_id,
            "size_mb": round(video_file.stat().st_size / (1024 * 1024), 2),
            "ta_source": "Source Dir",
            "requires_destruct": True,
        }
        bucket = "lost" if vid_id in lost_ids else "unindexed"
        results[bucket].append(info)

    for root, source_label in ((TARGET_DIR, "Target Dir"), (HIDDEN_DIR, "Hidden Dir")):
        for video_file in _iter_video_files(root):
            if video_file.is_symlink():
                continue
            vid_id = extract_id_from_filename(video_file.name)
            if not vid_id:
                continue
            size_mb = round(video_file.stat().st_size / (1024 * 1024), 2)
            if vid_id not in known_ids:
                results["unindexed"].append({
                    "path": str(video_file),
                    "filename": video_file.name,
                    "video_id": vid_id,
                    "type": "target_realfile",
                    "ta_source": source_label,
                    "size_mb": size_mb,
                    "requires_destruct": False,
                })
                continue

            raw_source = video_map[vid_id].get("filesystem_path")
            ta_source_path = translate_ta_path(raw_source) if raw_source else None
            bucket = "redundant" if (ta_source_path and ta_source_path.exists()) else "rescue"
            results[bucket].append({
                "path": str(video_file),
                "filename": video_file.name,
                "video_id": vid_id,
                "ta_source": str(ta_source_path) if ta_source_path else None,
                "size_mb": size_mb,
                "requires_destruct": False,
            })

    log(
        f"✅ Scan complete. Unindexed: {len(results['unindexed'])}, "
        f"Redundant: {len(results['redundant'])}, Rescue: {len(results['rescue'])}"
    )
    return results


def recover_video_metadata(filepath: str) -> tuple[bool, str]:
    src_path = Path(filepath)
    if not src_path.exists() or not src_path.is_file():
        return False, "File not found"

    vid_id = extract_id_from_filename(src_path.name)
    if not vid_id or not is_valid_youtube_id(vid_id):
        return False, "Could not extract valid Video ID from filename"

    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    dest_video = IMPORT_DIR / src_path.name
    dest_json = IMPORT_DIR / f"{src_path.stem}.info.json"
    output_template = IMPORT_DIR / f".recovery-{secrets.token_hex(12)}"
    temp_json = Path(f"{output_template}.info.json")

    log(f"🚑 Recovering: {vid_id} ...")
    cmd = [
        "yt-dlp",
        "--write-info-json",
        "--skip-download",
        "--id",
        f"https://www.youtube.com/watch?v={vid_id}",
        "-o",
        str(output_template),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        temp_json.unlink(missing_ok=True)
        log(f"   ❌ Recovery failed: {exc}")
        return False, str(exc)

    if result.returncode != 0:
        temp_json.unlink(missing_ok=True)
        error_msg = (result.stderr or "").strip() or "Unknown Error"
        log(f"   ⚠️ yt-dlp failed (Exit Code {result.returncode}). Error: {error_msg}")
        _record_lost_media(vid_id, src_path)
        return False, f"yt-dlp failed: {error_msg} (Added to Lost Media)"
    if not temp_json.is_file() or temp_json.stat().st_size == 0:
        temp_json.unlink(missing_ok=True)
        error_msg = "yt-dlp completed without producing metadata"
        log(f"   ⚠️ {error_msg} for {vid_id}")
        _record_lost_media(vid_id, src_path)
        return False, f"{error_msg} (Added to Lost Media)"

    log(f"   ✅ Metadata fetched (exit {result.returncode}).")

    media_temp: Path | None = None
    try:
        media_temp, method = _prepare_media_stage(src_path, dest_video)
        _publish_import_pair(temp_json, dest_json, media_temp, dest_video)
        log(f"   📂 Media {method} atomically.")
    except MediaReservationConflict:
        raise
    except OSError as exc:
        return False, f"Failed to stage recovery: {exc}"
    finally:
        temp_json.unlink(missing_ok=True)

    return True, "Ready for import"


def force_import(filepath: str) -> tuple[bool, str]:
    src_path = Path(filepath).resolve()
    if not src_path.exists() or not src_path.is_file():
        return False, "File not found"
    vid_id = extract_id_from_filename(src_path.name)
    if not vid_id or not is_valid_youtube_id(vid_id):
        return False, "Could not extract valid ID"

    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = IMPORT_DIR / f"{src_path.stem}.info.json"
    offline_meta = {
        "id": vid_id,
        "title": f"Offline Import - {src_path.stem}",
        "uploader": "Unknown (Lost Media)",
        "upload_date": datetime.now().strftime("%Y%m%d"),
        "description": "Imported via TubeSorterr Force Import (Lost Media)",
        "webpage_url": f"https://www.youtube.com/watch?v={vid_id}",
        "view_count": 0,
        "like_count": 0,
        "duration": 0,
    }
    media_temp: Path | None = None
    metadata_temp: Path | None = None
    try:
        dest_video = IMPORT_DIR / src_path.name
        media_temp, method = _prepare_media_stage(src_path, dest_video)
        metadata_temp = _write_json_temp(json_path, offline_meta)
        _publish_import_pair(metadata_temp, json_path, media_temp, dest_video)
        log(f"   📂 Media {method} atomically.")
        log("   📝 Generated offline metadata.")
        with get_db() as conn:
            conn.execute("DELETE FROM lost_media WHERE video_id = ?", (vid_id,))
            conn.commit()
        return True, "Force import successful"
    except MediaReservationConflict:
        raise
    except OSError as exc:
        log(f"   ❌ Force import failed: {exc}")
        return False, str(exc)
    finally:
        if media_temp is not None:
            media_temp.unlink(missing_ok=True)
        if metadata_temp is not None:
            metadata_temp.unlink(missing_ok=True)


def _is_allowed_root(path: Path, allowed_bases=RECOVERY_ALLOWED_BASES) -> bool:
    try:
        resolved = path.resolve(strict=False)
        return any(resolved == base.resolve(strict=False) for base in allowed_bases)
    except (OSError, RuntimeError):
        return True


def _delete_candidate_error(
    path: Path,
    *,
    destruct: bool,
    allowed_bases=RECOVERY_ALLOWED_BASES,
    source_dir: Path = SOURCE_DIR,
) -> str | None:
    if not path_in_allowed_tree(path, allowed_bases):
        return "Path outside allowed directories"
    if _is_allowed_root(path, allowed_bases):
        return "Configured root directories cannot be deleted"
    if not os.path.lexists(path):
        return "File not found"
    if path.is_dir():
        return "Directory deletion is not allowed"
    if not destruct and path_in_allowed_tree(path, (source_dir,)):
        return "Source files require destruct mode"
    return None


def _parse_delete_batch_payload(data: object) -> tuple[list[str] | None, bool, str | None]:
    if not isinstance(data, dict):
        return None, False, "Request body must be a JSON object"
    paths = data.get("filepaths")
    if not isinstance(paths, list):
        return None, False, "filepaths must be a list"
    if not paths:
        return None, False, "filepaths must contain at least one path"
    if len(paths) > _MAX_DELETE_BATCH:
        return None, False, f"filepaths may contain at most {_MAX_DELETE_BATCH} paths"
    if any(
        not isinstance(path, str)
        or not path
        or "\x00" in path
        or len(path) > _MAX_PATH_LENGTH
        for path in paths
    ):
        return None, False, "Every filepath must be a non-empty path string"
    destruct = data.get("destruct_mode", False)
    if type(destruct) is not bool:
        return None, False, "destruct_mode must be a boolean"
    return paths, destruct, None


def _resolve_destruct_source(
    path: Path,
    video_id: str | None,
    video_map: dict,
    *,
    source_dir: Path = SOURCE_DIR,
    fallback_lookup: Callable[[str], Path | None] | None = None,
) -> Path | None:
    """Find the exact source file for ``video_id`` inside an allowed tree."""
    if not video_id or not is_valid_youtube_id(video_id):
        return None
    candidates: list[Path | None] = []

    if path_in_allowed_tree(path, (source_dir,)):
        candidates.append(path)

    if os.path.islink(path):
        try:
            target_raw = os.readlink(path)
            log(f"   [DESTRUCT] Symlink target: {target_raw}")
            candidates.append(translated_symlink_target(path))
        except OSError as exc:
            log(f"   [DESTRUCT] Warning: Could not read symlink {path}: {exc}")

    meta = video_map.get(video_id) or {}
    raw_ta_path = meta.get("filesystem_path")
    if raw_ta_path:
        candidates.append(translate_ta_path(raw_ta_path))
        log(f"   [DESTRUCT] API Fallback Path: {candidates[-1]}")

    for candidate in candidates:
        if (
            candidate
            and candidate.is_file()
            and path_within(candidate, (source_dir,))
            and extract_id_from_filename(candidate.name) == video_id
        ):
            return candidate

    if fallback_lookup is not None:
        candidate = fallback_lookup(video_id)
        if candidate is not None:
            log(f"   [DESTRUCT] Source index found exact match: {candidate}")
        return candidate

    log(f"   [DESTRUCT] Final fallback: exact recursive search for ID {video_id}")
    video_extensions = {".mp4", ".mkv", ".webm", ".avi", ".mov"}
    for candidate in source_dir.rglob("*"):
        if "tcconf" in candidate.parts:
            continue
        if candidate.suffix.lower() not in video_extensions:
            continue
        if extract_id_from_filename(candidate.name) != video_id:
            continue
        if candidate.is_file() and path_within(candidate, (source_dir,)):
            log(f"   [DESTRUCT] Search found exact match: {candidate}")
            return candidate
    return None


def make_source_lookup(source_dir: Path = SOURCE_DIR) -> Callable[[str], Path | None]:
    """Return a lazy exact-ID lookup that walks the source tree at most once."""
    source_index: dict[str, Path] | None = None

    def find(video_id: str) -> Path | None:
        nonlocal source_index
        if source_index is None:
            source_index = {}
            video_extensions = {".mp4", ".mkv", ".webm", ".avi", ".mov"}
            for candidate in source_dir.rglob("*"):
                if "tcconf" in candidate.parts:
                    continue
                if candidate.suffix.lower() not in video_extensions:
                    continue
                candidate_id = extract_id_from_filename(candidate.name)
                if not candidate_id or candidate_id in source_index:
                    continue
                if candidate.is_file() and path_within(candidate, (source_dir,)):
                    source_index[candidate_id] = candidate
        return source_index.get(video_id)

    return find
