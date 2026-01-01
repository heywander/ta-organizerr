from pathlib import Path
import os
import requests
import re
import sys
import threading
import time
import ipaddress
from flask import Flask, jsonify, render_template, request, abort

# Load config from environment variables
API_URL = os.getenv("API_URL", "http://localhost:8457/api")
VIDEO_URL = os.getenv("VIDEO_URL", "http://localhost:8457/video/")
API_TOKEN = os.getenv("API_TOKEN", "")
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", 60)) # Default 60 minutes
ALLOWED_IPS = [ip.strip() for ip in os.getenv("ALLOWED_IPS", "127.0.0.1").split(",")]
SOURCE_DIR = Path("/app/source")
TARGET_DIR = Path("/app/target")
HEADERS = {"Authorization": f"Token {API_TOKEN}"}

app = Flask(__name__)

# Database setup
import sqlite3
from contextlib import contextmanager

DB_PATH = Path("/app/data/videos.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                video_id TEXT PRIMARY KEY,
                title TEXT,
                channel TEXT,
                published TEXT,
                symlink TEXT,
                status TEXT,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

init_db()

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
                    "published": published
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
    Scans TARGET_DIR for folders containing '+00:00'.
    Safely deletes them ONLY if they contain no real files (only symlinks or empty).
    """
    log("🧹 Starting cleanup. Scanning ONLY for folders containing '+00:00'...")
    cleaned_count = 0
    skipped_count = 0
    
    if not TARGET_DIR.exists():
        return

    # Walk top-down
    for channel_dir in TARGET_DIR.iterdir():
        if not channel_dir.is_dir():
            continue
            
        for video_dir in channel_dir.iterdir():
            if not video_dir.is_dir():
                continue
                
            if "+00:00" in video_dir.name:
                # Check safety
                safe_to_delete = True
                reason = ""
                
                for item in video_dir.iterdir():
                    if not item.is_symlink():
                        # Found a real file! Unsafe!
                        safe_to_delete = False
                        reason = "Contains real files"
                        break
                
                if safe_to_delete:
                    try:
                        # Remove all symlinks first
                        for item in video_dir.iterdir():
                            item.unlink()
                        # Remove directory
                        video_dir.rmdir()
                        log(f"   [DELETED] {video_dir.name}")
                        cleaned_count += 1
                    except Exception as e:
                        log(f"   ❌ Failed to delete {video_dir.name}: {e}")
                else:
                    log(f"   ⚠️ SKIPPING {video_dir.name} - {reason}")
                    skipped_count += 1
                    
    log(f"🧹 Cleanup complete. Removed: {cleaned_count}, Skipped: {skipped_count}")

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

# Main logic

def process_videos():
    global processed_videos
    processed_videos = []
    
    # 1. Fetch all metadata first
    video_map = fetch_all_metadata()
    
    # 2. Run cleanup
    cleanup_old_folders()
    
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
                    channel_dir = TARGET_DIR / sanitized_channel_name
                    channel_dir.mkdir(parents=True, exist_ok=True)
                    sanitized_title = sanitize(meta["title"])
                    folder_name = f"{meta['published']} - {sanitized_title}"
                    video_dir = channel_dir / folder_name
                    video_dir.mkdir(parents=True, exist_ok=True)
                    actual_file = next(channel_path.glob(f"{video_id}.*"), None)
                    if not actual_file:
                        continue
                    host_path_root = Path("/mnt/user/tubearchives/bp")
                    host_source_path = host_path_root / actual_file.relative_to(SOURCE_DIR)
                    dest_file = video_dir / f"video{actual_file.suffix}"
                    try:
                        if dest_file.exists():
                            if dest_file.is_symlink():
                                current_target = Path(os.readlink(dest_file))
                                if current_target.resolve() != host_source_path.resolve():
                                    dest_file.unlink()
                                    os.symlink(host_source_path, dest_file)
                                    log(f"   [FIX] Relinked: {folder_name}")
                                    new_links += 1
                                else:
                                    verified_links += 1
                        else:
                            os.symlink(host_source_path, dest_file)
                            log(f"   [NEW] Linked: {folder_name}")
                            new_links += 1
                    except Exception:
                        pass
                    
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

@app.route("/")
def index():
    return render_template('dashboard.html')

@app.route("/api/status")
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
def api_logs():
    start = request.args.get('start', 0, type=int)
    with log_lock:
        return jsonify({
            "logs": log_buffer[start:],
            "next_index": len(log_buffer)
        })

@app.route("/api/scan", methods=["POST"])
def api_scan():
    # Run in background to avoid blocking
    threading.Thread(target=process_videos).start()
    return jsonify({"status": "started"})

@app.route("/api/cleanup", methods=["POST"])
def api_cleanup():
    threading.Thread(target=cleanup_old_folders).start()
    return jsonify({"status": "started"})

@app.route("/api/check-orphans", methods=["POST"])
def api_check_orphans():
    orphaned = check_orphaned_links()
    return jsonify({"status": "complete", "orphaned": orphaned, "count": len(orphaned)})

@app.route("/transcode")
def transcode_page():
    return render_template('transcoding.html')

@app.route("/api/transcode/videos")
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
def api_transcode_logs():
    """Get transcode logs."""
    start = request.args.get('start', 0, type=int)
    with transcode_log_lock:
        return jsonify({
            "logs": transcode_log_buffer[start:],
            "next_index": len(transcode_log_buffer)
        })

if __name__ == "__main__":
    # Start scheduler in background thread
    thread = threading.Thread(target=scheduler, daemon=True)
    thread.start()
    
    app.run(host="0.0.0.0", port=5000)
