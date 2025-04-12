# TA Organizer

📂 Automatically organizes TubeArchivist downloads into human-readable folder structures using symlinks and metadata.

---

## ✨ Features

- Organizes downloaded videos by:
  - Channel name
  - Publish date
  - Video title
- Uses TubeArchivist’s API to fetch video metadata
- Creates symbolic links — leaving original files untouched
- Dockerized for easy deployment
- Supports Unraid and other Docker environments

---

## 🚀 Getting Started

### Requirements

- Python 3.11+
- Docker + Docker Compose
- A running [TubeArchivist](https://github.com/bbilly1/tubearchivist) instance

---

## 🔧 Configuration

Create a `.env` file in the root of the repo:

```env
# Required
API_TOKEN=your_api_token_here

# Optional overrides
API_URL=http://localhost:8457/api
VIDEO_URL=http://localhost:8457/video
```

---

## 🐳 Docker Usage

1. Clone this repo and navigate into it:

```bash
git clone https://github.com/Salpertio/ta-organizer.git
cd ta-organizer
```

2. Edit the `docker-compose.yml` to point to your source/target folders:

```yaml
volumes:
  - /your/path/to/downloaded/videos:/app/source:ro
  - /your/target/path:/app/target
```

3. Run the container:

```bash
docker compose up --build
```

---

## 📁 Example Output Structure

```
/app/target/
├── Channel Name/
│   ├── 2025-01-01 - Example Title/
│   │   └── video.mp4 (symlink)
```

---

## 🧰 Troubleshooting

If the script silently skips some files, ensure:

- The video exists in TubeArchivist
- Metadata is accessible via the API
- The `video_id` matches the filename stem

---

## 🛡️ License

MIT — do whatever you want, just give credit if you use it.

---

## 🙌 Credits

Built by [Salpertio](https://github.com/Salpertio) to tame the TubeArchivist folder jungle 🌴
