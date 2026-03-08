from pathlib import Path
import os
import requests
import re
import sys
import threading
import time
import ipaddress
import shutil
from datetime import datetime
from functools import wraps
from flask import Flask, jsonify, render_template, request, abort, Response, send_from_directory, session, redirect, url_for

# Load config from environment variables
API_URL = os.getenv("API_URL", "http://localhost:8457/api")
VIDEO_URL = os.getenv("VIDEO_URL", "http://localhost:8457/video/")
API_TOKEN = os.getenv("API_TOKEN", "")
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", 60)) # Default 60 minutes
ALLOWED_IPS = [ip.strip() for ip in os.getenv("ALLOWED_IPS", "127.0.0.1").split(",")]
UI_USERNAME = os.getenv("UI_USERNAME", "admin")
UI_PASSWORD = os.getenv("UI_PASSWORD", "password")
SOURCE_DIR = Path("/app/source")
TARGET_DIR = Path("/app/target")
HIDDEN_DIR = Path("/app/hidden")
IMPORT_DIR = Path("/app/import")
DATA_DIR = Path("/app/data")
HEADERS = {"Authorization": f"Token {API_TOKEN}"}

# Serve static files from ui/dist
STATIC_FOLDER = os.path.join(os.getcwd(), 'ui', 'dist')
app = Flask(__name__, static_folder=STATIC_FOLDER, static_url_path='/')
app.secret_key = os.getenv("FLASK_SECRET_KEY", "tubesortermagicpika") # Change in production!

# Database setup
import sqlite3
from contextlib import contextmanager

