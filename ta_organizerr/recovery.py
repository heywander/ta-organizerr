"""Lost-media recovery: scanning, yt-dlp metadata fetching, force import."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import HIDDEN_DIR, IMPORT_DIR, SOURCE_DIR, TARGET_DIR
from .db import get_db
from .logging_util import log
from .ta_api import fetch_all_metadata
from .utils import (
    extract_id_from_filename,
    is_valid_youtube_id,
    is_video,
    translate_ta_path,
)


def _iter_video_files(root: Path):
    if not root.exists():
        return
    for path in root.rglob("*"):
        if path.is_file() and is_video(path):
            yield path


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
            })

    log(
        f"✅ Scan complete. Unindexed: {len(results['unindexed'])}, "
        f"Redundant: {len(results['redundant'])}, Rescue: {len(results['rescue'])}"
    )
    return results


def recover_video_metadata(filepath: str) -> tuple[bool, str]:
    src_path = Path(filepath)
    if not src_path.exists():
        return False, "File not found"

    vid_id = extract_id_from_filename(src_path.name)
    if not vid_id or not is_valid_youtube_id(vid_id):
        return False, "Could not extract valid Video ID from filename"

    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    dest_video = IMPORT_DIR / src_path.name
    dest_json = IMPORT_DIR / f"{src_path.stem}.info.json"
    output_template = str(IMPORT_DIR / src_path.stem)

    log(f"🚑 Recovering: {vid_id} ...")
    cmd = [
        "yt-dlp",
        "--write-info-json",
        "--skip-download",
        "--id",
        f"https://www.youtube.com/watch?v={vid_id}",
        "-o",
        output_template,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"   ❌ Recovery failed: {exc}")
        return False, str(exc)

    if dest_json.exists() and dest_json.stat().st_size > 0:
        log(f"   ✅ Metadata fetched (exit {result.returncode}).")
    elif result.returncode != 0:
        error_msg = result.stderr.strip() or "Unknown Error"
        log(f"   ⚠️ yt-dlp failed (Exit Code {result.returncode}). Error: {error_msg}")
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO lost_media (video_id, filepath) VALUES (?, ?)",
                (vid_id, str(src_path)),
            )
            conn.commit()
        return False, f"yt-dlp failed: {error_msg} (Added to Lost Media)"

    try:
        if dest_video.exists():
            dest_video.unlink()
        try:
            os.link(src_path, dest_video)
            log("   🔗 Hardlinked video file.")
        except OSError:
            shutil.copy2(src_path, dest_video)
            log("   📂 Copied video file.")
    except OSError as exc:
        return False, f"Failed to move video: {exc}"

    return True, "Ready for import"


def force_import(filepath: str) -> tuple[bool, str]:
    src_path = Path(filepath).resolve()
    if not src_path.exists():
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
    try:
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(offline_meta, fh, indent=4)
        log("   📝 Generated offline metadata.")
        dest_video = IMPORT_DIR / src_path.name
        if dest_video.exists():
            dest_video.unlink()
        try:
            os.link(src_path, dest_video)
            log("   🔗 Hardlinked video.")
        except OSError:
            shutil.copy2(src_path, dest_video)
            log("   ©️ Copied video.")
        with get_db() as conn:
            conn.execute("DELETE FROM lost_media WHERE video_id = ?", (vid_id,))
            conn.commit()
        return True, "Force import successful"
    except OSError as exc:
        log(f"   ❌ Force import failed: {exc}")
        return False, str(exc)


# Allowed bases for any user-supplied filepath operation. These are the only
# directories the API is permitted to read/write under, regardless of what the
# client asks for. Resolution must end inside one of these.
RECOVERY_ALLOWED_BASES = (SOURCE_DIR, TARGET_DIR, HIDDEN_DIR, IMPORT_DIR)
