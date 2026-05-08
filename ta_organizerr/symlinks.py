"""Core scanning, symlink layout, and channel hide/unhide logic."""
from __future__ import annotations

import os
import shutil
import traceback
from pathlib import Path

from .config import HIDDEN_DIR, HOST_SOURCE_BASE, SOURCE_DIR, TARGET_DIR
from .db import get_db
from .logging_util import log
from .ta_api import fetch_all_metadata
from .utils import extract_id_from_filename, is_video, sanitize


# Module-level state for the dashboard.
processed_videos: list[dict] = []


def cleanup_old_folders() -> None:
    log("🧹 Starting cleanup of empty folders...")
    cleaned = 0
    for root in (TARGET_DIR, HIDDEN_DIR):
        try:
            if not root.exists():
                continue
        except OSError as exc:
            log(f"   ⚠️ Cannot access {root}: {exc}")
            continue
        for channel_dir in root.iterdir():
            if not channel_dir.is_dir():
                continue
            for video_dir in list(channel_dir.iterdir()):
                if not video_dir.is_dir():
                    continue
                try:
                    if not any(video_dir.iterdir()):
                        video_dir.rmdir()
                        log(f"   [DELETED EMPTY] {video_dir.name}")
                        cleaned += 1
                except OSError:
                    pass
            try:
                if channel_dir.exists() and not any(channel_dir.iterdir()):
                    channel_dir.rmdir()
                    log(f"   [DELETED CHANNEL] {channel_dir.name}")
                    cleaned += 1
            except OSError:
                pass
    log(f"🧹 Cleanup complete. Removed {cleaned} empty directories.")


