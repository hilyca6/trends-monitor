"""YouTube Data API v3 heat for game names.

Ported from the game-name-radar ``fast-signals.verifyYoutubeSignals``: search
``"{name} gameplay"`` over the last 7 days, keep title/description matches, then
pull stats for the matched videos.

Needs a YouTube Data API v3 key (Google Cloud Console -> enable the API ->
create an API key). Free quota is 10,000 units/day; a search costs 100 units.
When no key is set the caller simply skips this step.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger("trends.gameradar.youtube")

API_BASE = "https://www.googleapis.com/youtube/v3"
DAY_MS = 86_400_000
_WORD_RE = re.compile(r"[^a-z0-9]+")


def _normalize(value: str) -> str:
    return _WORD_RE.sub(" ", (value or "").lower()).strip()


def name_matches(title: str, name: str) -> bool:
    target = _normalize(name)
    text = _normalize(title)
    return len(target) >= 3 and target in text


class YouTubeError(RuntimeError):
    pass


def verify_youtube(
    name: str,
    api_key: str,
    *,
    timeout: int = 30,
    throttle: float = 0.0,
    now_ms: float | None = None,
) -> dict | None:
    """Return ``{checkedAt, videoCount, channelCount, totalViews, recent24h}`` or None."""
    if not api_key:
        return None
    if throttle:
        time.sleep(throttle)

    now_ms = now_ms if now_ms is not None else datetime.now(timezone.utc).timestamp() * 1000
    now_iso = datetime.now(timezone.utc).isoformat()
    published_after = datetime.fromtimestamp((now_ms - 7 * DAY_MS) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    search_params = {
        "part": "snippet",
        "type": "video",
        "order": "date",
        "maxResults": "25",
        "q": f"{name} gameplay",
        "publishedAfter": published_after,
        "key": api_key,
    }
    resp = requests.get(f"{API_BASE}/search", params=search_params, timeout=timeout)
    if resp.status_code != 200:
        raise YouTubeError(f"YouTube search returned {resp.status_code}")
    search_items = resp.json().get("items", [])
    matched = [
        it for it in search_items
        if name_matches(f"{it.get('snippet', {}).get('title', '')} {it.get('snippet', {}).get('description', '')}", name)
    ]
    ids = [it.get("id", {}).get("videoId") for it in matched]
    ids = [vid for vid in ids if vid]
    if not ids:
        return {"configured": True, "checkedAt": now_iso, "videoCount": 0, "channelCount": 0,
                "totalViews": 0, "totalLikes": 0, "totalComments": 0, "recent24h": 0}

    stats_params = {"part": "statistics,snippet", "id": ",".join(ids), "key": api_key}
    resp2 = requests.get(f"{API_BASE}/videos", params=stats_params, timeout=timeout)
    if resp2.status_code != 200:
        raise YouTubeError(f"YouTube stats returned {resp2.status_code}")
    items = resp2.json().get("items", [])
    return {
        "configured": True,
        "checkedAt": now_iso,
        "videoCount": len(items),
        "channelCount": len({it.get("snippet", {}).get("channelId") for it in items if it.get("snippet", {}).get("channelId")}),
        "totalViews": sum(int(it.get("statistics", {}).get("viewCount") or 0) for it in items),
        "totalLikes": sum(int(it.get("statistics", {}).get("likeCount") or 0) for it in items),
        "totalComments": sum(int(it.get("statistics", {}).get("commentCount") or 0) for it in items),
        "recent24h": sum(
            1 for it in items
            if _published_within(it.get("snippet", {}).get("publishedAt", ""), now_ms, DAY_MS)
        ),
    }


def _published_within(published_at: str, now_ms: float, window_ms: float) -> bool:
    try:
        t = datetime.fromisoformat(published_at.replace("Z", "+00:00")).timestamp() * 1000
    except (ValueError, TypeError):
        return False
    return now_ms - t <= window_ms
