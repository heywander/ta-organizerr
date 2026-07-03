# TubeSorter

TubeSorter is a self-hosted companion app for TubeArchivist. It builds a
human-readable media library from TubeArchivist downloads by creating symlinks
without moving the original archive files.

The app includes a Flask API, an Astro/Svelte dashboard, SQLite state, first-run
admin setup, scheduled scans, maintenance tools, recovery tools, hidden-channel
management, and optional FFmpeg transcoding.

## Features

- Organizes videos by channel, publish date, and title.
- Creates symlinks in `/app/target` while leaving source downloads in place.
- Keeps hidden channels in `/app/hidden`.
- Separates livestreams under a `#streams` folder.
- Fetches metadata from the TubeArchivist API.
- Tracks linked, missing, and error rows in SQLite.
- Provides dashboard stats, logs, scan controls, and channel filters.
- Checks and repairs broken symlinks and empty folders.
- Scans for unindexed, redundant, rescue, and lost media.
- Stages recovered videos and metadata into `/app/import`.
- Can transcode media to H.264/AAC with FFmpeg.
- Supports Docker and Unraid-style `PUID`/`PGID` ownership.

## Requirements

- Docker and Docker Compose
- A running TubeArchivist instance
- A TubeArchivist API token

For local development without Docker:

- Python 3.11+
- Bun, for building the Astro/Svelte UI
- FFmpeg and ffprobe, if using transcoding

## Configuration

Create a `.env` file in the repository root. At minimum, set:

```env
API_TOKEN=your_tubearchivist_api_token
API_URL=http://your-tubearchivist-host:8457/api
```

Common optional settings:

```env
VIDEO_URL=http://your-tubearchivist-host:8457/video/
SCAN_INTERVAL=60
ALLOWED_IPS=127.0.0.1,192.168.1.0/24

SOURCE_DIR=/app/source
TARGET_DIR=/app/target
HIDDEN_DIR=/app/hidden
IMPORT_DIR=/app/import
DATA_DIR=/app/data

# Host-side source path to write into symlink targets.
# Set this to the path your host/media clients should see.
HOST_SOURCE_BASE=/mnt/user/tubearchives/bp

# Set to true only when serving over HTTPS.
SESSION_COOKIE_SECURE=false
```

`HOST_SOURCE_BASE` matters: TubeSorter creates symlinks that point at the host
archive path, while backend tools translate those paths back to `/app/source`
when they need to inspect or transcode the source file inside the container.

## Docker Usage

Edit `docker-compose.yml` so the volumes match your host paths:

```yaml
services:
  tubesorter:
    build: .
    ports:
      - "8002:5000"
    volumes:
      - /path/to/tubearchivist/downloads:/app/source
      - /path/to/tubesorter/target:/app/target
      - /path/to/tubesorter/hidden:/app/hidden
      - /path/to/tubesorter/data:/app/data
      - /path/to/tubearchivist/import:/app/import
```

Then run:

```bash
docker compose up --build
```

Open `http://localhost:8002`. On first launch, the app redirects to `/setup` to
create the admin account. The password hash and generated Flask secret are
stored in `/app/data/videos.db`.

## Output Layout

```text
/app/target/
  Channel Name/
    2025-01-01 - Example Title/
      video.mp4 -> /host/source/path/Channel/Example [video_id].mp4

/app/hidden/
  Hidden Channel/
    2025-01-01 - Hidden Video/
      video.mp4 -> /host/source/path/Hidden Channel/Hidden [video_id].mp4
```

Livestreams are placed below the channel folder:

```text
/app/target/
  Channel Name/
    #streams/
      2025-01-01 - Stream Title/
        video.mp4
```

## Operations

- The scheduler runs a full scan every `SCAN_INTERVAL` minutes.
- The dashboard can trigger an immediate scan.
- Duplicate scans are skipped while one is already running.
- Maintenance can remove empty folders and zero-byte invalid `video.*` files.
- Orphan checks scan both target and hidden folders recursively.
- Recovery can stage files plus `.info.json` metadata for TubeArchivist import.
- Destruct mode deletes both target entries and matched source archive files; use
  it only when you intend to permanently remove media.

## Development

Backend syntax check:

```bash
python3 -m compileall -q ta_organizerr
```

Frontend build:

```bash
cd ui
bun install --frozen-lockfile
bun run build
```

Docker build:

```bash
docker build -t tubesorter .
```

## Troubleshooting

- If scans link no files, confirm the video IDs in filenames match
  TubeArchivist metadata.
- If symlinks look broken inside the container but work on the host, verify
  `HOST_SOURCE_BASE` and `/app/source` point at the same archive tree.
- If login works over HTTP but not through HTTPS, set
  `SESSION_COOKIE_SECURE=true` only behind a TLS-terminating proxy.
- If the UI lacks icons or fonts, check the browser console for CSP violations.
- If transcoding fails, confirm FFmpeg can read the translated source path and
  the source volume is writable.
