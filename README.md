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
- Lets an authenticated administrator watch or listen to recovery candidates
  in the browser before deciding how to classify or recover them.
- Reconciles tracked records, organized links, source media, and unindexed
  real files on a dedicated page before declaring a broken link truly missing.
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
- FFmpeg and ffprobe, if using transcoding or Listen only recovery previews

## Configuration

Copy the example configuration, then edit it for your deployment:

```bash
cp .env.example .env
```

At minimum, set the TubeArchivist connection and the host-visible source path:

```env
API_TOKEN=your_tubearchivist_api_token
API_URL=http://your-tubearchivist-host:8457/api

# Must be the same host directory mounted at /app/source in docker-compose.yml.
HOST_SOURCE_BASE=/absolute/host/path/to/tubearchivist/downloads
```

`API_URL` must be reachable from inside the container; `localhost` normally
refers to TubeSorter itself, not the TubeArchivist host.

Common optional settings:

```env
SCAN_INTERVAL=60
ALLOWED_IPS=127.0.0.1,192.168.1.0/24,10.0.0.0/8,172.16.0.0/12

PUID=99
PGID=100

HOST=0.0.0.0
PORT=5000
HOST_PORT=8002

# Set to true only when serving over HTTPS.
SESSION_COOKIE_SECURE=false

# Optional safety/resource caps.
MAX_METADATA_PAGES=10000
MAX_CONCURRENT_TRANSCODES=1
```

Compose reads these values from `.env`; values in the shell can override them.
Compose preserves the project's Unraid-compatible `PUID`/`PGID` default of
`99:100`; the bare image defaults to `1000:1000` when those variables are not
passed. Keep `HOST=0.0.0.0` for a port-published container. `HOST_PORT` is the
port opened on the host, while `PORT` is the port Gunicorn listens on inside
the container.

`HOST_SOURCE_BASE` is intentionally required for Compose and is used for both
the `/app/source` mount and generated symlink targets. TubeSorter rejects a
relative value at startup. A wrong absolute value can still point at the wrong
archive, so verify it before scanning. Bare-Docker/local runs temporarily retain the historical
`/mnt/user/tubearchives/bp` fallback and log a warning; set the value explicitly
instead, because that fallback will be removed in a future breaking release.

## Docker Usage

The supplied Compose file mounts `HOST_SOURCE_BASE` at `/app/source`. Edit the
remaining storage paths if you do not want project-local directories:

```yaml
services:
  tubesorter:
    build: .
    ports:
      - "${HOST_PORT:-8002}:${PORT:-5000}"
    volumes:
      - /path/to/tubesorter/target:/app/target
      - /path/to/tubesorter/hidden:/app/hidden
      - /path/to/tubesorter/data:/app/data
      - /path/to/tubearchivist/import:/app/import
```

Before starting, verify the fully resolved configuration:

```bash
docker compose config
```

This command fails when `HOST_SOURCE_BASE` is missing. Check that its resolved
value is the intended archive path, and that the resolved `PUID`/`PGID`,
listener ports, IP ranges, and secure-cookie setting are what you intended.

Then build and start the service:

```bash
docker compose up --build
```

Open `http://localhost:8002` when using the default `HOST_PORT`, or use the host
port you configured. On first launch, the app redirects to `/setup` to create
the admin account. The password hash and generated Flask secret are stored in
`/app/data/videos.db`.

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

Channel names that cannot be represented safely as-is receive a stable short
digest suffix. On the first successful scan after this upgrade, TubeSorter
creates and verifies the new link before retiring older generated links for the
same video ID. Real files under target/hidden are never removed by that layout
migration.

## Operations

- The scheduler runs a full scan every `SCAN_INTERVAL` minutes.
- The dashboard can trigger an immediate scan.
- Duplicate scans are skipped while one is already running.
- Maintenance can remove empty folders and zero-byte invalid `video.*` files.
- Orphan checks scan both target and hidden folders recursively.
- Recovery can stage files plus `.info.json` metadata for TubeArchivist import.
- Advanced Recovery can preview an allowed archive file without changing it.
  Preview requests are authenticated, confined to configured media roots, and
  streamed with byte-range support. Watching the original still depends on the
  browser's container and codec support. **Listen only** provides a compatibility
  fallback by converting the first audio track to a non-seekable MP3 stream with
  FFmpeg; one compatibility stream can run at a time.
