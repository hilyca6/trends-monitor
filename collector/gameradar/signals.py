"""Game-radar fast signals: multi-platform spread + freshness.

Ported from the game-name-radar ``fast-signals`` online model. Where that model
reads per-source first-seen timestamps, the game radar records them per source
id in the state record (``sourceFirst``). Several feeds of one portal collapse
into a single platform (see ``sources.platform_key``).
"""

from __future__ import annotations

from datetime import datetime

from ..games.store import is_web_game
from .sources import platform_key

DAY_MS = 86_400_000


def to_ms(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp() * 1000
    except (ValueError, TypeError):
        return 0.0


def compute_fast(rec: dict, now_ms: float) -> dict:
    """Score an item's spread: how many platforms, how fresh, how often seen."""
    platforms = {platform_key(s) for s in (rec.get("sources") or [])}
    plat_first: dict[str, float] = {}
    for source, ts in (rec.get("sourceFirst") or {}).items():
        key = platform_key(source)
        t = to_ms(ts)
        if key not in plat_first or (t and t < plat_first[key]):
            plat_first[key] = t
    added24 = sum(1 for t in plat_first.values() if t and now_ms - t <= DAY_MS)
    added48 = sum(1 for t in plat_first.values() if t and now_ms - t <= 2 * DAY_MS)
    sightings = int(rec.get("sightings") or 0)

    score = 0
    score += min(30, added24 * 10)
    score += min(12, max(0, added48 - added24) * 4)
    score += min(30, len(platforms) * 10)
    if len(platforms) >= 2:
        score += 20
    elif len(platforms) == 1:
        score += 8
    score += min(15, sightings * 3)
    if is_web_game(rec.get("name", "")):
        score += 5

    yt = rec.get("youtube") or {}
    yt_channels = int(yt.get("channelCount") or 0)
    yt_videos = int(yt.get("videoCount") or 0)
    yt_views = int(yt.get("totalViews") or 0)
    score += min(8, yt_channels * 2)
    if yt_videos >= 6:
        score += 3
    if yt_views >= 20000:
        score += 3
    score = max(0, min(100, score))

    if len(platforms) >= 2 and added24 >= 1:
        classification = "pass"
    elif added24 >= 2 or score >= 42:
        classification = "pass"
    elif score >= 20:
        classification = "watch"
    else:
        classification = "weak"

    return {
        "model": "gameradar",
        "score": score,
        "classification": classification,
        "onlinePlatformCount": len(platforms),
        "sourceAdded24h": added24,
        "sourceAdded48h": added48,
    }
