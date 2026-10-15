"""Core scanning, symlink layout, and channel hide/unhide logic."""
from __future__ import annotations

import os
import shutil
import threading
import traceback
from pathlib import Path

from .config import HIDDEN_DIR, HOST_SOURCE_BASE, SOURCE_DIR, TARGET_DIR
from .db import get_db
from .logging_util import log
from .ta_api import MetadataFetchError, fetch_all_metadata
from .utils import extract_id_from_filename, is_video, sanitize, translate_host_path


# Module-level state for the dashboard.
processed_videos: list[dict] = []
_SCAN_LOCK = threading.Lock()


def scan_is_running() -> bool:
    return _SCAN_LOCK.locked()


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
        for current, _dirs, _files in os.walk(root, topdown=False):
            current_path = Path(current)
            if current_path == root:
                continue
            try:
                if not any(current_path.iterdir()):
                    current_path.rmdir()
                    log(f"   [DELETED EMPTY] {current_path}")
                    cleaned += 1
            except OSError:
                pass
    log(f"🧹 Cleanup complete. Removed {cleaned} empty directories.")


def check_orphaned_links() -> list[dict]:
    log("🔍 Checking for orphaned symlinks...")
    orphaned: list[dict] = []
    total_checked = 0
    roots = (TARGET_DIR, HIDDEN_DIR)
    if not any(root.exists() for root in roots):
        log("⚠️ Target/hidden directories do not exist")
        return orphaned

    with get_db() as conn:
        for root in roots:
            if not root.exists():
                continue
            for video_file in root.rglob("video.*"):
                total_checked += 1
                if not video_file.is_symlink():
                    continue
                try:
                    target = Path(os.readlink(video_file))
                    if not target.is_absolute():
                        target = video_file.parent / target
                    translated_target = translate_host_path(target)
                    if target.exists() or (translated_target and translated_target.exists()):
                        continue

                    try:
                        rel_parent = video_file.parent.relative_to(root)
                        rel_parts = rel_parent.parts
                    except ValueError:
                        rel_parts = ()
                    channel_name = rel_parts[0] if rel_parts else "unknown"
                    folder_name = rel_parts[-1] if rel_parts else video_file.parent.name
                    parts = folder_name.split(" - ", 1)
                    published = parts[0] if parts else "unknown"
                    title = parts[1] if len(parts) > 1 else folder_name
                    video_id = extract_id_from_filename(target.name) or target.stem or "unknown"
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
                    log(f"   ❌ ERROR: {video_file}: {exc}")
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
        for video_file in root.rglob("video.*"):
            if video_file.is_symlink() or not video_file.is_file():
                continue
            try:
                size = video_file.stat().st_size
                if size == 0:
                    video_file.unlink()
                    log(f"   [FIXED] Deleted invalid file: {video_file} (was {size} bytes)")
                    fixed.append(str(video_file))
                else:
                    log(f"   ⚠️ Non-symlink media left untouched: {video_file} ({size} bytes)")
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


def _ensure_symlink(host_source_path: Path, dest_file: Path, video_dir: Path, channel_dir: Path) -> tuple[bool, bool, str | None]:
    """Idempotently create/repair the symlink.

    Returns ``(new_or_fixed, verified, error)``. ``error`` is set when no
    usable symlink exists after the attempt.
    """
    try:
        # Race-safe: try to create first, then handle pre-existing.
        channel_dir.mkdir(parents=True, exist_ok=True)
        video_dir.mkdir(parents=True, exist_ok=True)
        os.symlink(host_source_path, dest_file)
        return True, False, None
    except FileExistsError:
        if dest_file.is_symlink():
            try:
                current_target = Path(os.readlink(dest_file))
                if not current_target.is_absolute():
                    current_target = dest_file.parent / current_target
                if current_target.resolve() != host_source_path.resolve():
                    dest_file.unlink()
                    os.symlink(host_source_path, dest_file)
                    return True, False, None
                return False, True, None
            except OSError as exc:
                return False, False, str(exc)
        return False, False, f"Non-symlink file already exists at {dest_file}"


