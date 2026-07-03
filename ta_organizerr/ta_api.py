"""TubeArchivist API client."""
from __future__ import annotations

import requests

from .config import API_URL, HEADERS
from .logging_util import log


class MetadataFetchError(RuntimeError):
    """Raised when TubeArchivist metadata could not be fetched completely."""


def fetch_all_metadata() -> dict[str, dict]:
    """Walk the TA paginated video endpoint and return ``{video_id: metadata}``."""
    log("📥 Fetching all video metadata...")
    video_map: dict[str, dict] = {}
    page = 1
    while True:
        url = f"{API_URL}/video/?page={page}"
        try:
            response = requests.get(url, headers=HEADERS, timeout=30)
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            msg = f"Error fetching TubeArchivist metadata page {page}: {exc}"
            log(f"❌ {msg}")
            raise MetadataFetchError(msg) from exc

        if not isinstance(data, dict) or "data" not in data:
            msg = f"Unexpected TubeArchivist response on page {page}"
            log(f"❌ {msg}")
            raise MetadataFetchError(msg)

        if not data["data"]:
            break

        for video in data["data"]:
            vid_id = video.get("youtube_id") or video.get("_id")
            if not vid_id:
                continue
            title = video.get("title", "unknown_title")
            channel_info = video.get("channel", {}) or {}
            channel_name = (
                channel_info.get("channel_name")
                or channel_info.get("channel_title")
                or "Unknown Channel"
            )
            raw_date = video.get("published", "unknown_date") or "unknown_date"
            published = raw_date[:10] if len(raw_date) >= 10 else raw_date.replace("/", "-")
            video_map[vid_id] = {
                "title": title,
                "channel_name": channel_name,
                "published": published,
                "is_live": video.get("vid_type") == "streams",
                "filesystem_path": video.get("path") or video.get("filesystem_path"),
            }

        paginate = data.get("paginate")
        if paginate:
            current = paginate.get("current_page")
            last = paginate.get("last_page")
            if current is not None and last is not None and current >= last:
                break
        log(f"   - Page {page} fetched. Total videos so far: {len(video_map)}")
        page += 1

    log(f"✅ Metadata fetch complete. Found {len(video_map)} videos.")
    return video_map
