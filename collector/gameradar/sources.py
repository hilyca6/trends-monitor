"""Candidate discovery for the game-name radar.

All sources are free public listing pages / RSS feeds of browser-game portals
(ported from the ``game-name-radar`` scanner). Each function returns a list of
``{name, source, url}`` candidates.

Sources:
    itch               itch.io 最新网页游戏（HTML）
    itch-popular       itch.io New & Popular（HTML）
    itch-new-feed      itch.io New Feed（RSS）
    itch-featured-feed itch.io Featured Feed（RSS）
    crazygames         CrazyGames 新游戏（HTML）
    poki               Poki 新游戏（HTML）
    y8                 Y8 新游戏（HTML）
    gamepix            GamePix 新游戏（HTML）
    lagged             Lagged 新游戏（HTML）
    newgrounds         Newgrounds Daily Top Five（RSS）
"""

from __future__ import annotations

import html as _html
import logging
import re
from urllib.parse import unquote, urljoin, urlparse

import requests

from ..http import get_text

log = logging.getLogger("trends.gameradar.sources")

# Source id -> listing URL.
SOURCE_URLS = {
    "itch": "https://itch.io/games/newest/platform-web",
    "itch-popular": "https://itch.io/games/new-and-popular/platform-web",
    "itch-new-feed": "https://itch.io/feed/new.xml",
    "itch-featured-feed": "https://itch.io/feed/featured.xml",
    "crazygames": "https://www.crazygames.com/new",
    "poki": "https://poki.com/en/new",
    "y8": "https://www.y8.com/new/games",
    "gamepix": "https://www.gamepix.com/new",
    "lagged": "https://lagged.com/new-games",
    "newgrounds": "https://rss.ngfiles.com/dailytop5.xml",
}

# Sources served as RSS feeds (parsed with parse_feed).
FEED_SOURCES = ("newgrounds", "itch-new-feed", "itch-featured-feed")

DEFAULT_SOURCES = [
    "itch", "itch-popular", "itch-new-feed", "itch-featured-feed",
    "crazygames", "poki", "y8", "gamepix", "lagged", "newgrounds",
]

# id -> platform domain (several feeds of one portal count as one platform).
PLATFORM_BY_SOURCE = {
    "itch": "itch.io",
    "itch-popular": "itch.io",
    "itch-new-feed": "itch.io",
    "itch-featured-feed": "itch.io",
    "newgrounds": "newgrounds.com",
    "crazygames": "crazygames.com",
    "poki": "poki.com",
    "y8": "y8.com",
    "gamepix": "gamepix.com",
    "lagged": "lagged.com",
}


def platform_key(source: str) -> str:
    """Collapse multiple feeds of one portal into a single platform key."""
    return PLATFORM_BY_SOURCE.get(source, source)


# Newgrounds feeds mix games with movies/animations and there is no type field
# in the RSS. Titles that clearly read as animations are dropped. Extend this if
# you find more markers.
_MOVIE_TERMS = re.compile(
    r"\b(animation|animated|animatic|cartoon|meme|qna|reel|trailer|teaser|music video)\b"
    r"|\bep\.?\s*\d",
    re.IGNORECASE,
)


def is_probably_movie(name: str) -> bool:
    """Heuristic: True for titles that look like animations, not games."""
    return bool(_MOVIE_TERMS.search(name or ""))

# Per-portal game-detail path shapes (relative), used to filter real listings.
PORTAL_PROFILES = {
    "crazygames": re.compile(r"^/(?:[a-z]{2}/)?game/[a-z0-9-]+/?$", re.IGNORECASE),
    "poki": re.compile(r"^/(?:[a-z]{2}/)?g/[a-z0-9-]+/?$", re.IGNORECASE),
    "y8": re.compile(r"^/games/[a-z0-9_%.-]+/?$", re.IGNORECASE),
    "gamepix": re.compile(r"^/play/[a-z0-9-]+/?$", re.IGNORECASE),
    "lagged": re.compile(r"^/(?:[a-z]{2}/)?g/[a-z0-9-]+/?$", re.IGNORECASE),
}

_ANCHOR_RE = re.compile(r"<a\b([^>]*)>([\s\S]*?)</a>", re.IGNORECASE)
_ATTR_RE = {
    "href": re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE),
    "title": re.compile(r'title=["\']([^"\']+)["\']', re.IGNORECASE),
    "aria": re.compile(r'aria-label=["\']([^"\']+)["\']', re.IGNORECASE),
    "imgalt": re.compile(r'<img\b[^>]*alt=["\']([^"\']+)["\'][^>]*>', re.IGNORECASE),
}
_ITEM_RE = re.compile(r"<item\b[^>]*>([\s\S]*?)</item>", re.IGNORECASE)
_TITLE_RE = re.compile(r"<title\b[^>]*>([\s\S]*?)</title>", re.IGNORECASE)
_LINK_RE = re.compile(r"<link\b[^>]*>([\s\S]*?)</link>", re.IGNORECASE)

_STOP_WORDS = frozenset(
    {"play", "online", "game", "games", "free", "unblocked", "html5", "browser", "download"}
)