def _prune_stale_missing_rows() -> None:
    """Drop ``missing`` rows whose symlink entry no longer exists on disk.

    ``check_orphaned_links`` marks broken links as ``missing``; once the user
    (or cleanup) removes the link the row would otherwise linger forever and
    keep inflating the dashboard's missing count.
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT video_id, symlink FROM videos WHERE status = 'missing'"
        ).fetchall()
        stale = [row["video_id"] for row in rows if not row["symlink"] or not os.path.lexists(row["symlink"])]
        if stale:
            conn.executemany("DELETE FROM videos WHERE video_id = ?", [(vid,) for vid in stale])
            conn.commit()
            log(f"🧹 Pruned {len(stale)} stale 'missing' rows whose links are gone.")


def process_videos() -> str | None:
    """Run the full sync: fetch metadata, organize symlinks, persist DB rows."""
    if not _SCAN_LOCK.acquire(blocking=False):
        msg = "Scan already running"
        log(f"⏳ {msg}; skipping duplicate request.")
        return msg
    try:
        return _process_videos_unlocked()
    finally:
        _SCAN_LOCK.release()


def _process_videos_unlocked() -> str | None:
    global processed_videos
    processed_videos = []

    if not SOURCE_DIR.exists():
        msg = f"Source directory does not exist: {SOURCE_DIR}"
        log(f"❌ {msg}")
        return msg

    try:
        video_map = fetch_all_metadata()
    except MetadataFetchError as exc:
        msg = f"Aborting scan because TubeArchivist metadata fetch failed: {exc}"
        log(f"❌ {msg}")
        return msg

    with get_db() as conn:
        rows = conn.execute("SELECT channel_name FROM hidden_channels").fetchall()
    hidden_channels = {row["channel_name"] for row in rows}

    HIDDEN_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_old_folders()
    _enforce_hidden_locations(hidden_channels)

    new_links = 0
    verified_links = 0

    with get_db() as conn:
        # Keep probed codecs across rescans: park them in a temp table, then
        # the INSERT below pulls them back by video_id.
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS codec_cache AS SELECT video_id, video_codec, audio_codec FROM videos WHERE video_codec IS NOT NULL")
        conn.execute("DELETE FROM videos WHERE status IN ('linked', 'error')")
        try:
            for video_file in SOURCE_DIR.rglob("*"):
                if not video_file.is_file() or not is_video(video_file):
                    continue
                video_id = extract_id_from_filename(video_file.name) or video_file.stem
                meta = video_map.get(video_id)
                if not meta:
                    continue

                sanitized_channel = sanitize(meta["channel_name"], fallback="Unknown Channel")
                is_hidden = meta["channel_name"] in hidden_channels
                target_root = HIDDEN_DIR if is_hidden else TARGET_DIR
                other_root = TARGET_DIR if is_hidden else HIDDEN_DIR

                _migrate_channel_dir(other_root / sanitized_channel, target_root / sanitized_channel)

                channel_dir = target_root / sanitized_channel
                if meta.get("is_live"):
                    channel_dir = channel_dir / "#streams"

                folder_name = f"{meta['published']} - {sanitize(meta['title'], fallback=video_id)}"
                video_dir = channel_dir / folder_name
                host_source_path = HOST_SOURCE_BASE / video_file.relative_to(SOURCE_DIR)
                dest_file = video_dir / f"video{video_file.suffix}"

                link_error = None
                try:
                    is_new, is_verified, link_error = _ensure_symlink(
                        host_source_path,
                        dest_file,
                        video_dir,
                        channel_dir,
                    )
                    if is_new:
                        new_links += 1
                        log(f"   [NEW/FIX] Linked: {folder_name}")
                    elif is_verified:
                        verified_links += 1
                except OSError as exc:
                    link_error = str(exc)

                row_status = "error" if link_error else "linked"
                if link_error:
                    log(f"   ❌ Link error for {folder_name}: {link_error}")

                conn.execute(
                    """
                    INSERT OR REPLACE INTO videos
                    (video_id, title, channel, published, symlink, is_live, status,
                     source_path, video_codec, audio_codec)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?,
                            COALESCE(?, (SELECT video_codec FROM codec_cache WHERE video_id = ?)),
                            COALESCE(?, (SELECT audio_codec FROM codec_cache WHERE video_id = ?)))
                    """,
                    (
                        video_id,
                        meta["title"],
                        meta["channel_name"],
                        meta["published"],
                        str(dest_file),
                        1 if meta.get("is_live") else 0,
                        row_status,
                        str(video_file),
                        None, video_id,
                        None, video_id,
                    ),
                )
                processed_videos.append({
                    "video_id": video_id,
                    "title": meta["title"],
                    "channel": meta["channel_name"],
                    "published": meta["published"],
                    "symlink": str(dest_file),
                    "status": row_status,
                })
        except Exception as exc:  # noqa: BLE001 — ensure rollback path covers everything
            conn.rollback()
            log(f"❌❌❌ CRITICAL ERROR in process_videos: {exc}")
            log(f"Traceback: {traceback.format_exc()}")
            return str(exc)
        conn.execute("DROP TABLE IF EXISTS codec_cache")
        conn.commit()

    _prune_stale_missing_rows()

    log(f"✅ Scan complete. Processed {len(processed_videos)} videos.")
    log(f"   - New/Fixed Links: {new_links}")
    log(f"   - Verified Links:  {verified_links}")
    return None
