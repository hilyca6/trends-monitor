"""Google Trends related queries (top + rising) for early long-tail discovery.

For a trending term the RELATED_QUERIES widget returns two ranked lists:

  top    - the most-searched queries related to the term (already competitive)
  rising - queries whose interest is *growing right now*; value is the percent
           increase (or a breakout marker when it grows more than 5000%)

The `rising` list is the valuable one for SEO: those are the "还没爆、正在涨"
long-tails a small site can still win. Results are cached per (geo, query) in
state/related_cache.json so each term is fetched at most once.

Uses the same explore -> widgetdata flow as the interest client
(see collector/gtrends_api.py).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .gtrends_api import RELATED_API, BlockedError, GoogleTrendsClient

log = logging.getLogger("trends.related")

MAX_RISING = 8  # rising queries kept per base term
MAX_TOP = 8

# Queries made purely of generic modifiers carry no buildable topic.
_GENERIC_ONLY = {
    "free", "online", "new", "best", "top", "download", "play", "app", "apps",
    "web", "website", "full", "hd", "apk", "mod", "game", "games", "video",
    "videos", "the", "a", "an", "for", "of", "to", "in", "on", "with", "and",
    "2024", "2025", "2026", "2027",
}
_BAD_RE = re.compile(r"\b(?:download apk|mod apk|crack|cracked|torrent|cheat|cheats|keygen)\b", re.I)
_WORD_RE = re.compile(r"[a-z0-9]+")


def clean_query(value: str) -> str:
    """Normalise whitespace and strip a leading play/download verb."""
    return re.sub(r"\b(?:play|download)\s+", "", " ".join((value or "").split()), flags=re.I).strip()


def is_useful_rising(value: str) -> bool:
    """False for rising queries that are junk or carry no buildable topic.

    Keeps question forms (``how to ...``) and descriptive long-tails; only drops
    piracy/cheat phrasing, over-long strings and queries made purely of generic
    modifiers (``free online games``). Ported from the game-radar tools' rising
    filter, tuned to keep legitimate SEO long-tails.
    """
    q = " ".join((value or "").split())
    if not q or len(q) < 3 or len(q) > 64:
        return False
    if _BAD_RE.search(q):
        return False
    words = _WORD_RE.findall(q.lower())
    if not words or len(words) > 8:
        return False
    meaningful = [w for w in words if w not in _GENERIC_ONLY and not w.isdigit()]
    return len(meaningful) >= 1


def parse_related(payload) -> dict[str, list[dict]]:
    """Parse a relatedsearches payload into {"top": [...], "rising": [...]}.

    Each row is {"query": str, "value": int|None, "is_breakout": bool}.
    """
    out: dict[str, list[dict]] = {"top": [], "rising": []}
    if not isinstance(payload, dict):
        return out
    groups = ((payload.get("default") or {}).get("rankedList")) or []
    keys = ["top", "rising"]
    for i, group in enumerate(groups[:2]):
        rows: list[dict] = []
        for kw in (group or {}).get("rankedKeyword") or []:
            q = (kw.get("query") or "").strip()
            if not q:
                continue
            rows.append(
                {
                    "query": q,
                    "value": kw.get("value"),
                    "is_breakout": bool(kw.get("isBreakout")),
                }
            )
        out[keys[i]] = rows
    return out


def _load_cache(path) -> dict:
    if path and Path(path).exists():
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
    return {}


def _save_cache(path, cache: dict) -> None:
    if not path:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def fetch_related(client: GoogleTrendsClient, kw: str, geo: str, window: str = "now 7-d"):
    """Fetch one term's related queries, or None on transient failure."""
    widgets = client.explore(kw, geo, window)
    widget = client.find_widget(widgets, "RELATED_QUERIES")
    if not widget:
        return None
    payload = client.widget_data(widget, RELATED_API)
    if payload is None:
        return None
    return parse_related(payload)


def expand(
    session,
    queries: list[str],
    *,
    geo: str = "US",
    window: str = "now 7-d",
    throttle: float = 4.0,
    cache_path=None,
    max_rising: int = MAX_RISING,
    max_top: int = MAX_TOP,
    client: GoogleTrendsClient | None = None,
) -> dict[str, dict]:
    """Return {query: {"rising": [str], "top": [str]}} for each base query.

    Cached results are returned without any network call; failures are not
    cached so they are retried on the next run. `rising`/`top` are plain string
    lists (ordered by importance) ready for display/push.

    Pass `client` to reuse one warmed GoogleTrendsClient across geos (or across
    several calls) instead of warming Google up again for each call.
    """
    cache = _load_cache(cache_path)
    client = client or GoogleTrendsClient(session, throttle)
    out: dict[str, dict] = {}
    fetched = 0

    for q in queries:
        key = f"{geo}|{q}"
        rec = cache.get(key)
        if rec is None:
            try:
                data = fetch_related(client, q, geo, window)
            except BlockedError:
                log.warning("related: Google blocked us (/sorry/); stopping expansion")
                break
            if data is None:
                log.info("related %r: no data (will retry)", q)
                continue
            rec = {
                "geo": geo,
                "rising": data["rising"][:max_rising],
                "top": data["top"][:max_top],
            }
            cache[key] = rec
            fetched += 1
        out[q] = {
            "rising": [e["query"] for e in rec.get("rising", [])],
            "top": [e["query"] for e in rec.get("top", [])],
        }

    if fetched:
        _save_cache(cache_path, cache)
        log.info("related: fetched %d new term(s), cache %d", fetched, len(cache))
    return out


def rising_strings(related: dict | None) -> list[str]:
    return list((related or {}).get("rising") or [])