def check_orphaned_links() -> list[dict]:
    log("🔍 Checking for orphaned symlinks...")
    orphaned: list[dict] = []
    total_checked = 0
    if not TARGET_DIR.exists():
        log("⚠️ Target directory does not exist")
        return orphaned

    with get_db() as conn:
        for channel_dir in TARGET_DIR.iterdir():
            if not channel_dir.is_dir():
                continue
            channel_name = channel_dir.name
            for video_dir in channel_dir.iterdir():
                if not video_dir.is_dir():
                    continue
                folder_name = video_dir.name
                for video_file in video_dir.glob("video.*"):
                    total_checked += 1
                    if not video_file.is_symlink():
                        continue
                    try:
                        target = Path(os.readlink(video_file))
                        if target.exists():
                            continue
                        parts = folder_name.split(" - ", 1)
                        published = parts[0] if parts else "unknown"
                        title = parts[1] if len(parts) > 1 else folder_name
                        video_id = target.stem or "unknown"
                        orphaned.append({
                            "video_id": video_id,
                            "path": str(video_file),
                            "target": str(target),
                            "folder": folder_name,
                            "channel": channel_name,
                            "title": title,
                            "published": published,
                        })
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO videos
                            (video_id, title, channel, published, symlink, status)
                            VALUES (?, ?, ?, ?, ?, 'missing')
                            """,
                            (video_id, title, channel_name, published, str(video_file)),
                        )
                        log(f"   ⚠️ BROKEN: {folder_name} -> {target}")
                    except OSError as exc:
                        log(f"   ❌ ERROR: {folder_name}: {exc}")
        conn.commit()
    log(f"✅ Check complete. Scanned {total_checked} files, found {len(orphaned)} orphaned symlinks.")
    return orphaned


def fix_broken_symlinks() -> list[str]:
    log("🔧 Scanning for broken/invalid symlinks...")
    fixed: list[str] = []
    for root in (TARGET_DIR, HIDDEN_DIR):
        try:
            if not root.exists():
                continue
        except OSError:
            continue
        for channel_dir in root.iterdir():
            if not channel_dir.is_dir():
                continue
            for video_dir in channel_dir.iterdir():
                if not video_dir.is_dir():
                    continue
                video_file = video_dir / "video.mp4"
                try:
                    if video_file.exists() and not video_file.is_symlink():
                        size = video_file.stat().st_size
                        video_file.unlink()
                        log(f"   [FIXED] Deleted invalid file: {video_file} (was {size} bytes)")
                        fixed.append(str(video_file))
                except OSError as exc:
                    log(f"   ⚠️ Error checking {video_file}: {exc}")
    log(f"🔧 Cleanup complete. Removed {len(fixed)} invalid files.")
    return fixed


def _migrate_channel_dir(wrong: Path, correct: Path) -> None:
    if not wrong.exists():
        return
    try:
        if not correct.exists():
            shutil.move(str(wrong), str(correct))
            log(f"   [MOVE] Migrated {wrong.name} to {correct.parent.name}")
            return
        for item in list(wrong.iterdir()):
            dest_item = correct / item.name
            if not dest_item.exists():
                shutil.move(str(item), str(dest_item))
        try:
            wrong.rmdir()
        except OSError:
            pass
    except (OSError, shutil.Error) as exc:
        log(f"   ❌ Migration error for {wrong.name}: {exc}")


def _enforce_hidden_locations(hidden_channels: set[str]) -> None:
    log("🙈 Enforcing hidden channel status...")
    sanitized_hidden = {sanitize(name) for name in hidden_channels}

    # Move Hidden FROM Target TO Hidden.
    if TARGET_DIR.exists():
        for channel_dir in TARGET_DIR.iterdir():
            if not channel_dir.is_dir() or channel_dir.name not in sanitized_hidden:
                continue
            dest = HIDDEN_DIR / channel_dir.name
            log(f"   [MOVE] Found hidden channel in Public: {channel_dir.name}")
            try:
                if not dest.exists():
                    shutil.move(str(channel_dir), str(dest))
                    log("   ---> Moved to Hidden")
                else:
                    for item in channel_dir.iterdir():
                        dest_item = dest / item.name
                        if not dest_item.exists():
                            shutil.move(str(item), str(dest_item))
                    try:
                        channel_dir.rmdir()
                        log("   ---> Merged to Hidden")
                    except OSError:
                        log("   ---> Merged to Hidden (Old dir not empty)")
            except (OSError, shutil.Error) as exc:
                log(f"   ❌ Error moving {channel_dir.name}: {exc}")

    # Move Public FROM Hidden TO Target.
    if HIDDEN_DIR.exists():
        for channel_dir in HIDDEN_DIR.iterdir():
            if not channel_dir.is_dir() or channel_dir.name in sanitized_hidden:
                continue
            dest = TARGET_DIR / channel_dir.name
            log(f"   [MOVE] Found public channel in Hidden: {channel_dir.name}")
            try:
                if not dest.exists():
                    shutil.move(str(channel_dir), str(dest))
                    log("   ---> Moved to Public")
                else:
                    for item in channel_dir.iterdir():
                        dest_item = dest / item.name
                        if not dest_item.exists():
                            shutil.move(str(item), str(dest_item))
                    try:
                        channel_dir.rmdir()
                        log("   ---> Merged to Public")
                    except OSError:
                        log("   ---> Merged to Public (Old dir not empty)")
            except (OSError, shutil.Error) as exc:
                log(f"   ❌ Error moving {channel_dir.name}: {exc}")


def _ensure_symlink(host_source_path: Path, dest_file: Path, video_dir: Path, channel_dir: Path) -> tuple[bool, bool]:
    """Idempotently create/repair the symlink. Returns ``(new_or_fixed, verified)``."""
    try:
        # Race-safe: try to create first, then handle pre-existing.
        channel_dir.mkdir(parents=True, exist_ok=True)
        video_dir.mkdir(parents=True, exist_ok=True)
        os.symlink(host_source_path, dest_file)
        return True, False
    except FileExistsError:
        if dest_file.is_symlink():
            try:
                current_target = Path(os.readlink(dest_file))
                if current_target.resolve() != host_source_path.resolve():
                    dest_file.unlink()
                    os.symlink(host_source_path, dest_file)
                    return True, False
                return False, True
            except OSError:
                return False, False
        # Non-symlink stale file: fix_broken_symlinks() will clean this up later.
        return False, False


def process_videos() -> str | None:
    """Run the full sync: fetch metadata, organize symlinks, persist DB rows."""
    global processed_videos
    processed_videos = []

    video_map = fetch_all_metadata()

    with get_db() as conn:
        rows = conn.execute("SELECT channel_name FROM hidden_channels").fetchall()
    hidden_channels = {row["channel_name"] for row in rows}

    HIDDEN_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_old_folders()
    _enforce_hidden_locations(hidden_channels)

    new_links = 0
    verified_links = 0

    with get_db() as conn:
        conn.execute("DELETE FROM videos WHERE status = 'linked'")
        try:
            for channel_path in SOURCE_DIR.iterdir():
                if not channel_path.is_dir():
                    continue
                for video_file in channel_path.glob("*.*"):
                    if not is_video(video_file):
                        continue
                    video_id = extract_id_from_filename(video_file.name) or video_file.stem
                    meta = video_map.get(video_id)
                    if not meta:
                        continue

                    sanitized_channel = sanitize(meta["channel_name"])
                    is_hidden = meta["channel_name"] in hidden_channels
                    target_root = HIDDEN_DIR if is_hidden else TARGET_DIR
                    other_root = TARGET_DIR if is_hidden else HIDDEN_DIR

                    _migrate_channel_dir(other_root / sanitized_channel, target_root / sanitized_channel)

                    channel_dir = target_root / sanitized_channel
                    if meta.get("is_live"):
                        channel_dir = channel_dir / "#streams"

                    folder_name = f"{meta['published']} - {sanitize(meta['title'])}"
                    video_dir = channel_dir / folder_name
                    host_source_path = HOST_SOURCE_BASE / video_file.relative_to(SOURCE_DIR)
                    dest_file = video_dir / f"video{video_file.suffix}"

                    try:
                        is_new, is_verified = _ensure_symlink(host_source_path, dest_file, video_dir, channel_dir)
                        if is_new:
                            new_links += 1
                            log(f"   [NEW/FIX] Linked: {folder_name}")
                        elif is_verified:
                            verified_links += 1
                    except OSError as exc:
                        log(f"   ❌ Link error for {folder_name}: {exc}")

                    conn.execute(
                        """
                        INSERT OR REPLACE INTO videos
                        (video_id, title, channel, published, symlink, is_live, status)
                        VALUES (?, ?, ?, ?, ?, ?, 'linked')
                        """,
                        (
                            video_id,
                            meta["title"],
                            meta["channel_name"],
                            meta["published"],
                            str(dest_file),
                            1 if meta.get("is_live") else 0,
                        ),
                    )
                    processed_videos.append({
                        "video_id": video_id,
                        "title": meta["title"],
                        "channel": meta["channel_name"],
                        "published": meta["published"],
                        "symlink": str(dest_file),
                    })
        except Exception as exc:  # noqa: BLE001 — ensure rollback path covers everything
            conn.rollback()
            log(f"❌❌❌ CRITICAL ERROR in process_videos: {exc}")
            log(f"Traceback: {traceback.format_exc()}")
            return str(exc)
        conn.commit()

    log(f"✅ Scan complete. Processed {len(processed_videos)} videos.")
    log(f"   - New/Fixed Links: {new_links}")
    log(f"   - Verified Links:  {verified_links}")
    return None
