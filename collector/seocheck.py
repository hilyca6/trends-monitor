"""SERP opportunity scoring for fresh trending terms.

Google now hard-requires JS for search, so the competition check runs against
Bing (direct connection, no proxy) as a close proxy signal: strong authority
domains push the score down, UGC results (reddit/quora/forums) and sparse
results push it up. Scores are cached per query in state/seo_cache.json so
every query is checked at most once.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import quote_plus, unquote, urlsplit

from .http import build_session

log = logging.getLogger("trends.seocheck")

SERP_URL = "https://www.bing.com/search?q={q}&count=20&setlang=en&mkt={mkt}"
# Fallback: Yahoo Search (Bing-backed, no JS, no key). Used when Bing returns
# nothing (e.g. a datacenter IP is blocked on GitHub Actions). DuckDuckGo's HTML
# endpoint now 202-challenges scrapers, Yahoo is a reliable direct-connection
# substitute.
YAHOO_URL = "https://search.yahoo.com/search?p={q}&ei=UTF-8"

# Domains whose presence in the top 5 means a new small site cannot outrank them.
STRONG_DOMAINS = {
    "wikipedia.org",
    "nytimes.com",
    "cnn.com",
    "bbc.co.uk",
    "bbc.com",
    "espn.com",
    "imdb.com",
    "amazon.com",
    "yahoo.com",
    "britannica.com",
    "forbes.com",
    "healthline.com",
    "mayoclinic.org",
    "webmd.com",
    "merriam-webster.com",
    "dictionary.com",
    "investopedia.com",
    "booking.com",
    "tripadvisor.com",
    "indeed.com",
    "youtube.com",
    "tiktok.com",
    "instagram.com",
    "facebook.com",
    "x.com",
    "twitter.com",
    "msn.com",
    "microsoft.com",
}

# UGC results on page one mean the engine could not find enough good articles: opportunity.
UGC_HINTS = (
    "reddit.com",
    "quora.com",
    "stackexchange.com",
    "stackoverflow.com",
    "medium.com",
    "pinterest.com",
    "forum",
    "boards.",
    "community.",
)

# Pure-redirect/internal hosts that carry no competition signal.
_SKIP_HOST_RE = re.compile(
    r"(^|\.)(bing\.com|microsoft\.com|msn\.com|live\.com|google\.[a-z.]+|gstatic\.com|bing\.net)$"
)
_BING_U_RE = re.compile(r"u=a1([A-Za-z0-9\-_]{20,})")
_YAHOO_RU_RE = re.compile(r"RU=([^/]+)/RK=")
_DIRECT_RE = re.compile(r'href="(https?://[^"]+)"')
_DOMAIN_RE = re.compile(r"https?://([^/:?#]+)")
_WORD_RE = re.compile(r"[a-z0-9]+")


def _strip_url(url: str) -> str:
    """Drop query/fragment so cached SERP URLs stay short and stable."""
    try:
        p = urlsplit(url)
        return f"{p.scheme}://{p.netloc}{p.path}" if p.netloc else url
    except ValueError:
        return url

# --- market-freshness (SERP gap) signals, ported from the game-radar tools ---
# Hosts that are platforms/aggregators, not "someone built a site for this".
PLATFORM_HOSTS = {
    "store.steampowered.com", "steampowered.com", "steamcommunity.com",
    "itch.io", "crazygames.com", "poki.com", "y8.com", "gamepix.com",
    "lagged.com", "newgrounds.com", "roblox.com", "xbox.com",
    "playstation.com", "epicgames.com", "nintendo.com", "kongregate.com",
    "armorgames.com", "miniclip.com", "gamejolt.com",
    "youtube.com", "youtu.be", "twitch.tv", "reddit.com", "discord.com",
    "twitter.com", "x.com", "tiktok.com", "facebook.com", "instagram.com",
    "google.com", "bing.com", "pinterest.com", "medium.com",
}
DEDICATED_WIKI_HOSTS = {"fandom.com", "wiki.gg", "wikidot.com", "wikiwand.com"}
_CONTENT_RE = re.compile(
    r"(wiki|guide|walkthrough|database|maps?|builds?|characters?|items?|weapons?|codes?|攻略|图鉴)",
    re.IGNORECASE,
)

# Words that make a keyword name too generic / ambiguous to build a niche on.
_GENERIC_WORDS = {
    "the", "a", "an", "of", "and", "for", "to", "in", "on", "with", "my",
    "your", "new", "super", "mega", "funny", "city", "car", "truck", "race",
    "racing", "rush", "party", "food", "game", "games", "online", "3d", "2d",
    "idle", "clicker", "puzzle", "adventure", "hero", "heroes", "web",
}
_HIGH_RISK_RE = re.compile(
    r"\b(military|war|airstrike|attack|election|president|government|weather|"
    r"hurricane|earthquake|disease|virus|stock|crypto|bitcoin|football|"
    r"basketball|parking|supermarket|song|music|movie|film)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Game-intent / entity-conflict classification (ported from the game-name-radar
# SEO verifier). Decides whether a keyword's SERP is really about a game or
# about a same-named song / movie / news story.
# ---------------------------------------------------------------------------
DEDICATED_GAME_DOMAINS = {
    "itch.io", "crazygames.com", "poki.com", "y8.com", "gamepix.com", "lagged.com",
    "steamcommunity.com", "store.steampowered.com", "gamejolt.com", "kongregate.com",
    "armorgames.com", "miniclip.com", "roblox.com", "gx.games", "newgrounds.com",
}
NEWS_DOMAINS = {
    "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", "cnn.com", "foxnews.com",
    "nbcnews.com", "cbsnews.com", "abcnews.go.com", "nytimes.com", "washingtonpost.com",
    "theguardian.com", "aljazeera.com", "politico.com", "cfr.org", "defensenews.com",
    "newsweek.com", "time.com", "forbes.com", "bloomberg.com", "wsj.com", "npr.org",
    "axios.com", "usatoday.com", "euronews.com",
}
NON_GAME_DOMAINS = {
    "spotify.com", "genius.com", "soundcloud.com", "last.fm", "discogs.com",
    "allmusic.com", "songfacts.com", "lyrics.com", "musixmatch.com", "imdb.com",
    "rottentomatoes.com", "goodreads.com", "music.apple.com",
}
_GAME_TERMS = re.compile(
    r"\b(video game|browser game|online game|html5 game|indie game|gameplay|"
    r"walkthrough|playthrough|game demo|game jam|steam game|play the game|play game|"
    r"game controls|games?)\b",
    re.IGNORECASE,
)
_NEWS_CONTENT_TERMS = re.compile(
    r"\b(news|war|conflict|attack|airstrike|military strike|missile|troops|army|"
    r"defense|politics|election|president|government|israel|iran|ukraine|russia|"
    r"killed|ceasefire)\b",
    re.IGNORECASE,
)
_NON_GAME_CONTENT_TERMS = re.compile(
    r"\b(song|lyrics?|album|track|single|band|singer|artist|music|metallica|"
    r"soundtrack|concert|film|movie|television|tv series|episode|novel|book|"
    r"motorcycle|bike|price|product|company)\b",
    re.IGNORECASE,
)
_ANCHOR_ATTR_RE = re.compile(r"<a\b([^>]*)>([\s\S]*?)</a>", re.IGNORECASE)
_HREF_IN_ATTRS_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
_PHRASE_RE = re.compile(r"[^a-z0-9]+")


def _anchor_text_map(html: str) -> dict:
    """Map a result URL (and its raw href) to its anchor text (Bing title)."""
    titles: dict[str, str] = {}
    for m in _ANCHOR_ATTR_RE.finditer(html or ""):
        href_m = _HREF_IN_ATTRS_RE.search(m.group(1))
        if not href_m:
            continue
        href = href_m.group(1)
        target = href
        b64 = _BING_U_RE.search(href)
        if b64:
            decoded = _decode_bing_link(b64.group(1))
            if decoded:
                target = decoded
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(2))).strip()
        if not text:
            continue
        for key in (href, target):
            titles.setdefault(key, text)
    return titles


def _normalize_phrase(value: str) -> str:
    s = (value or "").lower().replace("&", " and ")
    s = re.sub(r"\b(?:web|game|games|online|official)\b", " ", s)
    return re.sub(r"\s+", " ", _PHRASE_RE.sub(" ", s)).strip()


def _result_matches_name(result: dict, target_name: str) -> bool:
    target = _normalize_phrase(target_name)
    text = _normalize_phrase(f"{result.get('title', '')} {result.get('url', '')}")
    if not target or not text:
        return False
    if target in text:
        return True
    target_tokens = [t for t in target.split() if len(t) > 1]
    if not target_tokens:
        return False
    text_tokens = set(text.split())
    overlap = sum(1 for t in target_tokens if t in text_tokens) / len(target_tokens)
    return overlap >= 0.75


def classify_intent(results: list[dict], target_name: str) -> dict:
    """Split a result list into game / news / non-game / other, with ratios."""
    game = news = non_game = other = 0
    for r in results[:10]:
        host = r.get("host", "")
        text = f"{r.get('title', '')} {r.get('url', '')}"
        name_match = _result_matches_name(r, target_name)
        dedicated_game = _hits(host, DEDICATED_GAME_DOMAINS) and name_match
        non_game_evidence = _hits(host, NON_GAME_DOMAINS) or bool(_NON_GAME_CONTENT_TERMS.search(text))
        game_evidence = name_match and (dedicated_game or bool(_GAME_TERMS.search(text)))
        news_evidence = _hits(host, NEWS_DOMAINS) or bool(_NEWS_CONTENT_TERMS.search(text))
        if game_evidence and not non_game_evidence:
            game += 1
        elif non_game_evidence:
            non_game += 1
        elif news_evidence:
            news += 1
        else:
            other += 1
    total = max(1, game + news + non_game + other)
    return {
        "game": game,
        "news": news,
        "nonGame": non_game,
        "other": other,
        "total": game + news + non_game + other,
        "gameRatio": round(game / total, 2),
        "newsRatio": round(news / total, 2),
        "nonGameRatio": round(non_game / total, 2),
    }


def _direct_session():
    """Bing breaks through some proxies (SSL reset); always connect directly."""
    s = build_session(None)
    s.trust_env = False
    s.proxies.clear()
    return s


def _decode_bing_link(b64: str) -> str | None:
    try:
        pad = b64 + "=" * (-len(b64) % 4)
        return base64.urlsafe_b64decode(pad).decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return None


def extract_results(html: str) -> list[dict]:
    """Ordered, de-duplicated SERP results as ``{url, host, path, title}`` records."""
    out: list[dict] = []
    seen: set[str] = set()
    titles = _anchor_text_map(html)

    candidates: list[str] = []
    for b64 in _BING_U_RE.findall(html):
        url = _decode_bing_link(b64)
        if url:
            candidates.append(url)
    # supplement with plain external anchors (some layouts skip obfuscation)
    candidates += _DIRECT_RE.findall(html)

    for url in candidates:
        m = _DOMAIN_RE.match(url)
        if not m:
            continue
        host = m.group(1).lower().removeprefix("www.")
        if _SKIP_HOST_RE.search(host):
            continue
        if url in seen:
            continue
        seen.add(url)
        try:
            path = urlsplit(url).path.lower()
        except ValueError:
            path = ""
        out.append({"url": url, "host": host, "path": path, "title": titles.get(url, "")})
    return out


def extract_domains(html: str) -> list[str]:
    """Ordered, de-duplicated result domains from a Bing SERP page."""
    found: list[str] = []
    seen: set[str] = set()
    for rec in extract_results(html):
        if rec["host"] not in seen:
            seen.add(rec["host"])
            found.append(rec["host"])
    return found


def extract_results_yahoo(html: str) -> list[dict]:
    """Same ``{url, host, path, title}`` records, parsed from Yahoo Search HTML.

    Yahoo wraps result links as ``.../RU=<urlencoded>/RK=...``; we decode RU and
    take the anchor text as the title.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for m in _ANCHOR_ATTR_RE.finditer(html or ""):
        href_m = _HREF_IN_ATTRS_RE.search(m.group(1))
        if not href_m:
            continue
        ru = _YAHOO_RU_RE.search(href_m.group(1))
        if not ru:
            continue
        url = unquote(ru.group(1))
        if not url.startswith("http"):
            continue
        dm = _DOMAIN_RE.match(url)
        if not dm:
            continue
        host = dm.group(1).lower().removeprefix("www.")
        if _SKIP_HOST_RE.search(host) or host.endswith("yahoo.com") or host.endswith("oath.com"):
            continue
        if url in seen:
            continue
        seen.add(url)
        try:
            path = urlsplit(url).path.lower()
        except ValueError:
            path = ""
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(2))).strip()
        out.append({"url": url, "host": host, "path": path, "title": text})
    return out