- Link Reconciliation classifies healthy links, exact-ID repairs, ambiguous
  matches, invalid media, real files where links were expected, and truly
  missing entries. Repair is limited to one case-sensitive exact video-ID match;
  similar titles and filenames are never used for automatic repair. Candidates
  are checked as non-empty regular media files with `ffprobe` and can be watched
  or heard before a decision.
- Transcoding replaces the resolved source media file in place after FFmpeg
  completes successfully. It is limited to MP4, Matroska (`.mkv`), and MOV
  containers. It maps every input stream, copies metadata/chapters and
  thumbnails, converts all audio to AAC, and converts the single primary video
  stream to H.264. Ambiguous multi-primary-video inputs and probe failures are
  rejected without replacing the source. Back up irreplaceable media before
  using it.

### Destructive operations

Normal organization creates and moves symlinks; it does not move the source
archive. Recovery may hardlink or copy media into `/app/import`.
When a hardlink is possible, the staged import and archive share an inode until
the importer removes or moves its entry. In-place content writes, `chmod`, or
`chown` through either path affect both names during that window; use separate
filesystems if you require copy isolation. The entrypoint never recursively
changes ownership in the shared import tree.

Every delete action permanently removes the selected filesystem entry. Destruct
mode goes further: for each selected entry, it attempts to resolve and delete the
matched source archive file before removing the target entry. There is no recycle
bin and the filesystem and SQLite changes are not one atomic transaction, so a
partially successful batch is possible. Confirm the displayed paths, review the
returned per-item errors, keep a current backup, and test with one disposable
video before running a large batch.

Link Reconciliation has narrower guarded actions. **Remove Broken Link** accepts
only a currently broken symlink with one exact database owner, removes that link
and row, and refuses a real file. **Forget Stale Record** changes SQLite only.
**Adopt Existing Real File** records the existing file in place; it does not move
or remove the media. Every reconciliation mutation checks the current inventory
token and revalidates paths and media at action time. Successful mutations write
an audit log entry and trigger an immediate inventory refresh.

The maintenance repair removes only zero-byte invalid `video.*` files, but it is
still wise to stop the service and snapshot `/app/data` before upgrades or bulk
maintenance. The SQLite database contains the admin password hash, Flask signing
secret, hidden channel list, and scan state, so protect backups accordingly.

## Development and Verification

Create a virtual environment and install the backend dependencies:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

Run the backend tests:

```bash
python -m unittest discover -s tests -v
```

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

Production-style configuration and startup smoke check:

```bash
docker compose config
docker compose up -d --build
docker compose ps
curl --fail http://127.0.0.1:8002/api/auth/setup-status
```

Use your configured host port instead of `8002` when you override `HOST_PORT`.

The publish workflow runs the backend unit tests before it builds and pushes an
image. A failed test prevents publication.

## Troubleshooting

- If scans link no files, confirm the video IDs in filenames match
  TubeArchivist metadata.
- If `docker compose config` reports that `HOST_SOURCE_BASE` is required, copy
  `.env.example` to `.env` and set it to the source mount's absolute host path.
- If symlinks look broken inside the container but work on the host, verify
  `HOST_SOURCE_BASE` and `/app/source` point at the same archive tree.
- If the container is unhealthy after changing `ALLOWED_IPS`, retain
  `127.0.0.1`; the container healthcheck connects over loopback.
- If login works over HTTP but not through HTTPS, set
  `SESSION_COOKIE_SECURE=true` only behind a TLS-terminating proxy.
- If the UI lacks icons or fonts, check the browser console for CSP violations.
- If transcoding fails, confirm FFmpeg can read the translated source path and
  the source volume is writable.