DB_PATH = Path("/app/data/videos.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS videos (
                video_id TEXT PRIMARY KEY,
                title TEXT,
                channel TEXT,
                published TEXT,
                symlink TEXT,
                status TEXT,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS lost_media (
                video_id TEXT PRIMARY KEY,
                filepath TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS hidden_channels (
                channel_name TEXT PRIMARY KEY
            );
        """)
        conn.commit()

# Retry loop for DB initialization to prevent crash on SMB lock
while True:
    try:
        init_db()
        print("Database initialized successfully.", flush=True)
        break
    except Exception as e:
        print(f"Database initialization failed (retrying in 10s): {e}", flush=True)
        import time
        time.sleep(10)

# Global State
processed_videos = []
log_buffer = []
log_lock = threading.Lock()
transcode_log_buffer = []
transcode_log_lock = threading.Lock()

# Utility functions
def log(msg):
    """Logs a message to stdout and the in-memory buffer."""
    print(msg, flush=True)
    with log_lock:
        log_buffer.append(msg)
        if len(log_buffer) > 1000:
            log_buffer.pop(0)

def tlog(msg):
    """Logs a message to the transcode log buffer."""
    print(f"[TRANSCODE] {msg}", flush=True)
    with transcode_log_lock:
        transcode_log_buffer.append(msg)
        if len(transcode_log_buffer) > 500:
            transcode_log_buffer.pop(0)

def detect_encoder():
    """Detect best available hardware encoder."""
    import subprocess
    try:
        result = subprocess.run(['ffmpeg', '-hide_banner', '-encoders'], 
                              capture_output=True, text=True)
        encoders = result.stdout
        
        if 'h264_nvenc' in encoders:
            return 'h264_nvenc'
        elif 'h264_vaapi' in encoders:
            return 'h264_vaapi'
        elif 'h264_videotoolbox' in encoders:
            return 'h264_videotoolbox'
        else:
            return 'libx264'
    except:
        return 'libx264'

def probe_codecs(filepath):
    """Probe video and audio codecs using ffprobe."""
    import subprocess
    try:
        # Get video codec
        v_result = subprocess.run([
            'ffprobe', '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=codec_name', '-of', 'csv=p=0', filepath
        ], capture_output=True, text=True)
        video_codec = v_result.stdout.strip()
        
        # Get audio codec
        a_result = subprocess.run([
            'ffprobe', '-v', 'error', '-select_streams', 'a:0',
            '-show_entries', 'stream=codec_name', '-of', 'csv=p=0', filepath
        ], capture_output=True, text=True)
        audio_codec = a_result.stdout.strip()
        
        return video_codec, audio_codec
    except Exception as e:
        tlog(f"Error probing {filepath}: {e}")
        return None, None

def transcode_video(filepath, encoder='libx264'):
    """Transcode a video file to H.264/AAC."""
    import subprocess
    
    original_path = Path(filepath)
    
    # Try to resolve symlink first (don't check if it exists, broken symlinks still exist as links)
    if original_path.is_symlink():
        try:
            actual_file = Path(os.readlink(original_path)).resolve()
            tlog(f"Following symlink: {filepath} -> {actual_file}")
            
            # Translate host path to container path
            # Host: /mnt/user/tubearchives/bp/... → Container: /app/source/...
            actual_file_str = str(actual_file)
            if actual_file_str.startswith("/mnt/user/tubearchives/bp"):
                container_path = actual_file_str.replace("/mnt/user/tubearchives/bp", "/app/source", 1)
                tlog(f"Translated path: {actual_file} -> {container_path}")
                filepath = container_path
            else:
                filepath = str(actual_file)
        except Exception as e:
            tlog(f"Error resolving symlink: {e}")
            return False
    elif not original_path.exists():
        tlog(f"File not found: {filepath}")
        return False
    
    # Now check if the actual file exists
    if not Path(filepath).exists():
        tlog(f"Source file not found: {filepath}")
        return False
    
    video_codec, audio_codec = probe_codecs(filepath)
    
    if video_codec == 'h264' and audio_codec == 'aac':
        tlog(f"Already H.264/AAC: {filepath}")
        return True
    
    temp_file = f"{filepath}.temp.mp4"
    
    try:
        # Determine transcode strategy
        if video_codec == 'h264':
            tlog(f"Audio-only transcode: {filepath}")
            cmd = [
                'ffmpeg', '-v', 'error', '-stats', '-i', filepath,
                '-c:v', 'copy',
                '-c:a', 'aac', '-b:a', '192k',
                '-movflags', '+faststart',
                '-y', temp_file
            ]
        else:
            tlog(f"Full transcode using {encoder}: {filepath}")
            if encoder == 'h264_nvenc':
                cmd = [
                    'ffmpeg', '-v', 'error', '-stats', '-i', filepath,
                    '-c:v', 'h264_nvenc', '-preset', 'fast', '-cq', '23',
                    '-c:a', 'aac', '-b:a', '192k',
                    '-movflags', '+faststart',
                    '-y', temp_file
                ]
            elif encoder == 'h264_vaapi':
                cmd = [
                    'ffmpeg', '-v', 'error', '-stats',
                    '-hwaccel', 'vaapi', '-hwaccel_output_format', 'vaapi',
                    '-i', filepath,
                    '-vf', 'format=nv12,hwupload',
                    '-c:v', 'h264_vaapi', '-b:v', '5M',
                    '-c:a', 'aac', '-b:a', '192k',
                    '-movflags', '+faststart',
                    '-y', temp_file
                ]
            else:  # libx264
                cmd = [
                    'ffmpeg', '-v', 'error', '-stats', '-i', filepath,
                    '-c:v', 'libx264', '-crf', '23', '-preset', 'medium',
                    '-c:a', 'aac', '-b:a', '192k',
                    '-movflags', '+faststart',
                    '-y', temp_file
                ]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode == 0:
            # Replace original
            Path(filepath).unlink()
            Path(temp_file).rename(filepath)
            tlog(f"✅ Success: {filepath}")
            return True
        else:
            # Check if it's a GPU error and retry with CPU
            if encoder in ['h264_nvenc', 'h264_vaapi', 'h264_videotoolbox'] and 'libcuda' in result.stderr or 'Cannot load' in result.stderr:
                tlog(f"⚠️ GPU encoding failed, retrying with CPU (libx264)...")
                
                # Retry with libx264
                if video_codec == 'h264':
                    cpu_cmd = [
                        'ffmpeg', '-v', 'error', '-stats', '-i', filepath,
                        '-c:v', 'copy',
                        '-c:a', 'aac', '-b:a', '192k',
                        '-movflags', '+faststart',
                        '-y', temp_file
                    ]
                else:
                    cpu_cmd = [
                        'ffmpeg', '-v', 'error', '-stats', '-i', filepath,
                        '-c:v', 'libx264', '-crf', '23', '-preset', 'medium',
                        '-c:a', 'aac', '-b:a', '192k',
                        '-movflags', '+faststart',
                        '-y', temp_file
                    ]
                
                cpu_result = subprocess.run(cpu_cmd, capture_output=True, text=True)
                
                if cpu_result.returncode == 0:
                    Path(filepath).unlink()
                    Path(temp_file).rename(filepath)
                    tlog(f"✅ Success (CPU): {filepath}")
                    return True
                else:
                    tlog(f"❌ Failed (CPU): {filepath}")
                    tlog(f"Error: {cpu_result.stderr}")
                    if Path(temp_file).exists():
                        Path(temp_file).unlink()
                    return False
            else:
                tlog(f"❌ Failed: {filepath}")
                tlog(f"Error: {result.stderr}")
                if Path(temp_file).exists():
                    Path(temp_file).unlink()
                return False
            
    except Exception as e:
        tlog(f"❌ Exception: {e}")
        if Path(temp_file).exists():
            Path(temp_file).unlink()
        return False

def sanitize(text):
    text = text.encode("ascii", "ignore").decode()
    text = re.sub(r'[\/:*?"<>|]', "_", text)
    return text.strip()

def fetch_all_metadata():
    log("📥 Fetching all video metadata...")
    video_map = {}
    page = 1
    while True:
        url = f"{API_URL}/video/?page={page}"
        try:
            response = requests.get(url, headers=HEADERS)
            response.raise_for_status()
            data = response.json()
            
            if 'data' not in data or not data['data']:
                break
                
            for video in data['data']:
                # Try to find the ID. It might be 'youtube_id' or '_id'
                vid_id = video.get("youtube_id") or video.get("_id")
                if not vid_id:
                    continue
                    
                title = video.get("title", "unknown_title")
                channel_info = video.get("channel", {})
                channel_name = channel_info.get("channel_name") or channel_info.get("channel_title") or "Unknown Channel"
                # Fix date format: take only first 10 chars (YYYY-MM-DD)
                raw_date = video.get("published", "unknown_date")
                published = raw_date[:10] if len(raw_date) >= 10 else raw_date.replace("/", "-")
                
                video_map[vid_id] = {
                    "title": title,
                    "channel_name": channel_name,
                    "published": published,
                    "filesystem_path": video.get("path") or video.get("filesystem_path")
                }
            
            # Check pagination to see if we are done
            if 'paginate' in data:
                current = data['paginate'].get('current_page')
                last = data['paginate'].get('last_page')
                if current is not None and last is not None and current >= last:
                    break
            else:
                pass

            log(f"   - Page {page} fetched. Total videos so far: {len(video_map)}")
            page += 1
            
        except Exception as e:
            log(f"❌ Error fetching page {page}: {e}")
            break
            
    log(f"✅ Metadata fetch complete. Found {len(video_map)} videos.")
    return video_map

def cleanup_old_folders():
    """
    Scans both TARGET_DIR and HIDDEN_DIR for empty or orphaned folders.
    Safely deletes them if they contain no real files.
    """
    log("🧹 Starting aggressive cleanup of empty folders...")
    cleaned_count = 0
    
    for root in [TARGET_DIR, HIDDEN_DIR]:
        if not root.exists():
            continue
            
        # Walk top-down: Channels
        for channel_dir in root.iterdir():
            if not channel_dir.is_dir():
                continue
                
            # Videos
            for video_dir in list(channel_dir.iterdir()): # List to allow removal
                if not video_dir.is_dir():
                    continue
                    
                # Check if it contains any real files
                safe_to_delete = True
                for item in video_dir.iterdir():
                    if not item.is_symlink():
                        safe_to_delete = False
                        break
                
                if safe_to_delete:
                    try:
                        # Remove all symlinks first
                        for item in list(video_dir.iterdir()):
                            item.unlink()
                        # Remove video directory
                        video_dir.rmdir()
                        log(f"   [DELETED VIDEO] {video_dir.name}")
                        cleaned_count += 1
                    except Exception as e:
                        pass # Likely not empty
            
            # After cleaning videos, try to clean the channel dir itself if empty
            try:
                if channel_dir.exists() and not any(channel_dir.iterdir()):
                    channel_dir.rmdir()
                    log(f"   [DELETED CHANNEL] {channel_dir.name}")
                    cleaned_count += 1
            except Exception:
                pass
                    
    log(f"🧹 Cleanup complete. Removed {cleaned_count} empty/orphaned directories.")

def check_orphaned_links():
    """
    Scans TARGET_DIR for video.mp4 symlinks and checks if they point to valid files.
    For orphaned links, parses the folder structure to extract metadata.
    Stores results in database.
    """
    log("🔍 Checking for orphaned symlinks...")
    orphaned = []
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
                
                # Look for video files
                for video_file in video_dir.glob("video.*"):
                    total_checked += 1
                    
                    if video_file.is_symlink():
                        try:
                            # Check if the symlink target exists
                            target = Path(os.readlink(video_file))
                            
                            if not target.exists():
                                # Parse folder name: "YYYY-MM-DD - Title"
                                parts = folder_name.split(" - ", 1)
                                published = parts[0] if len(parts) > 0 else "unknown"
                                title = parts[1] if len(parts) > 1 else folder_name
                                
                                # Try to extract video ID from symlink target path
                                video_id = target.stem if target.stem else "unknown"
                                
                                orphaned.append({
                                    "video_id": video_id,
                                    "path": str(video_file),
                                    "target": str(target),
                                    "folder": folder_name,
                                    "channel": channel_name,
                                    "title": title,
                                    "published": published
                                })
                                
                                # Store in DB
                                conn.execute("""
                                    INSERT OR REPLACE INTO videos 
                                    (video_id, title, channel, published, symlink, status)
                                    VALUES (?, ?, ?, ?, ?, 'missing')
                                """, (video_id, title, channel_name, published, str(video_file)))
                                
                                log(f"   ⚠️ BROKEN: {folder_name} -> {target}")
                        except Exception as e:
                            log(f"   ❌ ERROR: {folder_name}: {e}")
                            
        conn.commit()
                        
    log(f"✅ Check complete. Scanned {total_checked} files, found {len(orphaned)} orphaned symlinks.")
    return orphaned

def extract_id_from_filename(filename):
    """
    Extracts YouTube ID from filename.
    Expects format: 'Title [VIDEO_ID].ext' or just '[VIDEO_ID].ext'
    """
    # Regex for [VIDEO_ID] at end of stem
    match = re.search(r'\[([a-zA-Z0-9_-]{11})\]$', Path(filename).stem)
    if match:
        return match.group(1)
    
    # Fallback: maybe the whole filename is the ID?
    if re.match(r'^[a-zA-Z0-9_-]{11}$', Path(filename).stem):
        return Path(filename).stem
        
    return None

def scan_for_unindexed_videos():
    """
    Scans both SOURCE_DIR and TARGET_DIR for files.
    Classifies them as:
    - unindexed: Not in TA DB (Needs Import)
    - redundant: In TA DB AND Source exists (Safe Duplicate)
    - rescue: In TA DB BUT Source missing (Needs Rescue/Import)
    """
    log("🔍 Scanning for unindexed and legacy files...")
    
    # 1. Fetch current known IDs and their source paths
    video_map = fetch_all_metadata() # {id: {path: ..., ...}}
    known_ids = set(video_map.keys())
    
    # Fetch Lost Media IDs
    with get_db() as conn:
        lost_rows = conn.execute("SELECT video_id FROM lost_media").fetchall()
        lost_ids = {row["video_id"] for row in lost_rows}
    
    results = {
        "unindexed": [],
        "redundant": [],
        "rescue": [],
        "lost": []
    }

    # Helper to check if file is video
    def is_video(f):
        return f.suffix.lower() in ['.mp4', '.mkv', '.webm', '.mov']

    # --- Scan SOURCE_DIR (Standard Orphan Check) ---
    if SOURCE_DIR.exists():
        for channel_path in SOURCE_DIR.iterdir():
            if not channel_path.is_dir(): continue
            for video_file in channel_path.glob("*.*"):
                if not is_video(video_file): continue
                
                vid_id = extract_id_from_filename(video_file.name)
                if vid_id and vid_id not in known_ids:
                    # Check if it is known LOST media
                    file_info = {
                        "path": str(video_file),
                        "filename": video_file.name,
                        "video_id": vid_id,
                        "size_mb": round(video_file.stat().st_size / (1024*1024), 2),
                        "ta_source": "Source Dir"
                    }
                    
                    if vid_id in lost_ids:
                         results["lost"].append(file_info)
                    else:
                        results["unindexed"].append(file_info)


    # --- Scan TARGET_DIR (Legacy "Pinchflat" Check) ---
    if TARGET_DIR.exists():
        for channel_path in TARGET_DIR.iterdir():
            if not channel_path.is_dir(): continue
            for video_file in channel_path.glob("*.*"):
                if not is_video(video_file): continue
                
                # We only care about REAL files, not symlinks
                if video_file.is_symlink():
                    continue
                    
                vid_id = extract_id_from_filename(video_file.name)
                
                # Case 1: ID NOT in TA -> Recoverable
                if vid_id and vid_id not in known_ids:
                     results["unindexed"].append({
                        "path": str(video_file),
                        "filename": video_file.name,
                        "video_id": vid_id,
                        "type": "target_realfile",
                        "size_mb": round(video_file.stat().st_size / (1024 * 1024), 2)
                    })
                
                # Case 2: ID IS in TA
                elif vid_id:
                    # Check if TA's source file actually exists
                    ta_source_path = Path(video_map[vid_id]['filesystem_path'])
                    
                    if ta_source_path.exists():
                        # TA has it, Source exists. This file is REDUNDANT.
                        results["redundant"].append({
                            "path": str(video_file),
                            "filename": video_file.name,
                            "video_id": vid_id,
                            "ta_source": str(ta_source_path),
                            "size_mb": round(video_file.stat().st_size / (1024 * 1024), 2)
                        })
                    else:
                        # TA has it, BUT source is MISSING. This file is a RESCUE candidate.
                        results["rescue"].append({
                            "path": str(video_file),
                            "filename": video_file.name,
                            "video_id": vid_id,
                            "ta_source": str(ta_source_path), # Missing path
                            "size_mb": round(video_file.stat().st_size / (1024 * 1024), 2)
                        })

    log(f"✅ Scan complete. Unindexed: {len(results['unindexed'])}, Redundant: {len(results['redundant'])}, Rescue: {len(results['rescue'])}")
    return results

def recover_video_metadata(filepath):
    """
    Uses yt-dlp to fetch metadata for a video file and prepares it for import.
    """
    import subprocess
    import shutil
    import json
    
    src_path = Path(filepath)
    if not src_path.exists():
        return False, "File not found"
        
    vid_id = extract_id_from_filename(src_path.name)
    if not vid_id:
        return False, "Could not extract Video ID from filename"
        
    # Ensure import dir exists
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    
    # Target paths
    dest_video = IMPORT_DIR / src_path.name
    dest_json = IMPORT_DIR / f"{src_path.stem}.info.json"
    
    log(f"🚑 Recovering: {vid_id} ...")
    
    # 1. Fetch Metadata using yt-dlp
    cmd = [
        "yt-dlp",
        "--write-info-json",
        "--skip-download",
        "--id",
        f"https://www.youtube.com/watch?v={vid_id}",
        "-o", f"{IMPORT_DIR}/{src_path.stem}"
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        # Check if the metadata file was actually created
        
        if dest_json.exists() and dest_json.stat().st_size > 0:
             log(f"   ✅ Metadata fetched successfully (ignoring exit code {result.returncode}).")
        elif result.returncode != 0:
            error_msg = result.stderr.strip() or "Unknown Error"
            log(f"   ⚠️ yt-dlp failed (Exit Code {result.returncode}). Error: {error_msg}")
            
            # Smart Detection: Only mark as LOST if it's actually a "Video unavailable" error
            # If it's a network error, maybe we shouldn't mark it as lost yet?
            # For now, let's just log it better and still allow the user to see it in Lost Media (where they can 'Force' or 'Delete')
            
            with get_db() as conn:
                conn.execute("INSERT OR REPLACE INTO lost_media (video_id, filepath) VALUES (?, ?)", (vid_id, str(src_path)))
                conn.commit()
            return False, f"yt-dlp failed: {error_msg} (Added to Lost Media)"

            
        # 2. Copy/Symlink Video File
        try:
            # We hardlink if possible to save space/time, otherwise copy
            if dest_video.exists():
                dest_video.unlink()
            
            # Try symlink first? No, TA import consumes files. Copying is safer or hardlink.
            # Let's try hardlink (link)
            try:
                os.link(src_path, dest_video)
                log("   🔗 Hardlinked video file.")
            except OSError:
                shutil.copy2(src_path, dest_video)
                log("   📂 Copied video file.")
                
        except Exception as e:
            return False, f"Failed to move video: {e}"
            
        return True, "Ready for import"
        
    except Exception as e:
        log(f"   ❌ Recovery failed: {e}")
        return False, str(e)

# Main logic

def process_videos():
    global processed_videos
    processed_videos = []
    
    # 1. Fetch all metadata first
    video_map = fetch_all_metadata()
    
    # Get hidden channels
    hidden_channels = set()
    with get_db() as conn:
        rows = conn.execute("SELECT channel_name FROM hidden_channels").fetchall()
        hidden_channels = {row["channel_name"] for row in rows}

    # Ensure hidden directory exists
    HIDDEN_DIR.mkdir(parents=True, exist_ok=True)
    
    # 2. Run cleanup (On both target and hidden)
    # We need to adapt cleanup to handle hidden too, or just run it on both explicitly if we update the function
    # Let's keep it simple for now and rely on logic below to move things

    cleanup_old_folders()

    # 3. Enforce Hidden Logic (Independent of Source)
    # This ensures that even if files aren't in source (e.g. preserved in target), they still get moved.
    log("🙈 Enforcing hidden channel status...")
    
    # Move Hidden FROM Target TO Hidden
    if TARGET_DIR.exists():
        for channel_dir in TARGET_DIR.iterdir():
            if not channel_dir.is_dir(): continue
            
            # We need to match the folder name to the channel name.
            # Since folder names are sanitized, we might not know the original name easily.
            # BUT, we can check if the folder name *matches the sanitized version* of any hidden channel.
            
            # Optimization: Create a set of sanitized hidden names
            sanitized_hidden = {sanitize(name) for name in hidden_channels}
            
            if channel_dir.name in sanitized_hidden:
                # It is hidden, but in target! Move it.
                dest = HIDDEN_DIR / channel_dir.name
                try:
                    log(f"   [MOVE] Found hidden channel in Public: {channel_dir.name}")
                    if not dest.exists():
                        shutil.move(str(channel_dir), str(dest))
                        log(f"   ---> Moved to Hidden")
                    else:
                        # Merge logic
                        for item in channel_dir.iterdir():
                            dest_item = dest / item.name
                            if not dest_item.exists():
                                shutil.move(str(item), str(dest_item))
                        try:
                            channel_dir.rmdir()
                            log(f"   ---> Merged to Hidden")
                        except:
                            log(f"   ---> Merged to Hidden (Old dir not empty)")
                except Exception as e:
                    log(f"   ❌ Error moving {channel_dir.name}: {e}")

    # Move Public FROM Hidden TO Target
    if HIDDEN_DIR.exists():
        for channel_dir in HIDDEN_DIR.iterdir():
            if not channel_dir.is_dir(): continue
            
            # Optimization: Create a set of sanitized hidden names
            sanitized_hidden = {sanitize(name) for name in hidden_channels}
            
            if channel_dir.name not in sanitized_hidden:
                # It is NOT hidden, but needs to be in Public!
                dest = TARGET_DIR / channel_dir.name
                try:
                    log(f"   [MOVE] Found public channel in Hidden: {channel_dir.name}")
                    if not dest.exists():
                        shutil.move(str(channel_dir), str(dest))
                        log(f"   ---> Moved to Public")
                    else:
                        # Merge logic
                        for item in channel_dir.iterdir():
                            dest_item = dest / item.name
                            if not dest_item.exists():
                                shutil.move(str(item), str(dest_item))
                        try:
                            channel_dir.rmdir()
                            log(f"   ---> Merged to Public")
                        except:
                            log(f"   ---> Merged to Public (Old dir not empty)")
                except Exception as e:
                    log(f"   ❌ Error moving {channel_dir.name}: {e}")
    
    # Statistics
    new_links = 0
    verified_links = 0
    
    with get_db() as conn:
        # Clear existing "linked" videos (we'll repopulate)
        conn.execute("DELETE FROM videos WHERE status = 'linked'")
        
        try:
            for channel_path in SOURCE_DIR.iterdir():
                if not channel_path.is_dir():
                    continue
                for video_file in channel_path.glob("*.*"):
                    video_id = video_file.stem
                    
                    # Lookup in local map
                    meta = video_map.get(video_id)
                    if not meta:
                        continue
                        
                    sanitized_channel_name = sanitize(meta["channel_name"])
                    is_hidden = meta["channel_name"] in hidden_channels
                    target_root = HIDDEN_DIR if is_hidden else TARGET_DIR
                    other_root = TARGET_DIR if is_hidden else HIDDEN_DIR
                    
                    # Migration Logic
                    wrong_channel_dir = other_root / sanitized_channel_name
                    correct_channel_dir = target_root / sanitized_channel_name
                    
                    if wrong_channel_dir.exists():
                        try:
                            if not correct_channel_dir.exists():
                                shutil.move(str(wrong_channel_dir), str(correct_channel_dir))
                                log(f"   [MOVE] Migrated {sanitized_channel_name} to {target_root.name}")
                            else:
                                for item in list(wrong_channel_dir.iterdir()):
                                    dest_item = correct_channel_dir / item.name
                                    if not dest_item.exists():
                                        shutil.move(str(item), str(dest_item))
                                try:
                                    wrong_channel_dir.rmdir() 
                                except OSError:
                                    pass
                        except Exception as e:
                            log(f"   ❌ Migration error for {sanitized_channel_name}: {e}")

                    # Folder Creation (Delay until link check)
                    channel_dir = target_root / sanitized_channel_name
                    sanitized_title = sanitize(meta["title"])
                    folder_name = f"{meta['published']} - {sanitized_title}"
                    video_dir = channel_dir / folder_name
                    
                    host_path_root = Path("/mnt/user/tubearchives/bp")
                    host_source_path = host_path_root / video_file.relative_to(SOURCE_DIR)
                    dest_file = video_dir / f"video{video_file.suffix}"
                    
                    try:
                        link_success = False
                        if dest_file.exists():
                            if dest_file.is_symlink():
                                current_target = Path(os.readlink(dest_file))
                                if current_target.resolve() != host_source_path.resolve():
                                    dest_file.unlink()
                                    os.symlink(host_source_path, dest_file)
                                    log(f"   [FIX] Relinked: {folder_name}")
                                    new_links += 1
                                    link_success = True
                                else:
                                    verified_links += 1
                                    link_success = True
                        else:
                            # Create directories ONLY NOW
                            channel_dir.mkdir(parents=True, exist_ok=True)
                            video_dir.mkdir(parents=True, exist_ok=True)
                            os.symlink(host_source_path, dest_file)
                            log(f"   [NEW] Linked: {folder_name}")
                            new_links += 1
                            link_success = True
                    except Exception as e:
                        log(f"   ❌ Link error for {folder_name}: {e}")
                    
                    # Store in database
                    conn.execute("""
                        INSERT OR REPLACE INTO videos 
                        (video_id, title, channel, published, symlink, status)
                        VALUES (?, ?, ?, ?, ?, 'linked')
                    """, (video_id, meta["title"], meta["channel_name"], 
                            meta["published"], str(dest_file)))
                    
                    processed_videos.append({
                        "video_id": video_id,
                        "title": meta["title"],
                        "channel": meta["channel_name"],
                        "published": meta["published"],
                        "symlink": str(dest_file)
                    })
        except Exception as e:
            conn.rollback()
            return str(e)
        
        conn.commit()
            
    log(f"✅ Scan complete. Processed {len(processed_videos)} videos.")
    log(f"   - New/Fixed Links: {new_links}")
    log(f"   - Verified Links:  {verified_links}")
    return None

def scheduler():
    log(f"🕒 Background scheduler started. Scanning every {SCAN_INTERVAL} minutes.")
    while True:
        log("🔄 Running scheduled scan...")
        process_videos()
        time.sleep(SCAN_INTERVAL * 60)

# Flask routes

@app.before_request
def limit_remote_addr():
    # Skip check for local requests if needed, but generally good to enforce
    client_ip = request.remote_addr
    try:
        ip_obj = ipaddress.ip_address(client_ip)
        allowed = False
        for allowed_ip in ALLOWED_IPS:
            if not allowed_ip: continue
            if "/" in allowed_ip:
                if ip_obj in ipaddress.ip_network(allowed_ip, strict=False):
                    allowed = True
                    break
            else:
                if ip_obj == ipaddress.ip_address(allowed_ip):
                    allowed = True
                    break
        if not allowed:
            log(f"⛔ Access denied for IP: {client_ip}")
            abort(403)
    except ValueError as e:
        log(f"⛔ Invalid IP format: {client_ip}, Error: {e}")
        abort(403)

def check_auth(username, password):
    """Checks whether a username/password combination is valid."""
    return username == UI_USERNAME and password == UI_PASSWORD

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            if request.path.startswith('/api/'):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated

@app.route("/api/auth/login", methods=["POST"])
def api_login():
    data = request.json
    username = data.get("username")
    password = data.get("password")
    
    if check_auth(username, password):
        session['logged_in'] = True
        session['username'] = username
        return jsonify({"success": True})
    return jsonify({"error": "Invalid credentials"}), 401

@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"success": True})

@app.route("/api/auth/status")
def api_auth_status():
    return jsonify({
        "logged_in": session.get('logged_in', False),
        "username": session.get('username')
    })

@app.route("/login")
def login_page():
    return send_from_directory(app.static_folder, 'login/index.html')

@app.route("/")
@requires_auth
def index():
    return send_from_directory(app.static_folder, 'index.html')

@app.route('/<path:path>')
def serve_static(path):
    # Only serve if file exists in static folder
    return send_from_directory(app.static_folder, path)

@app.route("/api/status")
@requires_auth
def api_status():
    with get_db() as conn:
        # Get all videos from DB
        videos = []
        for row in conn.execute("SELECT * FROM videos ORDER BY channel, published DESC"):
            videos.append({
                "video_id": row["video_id"],
                "title": row["title"],
                "channel": row["channel"],
                "published": row["published"],
                "symlink": row["symlink"],
                "status": row["status"]
            })
        
        # Calculate stats
        total = len(videos)
        linked = sum(1 for v in videos if v["status"] == "linked")
        missing = sum(1 for v in videos if v["status"] == "missing")
        
        return jsonify({
            "total_videos": total,
            "verified_links": linked,
            "missing_count": missing,
            "videos": videos
        })

@app.route("/api/logs")
@requires_auth
def api_logs():
    start = request.args.get('start', 0, type=int)
    with log_lock:
        return jsonify({
            "logs": log_buffer[start:],
            "next_index": len(log_buffer)
        })

@app.route("/api/scan", methods=["POST"])
@requires_auth
def api_scan():
    # Run in background to avoid blocking
    threading.Thread(target=process_videos).start()
    return jsonify({"status": "started"})

@app.route("/api/cleanup", methods=["POST"])
@requires_auth
def api_cleanup():
    threading.Thread(target=cleanup_old_folders).start()
    return jsonify({"status": "started"})

@app.route("/api/check-orphans", methods=["POST"])
@requires_auth
def api_check_orphans():
    orphaned = check_orphaned_links()
    return jsonify({"status": "complete", "orphaned": orphaned, "count": len(orphaned)})

@app.route("/transcode")
@requires_auth
def transcode_page():
    return send_from_directory(app.static_folder, 'transcode/index.html')

@app.route("/api/transcode/videos")
@requires_auth
def api_transcode_videos():
    """Get all videos that need transcoding."""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 100, type=int)
    offset = (page - 1) * per_page
    
    with get_db() as conn:
        # Get total count
        total = conn.execute("SELECT COUNT(*) as count FROM videos WHERE status = 'missing'").fetchone()['count']
        
        videos = []
        for row in conn.execute(
            "SELECT * FROM videos WHERE status = 'missing' LIMIT ? OFFSET ?",
            (per_page, offset)
        ):
            videos.append({
                "video_id": row["video_id"],
                "title": row["title"],
                "channel": row["channel"],
                "published": row["published"],
                "symlink": row["symlink"]
            })
        
        return jsonify({
            "videos": videos,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page
        })

@app.route("/api/transcode/start", methods=["POST"])
@requires_auth
def api_transcode_start():
    """Start transcoding a video."""
    data = request.get_json()
    filepath = data.get('filepath')
    
    if not filepath:
        return jsonify({"error": "No filepath provided"}), 400
    
    encoder = detect_encoder()
    tlog(f"🖥️  Selected encoder: {encoder}")
    
    # Run in background
    def run_transcode():
        transcode_video(filepath, encoder)
    
    threading.Thread(target=run_transcode).start()
    return jsonify({"message": "Transcode started", "encoder": encoder})

@app.route("/api/transcode/logs")
@requires_auth
def api_transcode_logs():
    """Get transcode logs."""
    start = request.args.get('start', 0, type=int)
    with transcode_log_lock:
        return jsonify({
            "logs": transcode_log_buffer[start:],
            "next_index": len(transcode_log_buffer)
        })

# Global Scan State
SCAN_CACHE = {
    "status": "idle", # idle, scanning, done
    "results": None,
    "last_run": None 
}
SCAN_THREAD = None

@app.route("/api/recovery/scan", methods=["POST"])
@requires_auth
def api_recovery_scan():
    global SCAN_THREAD
    
    if SCAN_CACHE["status"] == "scanning":
        return jsonify({"status": "running", "message": "Scan already in progress"}), 202

    def run_scan_async():
        global SCAN_CACHE
        # SCAN_CACHE["status"] = "scanning" # Already set in main thread
        SCAN_CACHE["results"] = None
        try:
            results = scan_for_unindexed_videos()
            SCAN_CACHE["results"] = results
            SCAN_CACHE["status"] = "done"
            SCAN_CACHE["last_run"] = datetime.now().isoformat()
        except Exception as e:
            SCAN_CACHE["status"] = "error"
            SCAN_CACHE["results"] = str(e)
            log(f"❌ Async scan failed: {e}")

    # Set status IMMEDIATELY to avoid race condition where poll sees "idle"
    SCAN_CACHE["status"] = "scanning"
    SCAN_THREAD = threading.Thread(target=run_scan_async)
    SCAN_THREAD.start()
    
    return jsonify({"status": "started", "message": "Background scan started"}), 202

@app.route("/api/recovery/poll", methods=["GET"])
@requires_auth
def api_recovery_poll():
    return jsonify(SCAN_CACHE)

@app.route("/api/recovery/start", methods=["POST"])
@requires_auth
def api_recovery_start():
    data = request.get_json()
    filepath = data.get('filepath')
    
    if not filepath:
        return jsonify({"error": "No filepath provided"}), 400
        
    # Run synchronously to give user immediate feedback per file
    success, msg = recover_video_metadata(filepath)
    log(f"Recovery Result for {filepath}: {msg}")
    
    return jsonify({
        "message": msg, 
        "success": success,
        "status": "completed" if success else "failed" 
    })

@app.route("/api/recovery/delete-batch", methods=["POST"])
@requires_auth
def api_recovery_delete_batch():
    data = request.json
    paths = data.get("filepaths", [])
    destruct = data.get("destruct_mode", False)
    
    success_count = 0
    fail_count = 0
    errors = []
    
    log(f"🔥 Batch Delete started. Items: {len(paths)}, Destruct: {destruct}")
    
    # Refresh metadata for destruct mode
    video_map = {}
    if destruct:
        # Optimization: only fetch if destruct is on
        video_map = fetch_all_metadata() 
    
    for path in paths:
        try:
            # 1. Destruct Source if enabled
            p = Path(path)
            if destruct:
                source_deleted = False
                # Try to find video_id from DB first (most reliable for library files)
                vid_id = None
                with get_db() as conn:
                    row = conn.execute("SELECT video_id FROM videos WHERE symlink = ?", (path,)).fetchone()
                    if row:
                        vid_id = row['video_id']
                
                # Fallback: Extraction from filename
                if not vid_id:
                    vid_id = extract_id_from_filename(p.name)

                if vid_id:
                    meta = video_map.get(vid_id)
                    if meta:
                        source_path_raw = meta.get('filesystem_path')
                        if source_path_raw:
                            source_path = Path(source_path_raw)
                            if source_path.exists():
                                try:
                                    source_path.unlink()
                                    log(f"☢️ [DESTRUCT] Deleted source: {source_path}")
                                    source_deleted = True
                                except Exception as se:
                                    log(f"❌ [DESTRUCT] Failed to delete source {source_path}: {se}")
                                    raise Exception(f"Source deletion failed: {se}")
                
                if not source_deleted:
                    log(f"⚠️ [DESTRUCT] Source file not found for: {path} (ID: {vid_id or 'unknown'})")

            # 2. Delete Target
            if p.exists():
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()
                log(f"🗑️ Deleted target: {path}")
                
                # 3. Cleanup empty parent
                parent = p.parent
                if parent != Path(TARGET_DIR) and parent != Path(HIDDEN_DIR):
                    if parent.exists() and not any(parent.iterdir()):
                        try:
                            parent.rmdir()
                            log(f"🧹 [CLEANUP] Removed empty folder: {parent}")
                        except: pass

            success_count += 1
        except Exception as e:
            err_msg = str(e)
            log(f"❌ Failed to delete {path}: {err_msg}")
            fail_count += 1
            if err_msg not in errors:
                errors.append(err_msg)
            
    return jsonify({
        "success_count": success_count, 
        "fail_count": fail_count,
        "errors": errors[:5]
    })

@app.route("/api/system/check-permissions", methods=["GET"])
@requires_auth
def api_check_permissions():
    results = {}
    test_dirs = [
        ("source", SOURCE_DIR), 
        ("target", TARGET_DIR), 
        ("hidden", HIDDEN_DIR), 
        ("data", DATA_DIR)
    ]
    
    log("🔍 Running System Permission Check...")
    
    for name, path in test_dirs:
        if not path:
            results[name] = {"status": "unset", "writeable": False}
            continue
            
        p = Path(path)
        if not p.exists():
            results[name] = {"status": "missing", "writeable": False, "message": "Directory does not exist"}
            log(f"  ❌ {name} ({path}): MISSING")
            continue

        test_file = p / f".write_test_{os.getpid()}"
        try:
            # Try to write
            log(f"  🧪 Testing write on {name}...")
            if test_file.exists(): test_file.unlink() # Cleanup old failure
            with open(test_file, "w") as f:
                f.write("test")
            # Try to delete
            test_file.unlink()
            
            results[name] = {"status": "ok", "writeable": True}
            log(f"  ✅ {name} ({path}): WRITEABLE")
        except Exception as e:
            msg = str(e)
            results[name] = {"status": "error", "writeable": False, "message": msg}
            log(f"  ❌ {name} ({path}): READ-ONLY or PERMISSION DENIED - {msg}")
            # Identify if it is literally "Read-only file system"
            if "Read-only file system" in msg:
                log(f"  🚨 POSITIVE R/O MOUNT DETECTED for {name}")
            
    return jsonify(results)

@app.route("/api/recovery/delete", methods=["POST"])
@requires_auth
def api_recovery_delete():
    data = request.get_json()
    filepath = data.get('filepath')
    
    if not filepath:
        return jsonify({"error": "No filepath provided"}), 400
        
    p = Path(filepath)
    if not p.exists() or not p.is_file():
        return jsonify({"error": "File not found"}), 404
        
    # Safety Check: Never delete anything from SOURCE_DIR UNLESS it is redundancy check or lost media decision
    # (Actually user might want to delete lost media)
    # Let's refine logical check:
    # If it is in Lost Media table, allow delete.
    # If it is Redundant (Target check), allow delete.
    
    vid_id = extract_id_from_filename(p.name)
    
    # Check if this ID is in lost_media
    is_lost = False
    if vid_id:
        with get_db() as conn:
            row = conn.execute("SELECT 1 FROM lost_media WHERE video_id = ?", (vid_id,)).fetchone()
            if row: is_lost = True

    # If it's source dir and NOT lost media, we might want to be careful.
    # But user clicked "Delete" on "Redundant" tab potentially?
    # Actually the "Redundant" tab only targets files in TARGET_DIR usually? 
    # Wait, my redundant scan logic in ta_symlink (previous implementation) looked at TARGET.
    # But if Unindexed files are in SOURCE, and user wants to delete them?
    # Let's allow it but log it.
    
    try:
        p.unlink()
        
        # Cleanup Lost Media Table
        if vid_id:
            with get_db() as conn:
                conn.execute("DELETE FROM lost_media WHERE video_id = ?", (vid_id,))
                conn.commit()

        log(f"🗑️ Deleted file: {filepath}")
        return jsonify({"success": True, "message": "File deleted"})
    except Exception as e:
        log(f"❌ Delete failed: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/channels", methods=["GET"])
@requires_auth
def api_get_channels():
    with get_db() as conn:
        rows = conn.execute("SELECT DISTINCT channel FROM videos WHERE channel IS NOT NULL ORDER BY channel ASC").fetchall()
        channels = [row['channel'] for row in rows if row['channel']]
    return jsonify(channels)

@app.route("/api/channels/videos", methods=["GET"])
@requires_auth
def api_get_channel_videos():
    channel_name = request.args.get('channel')
    if not channel_name:
        return jsonify({"error": "No channel name provided"}), 400
        
    # Refresh metadata to get filesystem paths
    video_map = fetch_all_metadata()
    
    with get_db() as conn:
        rows = conn.execute("SELECT video_id, title, symlink FROM videos WHERE channel = ? ORDER BY published DESC", (channel_name,)).fetchall()
        
    videos = []
    for row in rows:
        vid_id = row['video_id']
        meta = video_map.get(vid_id, {})
        videos.append({
            "video_id": vid_id,
            "title": row['title'],
            "path": row['symlink'],
            "filename": Path(row['symlink']).name if row['symlink'] else meta.get('title'),
            "source_path": meta.get('filesystem_path'),
            "ta_source": meta.get('channel_name', channel_name)
        })
        
    return jsonify(videos)

@app.route('/api/recovery/force', methods=['POST'])
@requires_auth
def api_recovery_force():
    data = request.json
    filepath = data.get('filepath')
    if not filepath:
        return jsonify({"error": "No filepath provided"}), 400
        
    log(f"💪 Force Importing (Lost Media): {Path(filepath).name}")
    
    src_path = Path(filepath).resolve()
    if not src_path.exists():
        return jsonify({"error": "File not found"}), 404
        
    vid_id = extract_id_from_filename(src_path.name)
    if not vid_id:
         return jsonify({"error": "Could not extract ID"}), 400

    # 1. Generate Offline Metadata
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = IMPORT_DIR / f"{src_path.stem}.info.json"
    
    # minimal metadata
    offline_meta = {
        "id": vid_id,
        "title": f"Offline Import - {src_path.stem}",
        "uploader": "Unknown (Lost Media)",
        "upload_date": datetime.now().strftime("%Y%m%d"),
        "description": "Imported via TubeSorterr Force Import (Lost Media)",
        "webpage_url": f"https://www.youtube.com/watch?v={vid_id}",
        "view_count": 0,
        "like_count": 0,
        "duration": 0
    }
    
    import json
    try:
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(offline_meta, f, indent=4)
        log("   📝 Generated offline metadata.")
        
        # 2. Link/Copy Video
        dest_video = IMPORT_DIR / src_path.name
        if dest_video.exists(): dest_video.unlink()
        try:
            os.link(src_path, dest_video)
            log("   🔗 Hardlinked video.")
        except OSError:
            shutil.copy2(src_path, dest_video)
            log("   ©️ Copied video.")
            
        # 3. Clean up lost_media table
        with get_db() as conn:
            conn.execute("DELETE FROM lost_media WHERE video_id = ?", (vid_id,))
            conn.commit()
            
        return jsonify({"success": True, "message": "Force import successful"})
        
    except Exception as e:
        log(f"   ❌ Force import failed: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/hidden", methods=["GET"])
@requires_auth
def api_get_hidden():
    with get_db() as conn:
        rows = conn.execute("SELECT channel_name FROM hidden_channels ORDER BY channel_name").fetchall()
        channels = [row["channel_name"] for row in rows]
    return jsonify({"channels": channels})

@app.route("/api/hidden", methods=["POST"])
@requires_auth
def api_add_hidden():
    data = request.json
    channel = data.get("channel")
    if not channel:
        return jsonify({"error": "No channel name provided"}), 400
    
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO hidden_channels (channel_name) VALUES (?)", (channel,))
        conn.commit()
    
    log(f"🙈 Added to hidden list: {channel}")
    return jsonify({"success": True})

@app.route("/api/hidden", methods=["DELETE"])
@requires_auth
def api_remove_hidden():
    data = request.json
    channel = data.get("channel")
    if not channel:
        return jsonify({"error": "No channel name provided"}), 400
        
    with get_db() as conn:
        conn.execute("DELETE FROM hidden_channels WHERE channel_name = ?", (channel,))
        conn.commit()
    
    log(f"👁️ Removed from hidden list: {channel}")
    return jsonify({"success": True})
    
if __name__ == "__main__":
    # Start scheduler in background thread
    thread = threading.Thread(target=scheduler, daemon=True)
    thread.start()
    
    app.run(host="0.0.0.0", port=5000)