def _hits(domain: str, needles) -> bool:
    return any(n == domain or n in domain or domain.endswith("." + n) for n in needles)


def score_serp(domains: list[str]) -> dict:
    """Turn an ordered domain list into an opportunity score (0-100, higher = easier).

    None score means we could not parse any results (blocked / layout change).
    """
    top = domains[:10]
    if not top:
        return {"score": None, "label": "?", "ugc": False, "strong": 0, "n": 0}

    strong = sum(1 for d in top[:5] if _hits(d, STRONG_DOMAINS))
    ugc = any(_hits(d, UGC_HINTS) for d in top)
    small = sum(1 for d in top if not _hits(d, STRONG_DOMAINS) and not _hits(d, UGC_HINTS))

    score = 60
    score -= strong * 12
    score += 18 if ugc else 0
    score += 10 if small >= 6 else 0
    score += 10 if len(top) < 5 else 0
    score = max(5, min(95, score))

    label = "高" if score >= 70 else ("中" if score >= 45 else "低")
    return {"score": score, "label": label, "ugc": ugc, "strong": strong, "n": len(top)}


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _host_match(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _is_platform_host(host: str) -> bool:
    return any(_host_match(host, d) for d in PLATFORM_HOSTS)


def market_status(query: str, results: list[dict]) -> dict:
    """Is this SERP gap open, contested or already occupied? (SERP-gap signal).

    * dedicated   – a result whose *domain* contains the keyword (someone named a
      site after it), or a fandom/wiki host whose path mentions it
    * content      – a result whose path mentions the keyword AND reads like a
      wiki/guide/walkthrough page (an early content competitor)
    """
    cq = _compact(query)
    records = [r for r in results if r.get("host")]
    nonplatform = [r for r in records if not _is_platform_host(r["host"])]

    dedicated: list[str] = []
    content: list[str] = []
    for r in nonplatform:
        host, path = r["host"], r.get("path", "")
        host_hit = bool(cq) and len(cq) >= 4 and any(cq in _compact(lbl) for lbl in host.split("."))
        wiki_hit = any(_host_match(host, d) for d in DEDICATED_WIKI_HOSTS) and bool(cq) and cq in _compact(path)
        if host_hit or wiki_hit:
            dedicated.append(host)
        if cq and (cq in _compact(path) or host_hit) and _CONTENT_RE.search(host + path):
            content.append(host)

    dedicated_domains = sorted(set(dedicated))
    content_domains = sorted(set(content))
    checked = bool(records)

    if dedicated_domains:
        status, confidence = "occupied", "high"
        reason = "已发现专站/同名域名占位：" + "、".join(dedicated_domains[:3])
    elif len(content_domains) >= 2:
        status, confidence = "occupied", "medium"
        reason = "已有多个攻略/Wiki竞争站覆盖：" + "、".join(content_domains[:3])
    elif not checked:
        status, confidence = "unknown", "low"
        reason = "未解析到搜索结果"
    elif len(content_domains) == 1:
        status, confidence = "contested", "medium"
        reason = "已出现早期内容竞争站：" + content_domains[0]
    else:
        status, confidence = "greenfield", "medium"
        reason = "未发现专站或攻略竞争站占位"
    return {
        "status": status, "confidence": confidence, "reason": reason,
        "dedicatedDomains": dedicated_domains, "contentDomains": content_domains,
        "checked": checked,
    }


def name_risk(name: str) -> int:
    """0-30 risk that a keyword name is too generic / ambiguous / high-risk.

    Ported from the game-radar SEO verifier; higher = riskier to build a niche on.
    """
    tokens = _WORD_RE.findall((name or "").lower())
    if not tokens:
        return 25
    generic = sum(1 for t in tokens if t in _GENERIC_WORDS or re.fullmatch(r"\d+d?", t))
    risk = round((generic / len(tokens)) * 18)
    if _HIGH_RISK_RE.search(name or ""):
        risk += 8
    if len(tokens) == 1:
        risk += 5
    return min(30, risk)


def _load_cache(path) -> dict:
    if path:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {}


def _save_cache(path, cache: dict) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def check_queries(
    session,
    queries: list[str],
    *,
    throttle: float = 3.0,
    geo: str = "US",
    cache_path=None,
    cache_days: float = 7,
) -> dict:
    """Score each query; cached (and not-yet-stale) queries skip the network.

    ``session`` may be None (a direct, proxy-free session is built). ``geo``
    maps to Bing's mkt parameter (e.g. "US" -> "en-US"). Results older than
    ``cache_days`` are re-checked so the SERP-gap / fast-heat signals can still
    move over time.
    """
    cache = _load_cache(cache_path)
    now = datetime.now(timezone.utc)

    def _fresh(q: str) -> bool:
        rec = cache.get(q)
        if not isinstance(rec, dict) or not rec.get("checked"):
            return False
        try:
            age = (now - datetime.fromisoformat(rec["checked"])).total_seconds()
        except (ValueError, TypeError):
            return False
        return age <= cache_days * 86400

    pending = [q for q in queries if not _fresh(q)]
    if pending:
        log.info("serp check: %d queries (%d cached)", len(pending), len(queries) - len(pending))

    if pending and session is None:
        session = _direct_session()

    for i, q in enumerate(pending):
        mkt = f"en-{geo}" if "-" not in geo else geo
        html = get_html(session, SERP_URL.format(q=quote_plus(q), mkt=mkt))
        results = extract_results(html or "")
        if not results:  # Bing blocked / empty -> Yahoo fallback (Bing-backed, direct)
            yh = get_html(session, YAHOO_URL.format(q=quote_plus(q)))
            results = extract_results_yahoo(yh or "")
        domains: list[str] = []
        seen_hosts: set[str] = set()
        for rec in results:
            if rec["host"] not in seen_hosts:
                seen_hosts.add(rec["host"])
                domains.append(rec["host"])
        result = score_serp(domains)
        result["market"] = market_status(q, results)
        result["nameRisk"] = name_risk(q)
        intent = classify_intent(results, q)
        result["gameIntent"] = intent
        result["entityConflict"] = intent["nonGameRatio"] >= 0.4
        result["serpUrls"] = [_strip_url(r["url"]) for r in results][:5]
        result["checked"] = datetime.now(timezone.utc).isoformat()
        cache[q] = result
        if (i + 1) % 5 == 0:
            _save_cache(cache_path, cache)

    _save_cache(cache_path, cache)
    return {q: cache[q] for q in queries if q in cache}


def get_html(session, url: str) -> str | None:
    try:
        r = session.get(url, timeout=30)
        if r.status_code != 200:
            log.warning("GET %s -> HTTP %s", url.split("?")[0], r.status_code)
            return None
        return r.text
    except Exception as e:  # noqa: BLE001
        log.warning("GET %s failed: %s", url.split("?")[0], e)
        return None
