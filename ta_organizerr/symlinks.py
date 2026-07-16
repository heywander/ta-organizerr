"""Core scanning, symlink layout, and channel hide/unhide logic."""
from __future__ import annotations

import os
import shutil
import stat
import threading
import traceback
from pathlib import Path

from .config import HIDDEN_DIR, HOST_SOURCE_BASE, SOURCE_DIR, TARGET_DIR
from .db import get_db
from .logging_util import log
from .library_lock import LIBRARY_MUTATION_LOCK
from .media_reservations import MediaReservationConflict, media_mutation_guard
from .ta_api import MetadataFetchError, fetch_all_metadata
from .utils import (
    channel_component,
    extract_id_from_filename,
    is_video,
    path_in_allowed_tree,
    path_within,
    sanitize,
    translate_host_path,
)


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

    missing_rows: list[tuple[str, str, str, str, str]] = []
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
                missing_rows.append(
                    (video_id, title, channel_name, published, str(video_file))
                )
                log(f"   ⚠️ BROKEN: {folder_name} -> {target}")
            except OSError as exc:
                log(f"   ❌ ERROR: {video_file}: {exc}")

    if missing_rows:
        with get_db() as conn:
            conn.executemany(
                """
                INSERT INTO videos
                (video_id, title, channel, published, symlink, status)
                VALUES (?, ?, ?, ?, ?, 'missing')
                ON CONFLICT(video_id) DO UPDATE SET
                    title = excluded.title,
                    channel = excluded.channel,
                    published = excluded.published,
                    symlink = excluded.symlink,
                    status = excluded.status,
                    last_updated = CURRENT_TIMESTAMP
                WHERE videos.adopted_real = 0
                """,
                missing_rows,
            )
            conn.commit()
    log(f"✅ Check complete. Scanned {total_checked} files, found {len(orphaned)} orphaned symlinks.")
    return orphaned


def fix_broken_symlinks() -> list[str]:
    log("🔧 Scanning for broken/invalid symlinks...")
    fixed: list[str] = []
    with get_db() as conn:
        adopted_rows = conn.execute(
            "SELECT symlink FROM videos WHERE adopted_real = 1 AND symlink IS NOT NULL"
        ).fetchall()
    adopted_entries = {
        str(Path(row["symlink"]).parent.resolve(strict=False) / Path(row["symlink"]).name)
        for row in adopted_rows
        if isinstance(row["symlink"], str)
    }
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
                entry_key = str(video_file.parent.resolve(strict=False) / video_file.name)
                if entry_key in adopted_entries:
                    log(f"   [PRESERVED] Explicitly adopted real media: {video_file}")
                    continue
                size = video_file.stat().st_size
                if size == 0:
                    with media_mutation_guard(
                        paths=(video_file,),
                        action="delete this invalid media file",
                    ):
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
    try:
        with media_mutation_guard(
            trees=(wrong,),
            action="move this channel while its media is being processed",
        ):
            if not wrong.exists():
                return
            managed_roots = (TARGET_DIR, HIDDEN_DIR)
            if (
                wrong.is_symlink()
                or correct.is_symlink()
                or not path_within(wrong, managed_roots)
                or not path_in_allowed_tree(correct, managed_roots)
            ):
                log(f"   ⛔ Refusing unsafe channel-directory migration: {wrong} -> {correct}")
                return
            correct.parent.mkdir(parents=True, exist_ok=True)
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
    except MediaReservationConflict as exc:
        log(f"   ⏳ Deferred channel migration {wrong.name}: {exc}")
    except (OSError, shutil.Error) as exc:
        log(f"   ❌ Migration error for {wrong.name}: {exc}")


def _channel_component_map(channel_names: set[str]) -> dict[str, str]:
    """Build stable channel components independent of current collisions."""
    return {name: channel_component(name) for name in channel_names}


