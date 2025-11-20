
from pathlib import Path
import os
import requests
import re
import sys
import threading
import time
from flask import Flask, jsonify, render_template_string, request

# Load config from environment variables
API_URL = os.getenv("API_URL", "http://localhost:8457/api")
VIDEO_URL = os.getenv("VIDEO_URL", "http://localhost:8457/video/")
API_TOKEN = os.getenv("API_TOKEN", "")
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", 60)) # Default 60 minutes
SOURCE_DIR = Path("/app/source")
TARGET_DIR = Path("/app/target")
HEADERS = {"Authorization": f"Token {API_TOKEN}"}

app = Flask(__name__)

processed_videos = []

# Utility functions
def sanitize(text):
    text = text.encode("ascii", "ignore").decode()
    text = re.sub(r'[\/:*?"<>|]', "_", text)
    return text.strip()

def fetch_all_metadata():
    print("📥 Fetching all video metadata...", flush=True)
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
                # Fallback if no pagination info, just stop if empty data (handled above) or arbitrary limit?
                # If we got data but no pagination, maybe it's a single page result?
                # But we loop until no data.
                pass

            print(f"   - Page {page} fetched. Total videos so far: {len(video_map)}", flush=True)
            page += 1
            
        except Exception as e:
            print(f"❌ Error fetching page {page}: {e}", flush=True)
            # If a page fails, maybe we should stop or retry? For now, let's stop to avoid infinite loops on auth error
            break
            
    print(f"✅ Metadata fetch complete. Found {len(video_map)} videos.", flush=True)
    return video_map

def cleanup_old_folders():
    """
    Scans TARGET_DIR for folders containing '+00:00'.
    Safely deletes them ONLY if they contain no real files (only symlinks or empty).
    """
    print("🧹 Starting cleanup. Scanning ONLY for folders containing '+00:00'...", flush=True)
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
                        print(f"   [DELETED] {video_dir.name}", flush=True)
                        cleaned_count += 1
                    except Exception as e:
                        print(f"   ❌ Failed to delete {video_dir.name}: {e}", flush=True)
                else:
                    print(f"   ⚠️ SKIPPING {video_dir.name} - {reason}", flush=True)
                    skipped_count += 1
                    
    print(f"🧹 Cleanup complete. Removed: {cleaned_count}, Skipped: {skipped_count}", flush=True)

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
    
    try:
        for channel_path in SOURCE_DIR.iterdir():
            if not channel_path.is_dir():
                continue
            for video_file in channel_path.glob("*.*"):
                video_id = video_file.stem
                
                # 2. Lookup in local map
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
                                print(f"   [FIX] Relinked: {folder_name}", flush=True)
                                new_links += 1
                            else:
                                verified_links += 1
                    else:
                        os.symlink(host_source_path, dest_file)
                        print(f"   [NEW] Linked: {folder_name}", flush=True)
                        new_links += 1
                except Exception:
                    pass
                processed_videos.append({
                    "video_id": video_id,
                    "title": meta["title"],
                    "channel": meta["channel_name"],
                    "published": meta["published"],
                    "symlink": str(dest_file)
                })
    except Exception as e:
        return str(e)
        
    print(f"✅ Scan complete. Processed {len(processed_videos)} videos.", flush=True)
    print(f"   - New/Fixed Links: {new_links}", flush=True)
    print(f"   - Verified Links:  {verified_links}", flush=True)
    return None

def scheduler():
    print(f"🕒 Background scheduler started. Scanning every {SCAN_INTERVAL} minutes.", flush=True)
    while True:
        print("🔄 Running scheduled scan...", flush=True)
        process_videos()
        time.sleep(SCAN_INTERVAL * 60)

# Flask routes
@app.route("/")
def index():
    return render_template_string('''
    <html>
    <head><title>TA Organizerr</title></head>
    <body>
        <h1>TA Organizerr</h1>
        <form method="post" action="/process">
            <button type="submit">Process Videos</button>
        </form>
        <h2>Processed Videos</h2>
        <ul>
        {% for v in videos %}
            <li>{{v.published}} - {{v.title}} ({{v.channel}}) <br>Symlink: {{v.symlink}}</li>
        {% endfor %}
        </ul>
    </body>
    </html>
    ''', videos=processed_videos)

@app.route("/process", methods=["POST"])
def process():
    error = process_videos()
    if error:
        return f"Error: {error}", 500
    return render_template_string('''
    <html>
    <head><title>TA Organizerr</title></head>
    <body>
        <h1>TA Organizerr</h1>
        <form method="post" action="/process">
            <button type="submit">Process Videos</button>
        </form>
        <h2>Processed Videos</h2>
        <ul>
        {% for v in videos %}
            <li>{{v.published}} - {{v.title}} ({{v.channel}}) <br>Symlink: {{v.symlink}}</li>
        {% endfor %}
        </ul>
    </body>
    </html>
    ''', videos=processed_videos)

@app.route("/api/videos")
def api_videos():
    return jsonify(processed_videos)

if __name__ == "__main__":
    # Start scheduler in background thread
    thread = threading.Thread(target=scheduler, daemon=True)
    thread.start()
    
    app.run(host="0.0.0.0", port=5000)
