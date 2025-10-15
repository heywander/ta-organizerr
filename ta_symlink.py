
from pathlib import Path
import os
import requests
import re
import sys
from flask import Flask, jsonify, render_template_string, request

# Load config from environment variables
API_URL = os.getenv("API_URL", "http://localhost:8457/api")
VIDEO_URL = os.getenv("VIDEO_URL", "http://localhost:8457/video/")
API_TOKEN = os.getenv("API_TOKEN", "")
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

def fetch_video_metadata(video_id):
    url = f"{API_URL}/video/{video_id}/"
    try:
        response = requests.get(url, headers=HEADERS)
        response.raise_for_status()
        data = response.json()

        title = data.get("title", "unknown_title")
        channel_info = data.get("channel", {})
        channel_id = channel_info.get("channel_id", "unknown_channel")
        channel_name = channel_info.get("channel_name") or channel_info.get("channel_title") or "Unknown Channel"
        published = data.get("published", "unknown_date").replace("/", "-")

        return {
            "title": title,
            "channel_id": channel_id,
            "channel_name": channel_name,
            "published": published
        }
    except Exception as e:
        print(f"❌ Error fetching metadata for {video_id}: {e}", flush=True)
        return None

# Main logic

def process_videos():
    global processed_videos
    processed_videos = []
    try:
        for channel_path in SOURCE_DIR.iterdir():
            if not channel_path.is_dir():
                continue
            for video_file in channel_path.glob("*.*"):
                video_id = video_file.stem
                meta = fetch_video_metadata(video_id)
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
                    else:
                        os.symlink(host_source_path, dest_file)
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
    return None


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
    app.run(host="0.0.0.0", port=5000)