def _enforce_hidden_locations(
    hidden_channels: set[str],
    component_map: dict[str, str] | None = None,
) -> None:
    log("🙈 Enforcing hidden channel status...")
    component_map = component_map or _channel_component_map(hidden_channels)
    sanitized_hidden: set[str] = set()
    for name in hidden_channels:
        component = component_map.get(name, channel_component(name))
        sanitized_hidden.add(component)
        # Treat the old lossy component as hidden during migration. If a
        # public and hidden channel once collided, hiding stale content is the
        # privacy-preserving behavior until new hashed links are generated.
        legacy_component = sanitize(name)
        if legacy_component != component:
            sanitized_hidden.add(legacy_component)

    # Move Hidden FROM Target TO Hidden.
    if TARGET_DIR.exists():
        for channel_dir in TARGET_DIR.iterdir():
            if channel_dir.is_symlink():
                log(f"   ⛔ Skipping symlinked channel directory: {channel_dir}")
                continue
            if not channel_dir.is_dir() or channel_dir.name not in sanitized_hidden:
                continue
            dest = HIDDEN_DIR / channel_dir.name
            log(f"   [MOVE] Found hidden channel in Public: {channel_dir.name}")
            _migrate_channel_dir(channel_dir, dest)

    # Move Public FROM Hidden TO Target.
    if HIDDEN_DIR.exists():
        for channel_dir in HIDDEN_DIR.iterdir():
            if channel_dir.is_symlink():
                log(f"   ⛔ Skipping symlinked channel directory: {channel_dir}")
                continue
            if not channel_dir.is_dir() or channel_dir.name in sanitized_hidden:
                continue
            dest = TARGET_DIR / channel_dir.name
            log(f"   [MOVE] Found public channel in Hidden: {channel_dir.name}")
            _migrate_channel_dir(channel_dir, dest)


def _index_managed_links(db_rows) -> dict[str, list[Path]]:
    """Index only DB-owned prior links, including a hidden/public move."""
    result: dict[str, list[Path]] = {}
    target_root = TARGET_DIR.resolve(strict=False)
    hidden_root = HIDDEN_DIR.resolve(strict=False)
    for row in db_rows:
        video_id = row["video_id"]
        raw_path = row["symlink"]
        if not isinstance(video_id, str) or not isinstance(raw_path, str):
            continue
        prior = Path(raw_path)
        candidates = [prior]
        try:
            prior_entry = prior.parent.resolve(strict=False) / prior.name
            if prior_entry.is_relative_to(target_root):
                candidates.append(HIDDEN_DIR / prior_entry.relative_to(target_root))
            elif prior_entry.is_relative_to(hidden_root):
                candidates.append(TARGET_DIR / prior_entry.relative_to(hidden_root))
        except (OSError, RuntimeError, ValueError):
            pass
        for link in candidates:
            if not link.is_symlink() or not path_in_allowed_tree(
                link, (TARGET_DIR, HIDDEN_DIR)
            ):
                continue
            try:
                target_id = extract_id_from_filename(Path(os.readlink(link)).name)
            except OSError:
                continue
            if target_id == video_id:
                result.setdefault(video_id, []).append(link)
                break
    return result


def _paired_managed_path(path: Path) -> Path | None:
    """Return the same relative entry in the opposite public/hidden root."""
    try:
        entry = path.parent.resolve(strict=False) / path.name
        target_root = TARGET_DIR.resolve(strict=False)
        hidden_root = HIDDEN_DIR.resolve(strict=False)
        if entry.is_relative_to(target_root):
            return HIDDEN_DIR / entry.relative_to(target_root)
        if entry.is_relative_to(hidden_root):
            return TARGET_DIR / entry.relative_to(hidden_root)
    except (OSError, RuntimeError, ValueError):
        pass
    return None


def _real_managed_entry(path: Path) -> bool:
    try:
        entry_stat = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISREG(entry_stat.st_mode)
        and path_in_allowed_tree(path, (TARGET_DIR, HIDDEN_DIR))
    )


def _remove_stale_managed_links(
    video_id: str,
    current_link: Path,
    link_index: dict[str, list[Path]],
) -> None:
    """Retire old generated paths only after the replacement link is valid."""
    managed_roots = tuple(root.resolve(strict=False) for root in (TARGET_DIR, HIDDEN_DIR))
    current_entry = current_link.parent.resolve(strict=False) / current_link.name
    for link in link_index.get(video_id, []):
        try:
            link_entry = link.parent.resolve(strict=False) / link.name
            if link_entry == current_entry or not link.is_symlink():
                continue
            if not path_in_allowed_tree(link, (TARGET_DIR, HIDDEN_DIR)):
                continue
            target = Path(os.readlink(link))
            if extract_id_from_filename(target.name) != video_id:
                continue
            with media_mutation_guard(
                paths=(link,),
                action="remove this superseded organized link",
            ):
                link.unlink()
                log(f"   🧹 Removed superseded generated link: {link}")
                parent = link.parent
                while parent.resolve(strict=False) not in managed_roots:
                    if parent.is_symlink() or not parent.exists() or any(parent.iterdir()):
                        break
                    parent.rmdir()
                    parent = parent.parent
        except MediaReservationConflict as exc:
            log(f"   ⏳ Deferred superseded link cleanup {link}: {exc}")
        except (OSError, RuntimeError) as exc:
            log(f"   ⚠️ Could not clean superseded link {link}: {exc}")
    link_index[video_id] = [current_link]


