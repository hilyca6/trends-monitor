"""Candidate discovery for the new-game radar.

Sources (all free):
  - Steam Store featured categories (new releases / coming soon)
  - iTunes RSS "new games" per country (genre 6014)
  - Google Trends "Games" category trending terms (cat id 6)
"""

from __future__ import annotations

import logging

import requests

from .. import config
from .store import is_junk, is_web_game

log = logging.getLogger("trends.games.sources")

STEAM_FEATURED = "https://store.steampowered.com/api/featuredcategories"
ITUNES_NEW_GAMES = "https://itunes.apple.com/{cc}/rss/newfreeapplications/limit={limit}/genre={genre}/json"


def steam_new(session: requests.Session) -> list[dict]:
    try:
        r = session.get(STEAM_FEATURED, params={"cc": config.STEAM_CC, "l": "en"}, timeout=30)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:  # noqa: BLE001
        log.warning("steam failed: %s", e)
        return []

    out: list[dict] = []
    for key, label, limit in (
        ("new_releases", "steam_new", config.STEAM_NEW_LIMIT),
        ("coming_soon", "steam_soon", config.STEAM_COMING_LIMIT),
    ):
        items = (data.get(key, {}).get("items") or [])[:limit]
        size = len(items)
        for i, it in enumerate(items):
            name = it.get("name")
            if not name:
                continue
            appid = it.get("id")
            url = it.get("url") or (f"https://store.steampowered.com/app/{appid}" if appid else None)
            out.append({"name": name, "source": label, "url": url, "rank": i + 1, "size": size})
    log.info("steam -> %d candidates", len(out))
    return out


def appstore_new(session: requests.Session, countries: list[str] | None = None) -> list[dict]:
    countries = countries or config.APPSTORE_COUNTRIES
    out: list[dict] = []
    for cc in countries:
        url = ITUNES_NEW_GAMES.format(cc=cc, limit=config.APPSTORE_LIMIT, genre=config.APPSTORE_GENRE_GAMES)
        try:
            r = session.get(url, timeout=30)
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as e:  # noqa: BLE001
            log.warning("itunes %s failed: %s", cc, e)
            continue
        entries = data.get("feed", {}).get("entry", [])
        size = len(entries)
        for i, entry in enumerate(entries):
            name = (entry.get("im:name") or {}).get("label")
            if not name:
                continue
            url_link = (entry.get("id") or {}).get("label")
            out.append({"name": name, "source": f"appstore:{cc}", "url": url_link, "rank": i + 1, "size": size})
    log.info("appstore -> %d candidates", len(out))
    return out


# Google Trends primary category 17 = Sports. Horse-racing and match-day
# queries often carry Games (6) as a secondary category, so they need a
# dedicated drop rule.
_SPORTS_CAT = 17


def _is_non_game_trend(it: dict, q: str) -> bool:
    """True when a Google-trending term is clearly not a game.

    Trends tags people and sports news with the Games category sometimes
    (e.g. ``leonna mayor`` -> ``cats: [17, 6]``). The ``noise`` label catches
    most of these, but enrichment can lag or fail, so also fall back to the
    LLM entity/classification and to Google's primary category.
    """
    if is_web_game(q):
        return False
    cat = it.get("cat")
    entity = it.get("entity")
    if entity == "person" and cat != "gaming":
        return True
    if cat and cat != "gaming" and entity != "game":
        return True
    cats = it.get("cats") or []
    if cats and cats[0] == _SPORTS_CAT and entity != "game":
        return True
    return False


def non_game_trending_keys(trends_json: dict, geos: list[str] | None = None) -> set[str]:
    """Normalized keys of trending terms that are clearly not games.

    Used to purge records that slipped into the games state before the current
    filters existed (for example ``leonna mayor``).
    """
    keys: set[str] = set()
    for geo, arr in (trends_json.get("items") or {}).items():
        if geos and geo not in geos:
            continue
        for it in arr:
            q = it["q"]
            if it.get("noise") or _is_non_game_trend(it, q) or is_junk(q):
                keys.add(" ".join(q.strip().lower().split()))
    return keys


def from_trending_games(trends_json: dict, geos: list[str] | None = None) -> list[dict]:
    """Games currently trending on Google, from trends.json.

    A term qualifies when it is categorised as Games (cat id 6) OR it looks like
    a web game (``foo.app`` / ``foo.io`` / ...), which catches sudden viral
    browser games that never hit Steam/App Store. Noise-flagged terms, people /
    sports news and game "content queries" (wordle hints, server status, ...)
    are dropped.

    Carries through the trend's own ``growth`` and ``zh`` so the radar can rank
    by "sudden rise" without extra API calls.
    """
    out: list[dict] = []
    for geo, arr in (trends_json.get("items") or {}).items():
        if geos and geo not in geos:
            continue
        for it in arr:
            if it.get("noise"):
                continue
            q = it["q"]
            if not (6 in (it.get("cats") or []) or is_web_game(q)):
                continue
            if _is_non_game_trend(it, q) or is_junk(q):
                continue
            out.append(
                {
                    "name": q,
                    "source": f"trending:{geo}",
                    "growth": it.get("growth"),
                    "zh": it.get("zh", ""),
                }
            )
    log.info("trending-games -> %d candidates", len(out))
    return out