def _decode(value: str) -> str:
    value = re.sub(r"<!\[CDATA\[([\s\S]*?)\]\]>", r"\1", value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return _html.unescape(re.sub(r"\s+", " ", value)).strip()


def _clean_name(value: str) -> str:
    """Strip common listing noise from a title, then normalize whitespace."""
    v = _decode(value)
    v = re.sub(r"^\s*\d{1,3}[.)]\s+", "", v)
    v = re.sub(r"^(?:new|hot|top|updated|originals?)\s+", "", v, flags=re.IGNORECASE)
    v = re.sub(r"\s+(?:new|hot|top|updated|originals?)$", "", v, flags=re.IGNORECASE)
    v = re.sub(r"\s*[|–—-]\s*(?:itch\.io|play online|free online game|game)\s*$", "", v, flags=re.IGNORECASE)
    v = re.sub(r"^\s*play\s+", "", v, flags=re.IGNORECASE)
    v = re.sub(r"\s+(?:online|unblocked|game)$", "", v, flags=re.IGNORECASE)
    v = re.sub(r"\s+\d(?:\.\d)?$", "", v)
    return re.sub(r"\s+", " ", v).strip()


def _name_from_slug(url: str) -> str:
    try:
        slug = urlparse(url).path.rstrip("/").split("/")[-1]
    except ValueError:
        return ""
    slug = re.sub(r"\.[a-z0-9]{2,5}$", "", unquote(slug))
    words = [w for w in re.split(r"[-_+]+", slug) if w and w.lower() not in _STOP_WORDS]
    return " ".join(w[:1].upper() + w[1:] if w.islower() else w for w in words)


def _iter_anchors(html: str):
    for m in _ANCHOR_RE.finditer(html or ""):
        attrs, body = m.group(1), m.group(2)
        href = _ATTR_RE["href"].search(attrs)
        if not href:
            continue
        title = _ATTR_RE["aria"].search(attrs) or _ATTR_RE["title"].search(attrs)
        alt = _ATTR_RE["imgalt"].search(body)
        text = _decode(body)
        raw = (title.group(1) if title else "") or text or (alt.group(1) if alt else "")
        yield href.group(1), raw


def parse_itch(html: str, base_url: str) -> list[dict]:
    """itch.io listing: anchors whose class contains ``title``."""
    out: list[dict] = []
    seen: set[str] = set()
    rex = re.compile(r'<a\b([^>]*class=["\'][^"\']*\btitle\b[^"\']*["\'][^>]*)>([\s\S]*?)</a>', re.IGNORECASE)
    for m in rex.finditer(html or ""):
        href = _ATTR_RE["href"].search(m.group(1))
        if not href:
            continue
        url = urljoin(base_url, _html.unescape(href.group(1)))
        if url in seen:
            continue
        name = _clean_name(m.group(2))
        if not name:
            continue
        seen.add(url)
        out.append({"name": name, "url": url})
    return out


def parse_portal(html: str, base_url: str, source: str) -> list[dict]:
    """Generic portal listing: keep same-host anchors matching the path profile."""
    profile = PORTAL_PROFILES.get(source)
    if not profile:
        return []
    base_host = urlparse(base_url).netloc.replace("www.", "").lower()
    out: list[dict] = []
    seen: set[str] = set()
    for href, raw in _iter_anchors(html):
        url = urljoin(base_url, _html.unescape(href))
        try:
            parsed = urlparse(url)
        except ValueError:
            continue
        if parsed.netloc.replace("www.", "").lower() != base_host:
            continue
        if not profile.match(parsed.path):
            continue
        if url in seen:
            continue
        name = _clean_name(raw) or _name_from_slug(url)
        if len(name) < 2:
            continue
        seen.add(url)
        out.append({"name": name, "url": url})
    return out


def parse_feed(xml: str, base_url: str = "") -> list[dict]:
    """RSS ``<item>`` listing (Newgrounds): title + link."""
    out: list[dict] = []
    for m in _ITEM_RE.finditer(xml or ""):
        block = m.group(1)
        title = _TITLE_RE.search(block)
        link = _LINK_RE.search(block)
        name = _clean_name(title.group(1)) if title else ""
        url = _decode(link.group(1)) if link else ""
        if not name or not url:
            continue
        out.append({"name": name, "url": url})
    return out


def _fetch(session: requests.Session, url: str, throttle: float) -> str | None:
    try:
        return get_text(session, url, throttle=throttle)
    except requests.RequestException as e:  # noqa: BLE001
        log.warning("%s failed: %s", url, e)
        return None


def discover_source(session: requests.Session, source: str, throttle: float = 1.0) -> list[dict]:
    url = SOURCE_URLS.get(source)
    if not url:
        return []
    html = _fetch(session, url, throttle)
    if not html:
        return []

    if source in ("itch", "itch-popular"):
        entries = parse_itch(html, url)
    elif source in FEED_SOURCES:
        entries = parse_feed(html, url)
        if source.startswith("newgrounds"):
            entries = [e for e in entries if not is_probably_movie(e["name"])]
    else:
        entries = parse_portal(html, url, source)

    for e in entries:
        e["source"] = source
    log.info("gameradar/%s -> %d candidates", source, len(entries))
    return entries


def discover(session: requests.Session, sources: list[str], throttle: float = 1.0) -> list[dict]:
    out: list[dict] = []
    for source in sources:
        out += discover_source(session, source, throttle)
    return out