def _ensure_symlink(host_source_path: Path, dest_file: Path, video_dir: Path, channel_dir: Path) -> tuple[bool, bool, str | None]:
    """Idempotently create/repair the symlink.

    Returns ``(new_or_fixed, verified, error)``. ``error`` is set when no
    usable symlink exists after the attempt.
    """
    if not os.path.lexists(dest_file):
        try:
            with media_mutation_guard(
                paths=(dest_file,),
                action="create this organized link",
            ):
                channel_dir.mkdir(parents=True, exist_ok=True)
                video_dir.mkdir(parents=True, exist_ok=True)
                os.symlink(host_source_path, dest_file)
            return True, False, None
        except FileExistsError:
            # Another serialized/external writer populated the entry; verify
            # it below instead of replacing it blindly.
            pass
        except (OSError, MediaReservationConflict) as exc:
            return False, False, str(exc)

    if dest_file.is_symlink():
        try:
            current_target = Path(os.readlink(dest_file))
            if not current_target.is_absolute():
                current_target = dest_file.parent / current_target
            if current_target.resolve() != host_source_path.resolve():
                with media_mutation_guard(
                    paths=(dest_file,),
                    action="replace this organized link",
                ):
                    dest_file.unlink()
                    os.symlink(host_source_path, dest_file)
                return True, False, None
            return False, True, None
        except OSError as exc:
            return False, False, str(exc)
    return False, False, f"Non-symlink file already exists at {dest_file}"


def _destination_for_video(
    channel_dir: Path,
    folder_name: str,
    video_id: str,
    suffix: str,
) -> tuple[Path, Path]:
    """Keep the legacy layout unless it would collide with another video."""
    preferred_dir = channel_dir / folder_name
    try:
        existing_links = list(preferred_dir.glob("video.*"))
    except OSError:
        existing_links = []

    for existing in existing_links:
        if not existing.is_symlink():
            unique_dir = channel_dir / f"{folder_name} [{video_id}]"
            return unique_dir, unique_dir / f"video{suffix}"
        try:
            target = Path(os.readlink(existing))
        except OSError:
            unique_dir = channel_dir / f"{folder_name} [{video_id}]"
            return unique_dir, unique_dir / f"video{suffix}"
        existing_id = extract_id_from_filename(target.name)
        if existing_id != video_id:
            unique_dir = channel_dir / f"{folder_name} [{video_id}]"
            return unique_dir, unique_dir / f"video{suffix}"
    return preferred_dir, preferred_dir / f"video{suffix}"


