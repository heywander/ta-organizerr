"""TubeArchivist API client."""
from __future__ import annotations

import requests

from .config import API_URL, HEADERS, MAX_METADATA_PAGES
from .logging_util import log


class MetadataFetchError(RuntimeError):
    """Raised when TubeArchivist metadata could not be fetched completely."""


def fetch_all_metadata() -> dict[str, dict]:
    """Walk the TA paginated video endpoint and return ``{video_id: metadata}``."""
    log("📥 Fetching all video metadata...")
    video_map: dict[str, dict] = {}
    seen_page_signatures: set[tuple[str, ...]] = set()
    page = 1
    while True:
        if page > MAX_METADATA_PAGES:
            msg = f"TubeArchivist pagination exceeded {MAX_METADATA_PAGES} pages"
            log(f"❌ {msg}")
            raise MetadataFetchError(msg)
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

        page_items = data["data"]
        if not isinstance(page_items, list):
            msg = f"Unexpected TubeArchivist data type on page {page}"
            log(f"❌ {msg}")
            raise MetadataFetchError(msg)
        paginate = data.get("paginate")
        is_last_page: bool | None = None
        if paginate is not None:
            if not isinstance(paginate, dict):
                msg = f"Unexpected TubeArchivist pagination data on page {page}"
                log(f"❌ {msg}")
                raise MetadataFetchError(msg)
            current = paginate.get("current_page")
            last = paginate.get("last_page")
            if current is not None and last is not None:
                try:
                    current_i = int(current)
                    last_i = int(last)
                    is_last_page = current_i >= last_i
                except (TypeError, ValueError) as exc:
                    msg = f"Invalid TubeArchivist pagination values on page {page}"
                    log(f"❌ {msg}")
                    raise MetadataFetchError(msg) from exc

        if not page_items:
            if is_last_page is False:
                msg = (
                    "TubeArchivist returned an empty page before pagination "
                    f"completed (page {page})"
                )
                log(f"❌ {msg}")
                raise MetadataFetchError(msg)
            break

        if not all(isinstance(video, dict) for video in page_items):
            msg = f"Unexpected TubeArchivist video entry on page {page}"
            log(f"❌ {msg}")
            raise MetadataFetchError(msg)
        signature = tuple(
            str(video.get("youtube_id") or video.get("_id") or "")
            for video in page_items
        )
        if signature in seen_page_signatures:
            msg = f"TubeArchivist pagination made no progress on page {page}"
            log(f"❌ {msg}")
            raise MetadataFetchError(msg)
        seen_page_signatures.add(signature)

        for video in page_items:
            vid_id = video.get("youtube_id") or video.get("_id")
            if not isinstance(vid_id, str) or not vid_id:
                continue
            title = str(video.get("title") or "unknown_title")
            channel_info = video.get("channel")
            if not isinstance(channel_info, dict):
                channel_info = {}
            channel_name = (
                channel_info.get("channel_name")
                or channel_info.get("channel_title")
                or "Unknown Channel"
            )
            channel_name = str(channel_name)
            raw_date = str(video.get("published") or "unknown_date")
            published = raw_date[:10] if len(raw_date) >= 10 else raw_date.replace("/", "-")
            video_map[vid_id] = {
                "title": title,
                "channel_name": channel_name,
                "published": published,
                "is_live": video.get("vid_type") == "streams",
                "filesystem_path": video.get("path") or video.get("filesystem_path"),
            }

        if is_last_page:
            break
        log(f"   - Page {page} fetched. Total videos so far: {len(video_map)}")
        page += 1

    log(f"✅ Metadata fetch complete. Found {len(video_map)} videos.")
    return video_map