def process_videos() -> str | None:
    """Run the full sync: fetch metadata, organize symlinks, persist DB rows."""
    if not _SCAN_LOCK.acquire(blocking=False):
        msg = "Scan already running"
        log(f"⏳ {msg}; skipping duplicate request.")
        return msg
    try:
        # Lock ordering is always scan lock, then the shared filesystem lock.
        # Reconciliation never acquires the scan lock, preventing inversion.
        with LIBRARY_MUTATION_LOCK:
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
        prior_link_rows = conn.execute(
            "SELECT video_id, symlink, adopted_real FROM videos WHERE symlink IS NOT NULL"
        ).fetchall()
    hidden_channels = {row["channel_name"] for row in rows}
    all_channel_names = {
        str(meta["channel_name"])
        for meta in video_map.values()
        if isinstance(meta, dict) and meta.get("channel_name") is not None
    } | hidden_channels
    component_map = _channel_component_map(all_channel_names)

    HIDDEN_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_old_folders()
    _enforce_hidden_locations(hidden_channels, component_map)
    managed_link_index = _index_managed_links(prior_link_rows)
    adopted_paths: dict[str, Path] = {}
    adopted_updates: list[tuple[str, str, str]] = []
    for row in prior_link_rows:
        if (
            "adopted_real" not in row.keys()
            or not row["adopted_real"]
            or not isinstance(row["symlink"], str)
        ):
            continue
        stored = Path(row["symlink"])
        paired = _paired_managed_path(stored)
        current = next(
            (candidate for candidate in (stored, paired) if candidate and _real_managed_entry(candidate)),
            None,
        )
        if current is None:
            continue
        adopted_paths[row["video_id"]] = current
        if str(current) != row["symlink"]:
            adopted_updates.append((str(current), row["video_id"], row["symlink"]))

    # Hidden/public migration must update adopted records even when a
    # terminated video is absent from current TubeArchivist metadata and never
    # enters the source-processing loop below.
    if adopted_updates:
        with get_db() as conn:
            conn.executemany(
                """
                UPDATE videos SET symlink = ?, status = 'adopted',
                                  adopted_real = 1,
                                  last_updated = CURRENT_TIMESTAMP
                WHERE video_id = ? AND symlink = ? AND adopted_real = 1
                """,
                adopted_updates,
            )
            conn.commit()

    new_links = 0
    verified_links = 0

    db_rows: list[tuple] = []
    stale_link_retirements: list[tuple[str, Path]] = []
    try:
        for video_file in SOURCE_DIR.rglob("*"):
            if not video_file.is_file() or not is_video(video_file):
                continue
            video_id = extract_id_from_filename(video_file.name) or video_file.stem
            meta = video_map.get(video_id)
            if not meta:
                continue

            channel_name = str(meta["channel_name"])
            sanitized_channel = component_map.get(
                channel_name,
                channel_component(channel_name),
            )
            is_hidden = meta["channel_name"] in hidden_channels
            target_root = HIDDEN_DIR if is_hidden else TARGET_DIR

            channel_dir = target_root / sanitized_channel
            if meta.get("is_live"):
                channel_dir = channel_dir / "#streams"

            published = sanitize(str(meta["published"]))
            folder_name = f"{published} - {sanitize(meta['title'])}"
            host_source_path = HOST_SOURCE_BASE / video_file.relative_to(SOURCE_DIR)
            video_dir, dest_file = _destination_for_video(
                channel_dir,
                folder_name,
                video_id,
                video_file.suffix,
            )

            link_error = None
            is_adopted = False
            try:
                adopted_path = adopted_paths.get(video_id)
                if adopted_path is not None:
                    current_adopted = adopted_path if _real_managed_entry(adopted_path) else None
                    if current_adopted is not None:
                        # Adoption is an explicit operator decision. Preserve
                        # the real file in place rather than replacing it on a
                        # later normal scan.
                        dest_file = current_adopted
                        video_dir = current_adopted.parent
                        is_adopted = True
                        verified_links += 1
                if is_adopted:
                    pass
                elif not path_in_allowed_tree(dest_file, (target_root,)):
                    link_error = f"Destination escapes target root: {dest_file}"
                else:
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
                    if not link_error and not is_adopted:
                        stale_link_retirements.append((video_id, dest_file))
            except OSError as exc:
                link_error = str(exc)

            row_status = "error" if link_error else ("adopted" if is_adopted else "linked")
            if link_error:
                log(f"   ❌ Link error for {folder_name}: {link_error}")

            db_rows.append((
                video_id,
                meta["title"],
                meta["channel_name"],
                meta["published"],
                str(dest_file),
                1 if meta.get("is_live") else 0,
                row_status,
                1 if is_adopted else 0,
            ))
            processed_videos.append({
                "video_id": video_id,
                "title": meta["title"],
                "channel": meta["channel_name"],
                "published": meta["published"],
                "symlink": str(dest_file),
                "status": row_status,
                "adopted_real": is_adopted,
            })
    except Exception as exc:  # noqa: BLE001 — leave the previous DB snapshot intact
        log(f"❌❌❌ CRITICAL ERROR in process_videos: {exc}")
        log(f"Traceback: {traceback.format_exc()}")
        return str(exc)

    try:
        with get_db() as conn:
            conn.execute("DELETE FROM videos WHERE status IN ('linked', 'error')")
            conn.executemany(
                """
                INSERT OR REPLACE INTO videos
                (video_id, title, channel, published, symlink, is_live, status, adopted_real)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                db_rows,
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 — surface DB consistency failures
        log(f"❌ Could not persist scan results: {exc}")
        return str(exc)

    # The DB is now authoritative for the replacement paths. Only at this
    # point is it safe to retire DB-owned links from the previous snapshot.
    for video_id, dest_file in stale_link_retirements:
        _remove_stale_managed_links(
            video_id,
            dest_file,
            managed_link_index,
        )

    log(f"✅ Scan complete. Processed {len(processed_videos)} videos.")
    log(f"   - New/Fixed Links: {new_links}")
    log(f"   - Verified Links:  {verified_links}")
    return None
