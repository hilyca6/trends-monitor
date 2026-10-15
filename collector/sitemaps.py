"""Competitor-sitemap first-mover signal ("the market votes with domains").

When a game/site starts getting written into more independent domains'
sitemaps, the owners are already building pages for it — *before* the traffic
shows up. This module periodically fetches a list of watched sitemaps, parses
their URLs, extracts a keyword from each URL slug and tracks how many distinct
domains list it, plus how recently those domains started listing it.

State (state/sitemap_state.json) keeps ``{watch_url: {page_url: first_seen}}``
so a URL is only "new" the first time we see it. Output goes to
web/data/sitemap.json:

    {updated, watch_count, count, items:[{
        q, domains[], domain_count, domain_count_7d, domain_count_15d,
        domain_count_30d, first, new, zh, noise, cat, subcat, entity, trigger
    }]}
"""

from __future__ import annotations

import argparse
import gzip
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import config
from .enrich import enrich
from .http import build_session
from .store import _load_json, now_iso, save_json

log = logging.getLogger("trends.sitemaps")

STATE_NAME = "sitemap_state.json"
MAX_ITEMS = 2000

# Marks URLs captured on a watch's very first (baseline) snapshot, so an entire
# pre-existing sitemap is not reported as "new" the first time we see it.
BASELINE_ISO = "2000-01-01T00:00:00+00:00"

# Path segments that are site boilerplate, not keywords.
_SKIP_SEG = {
    "games", "game", "play", "online", "en", "zh", "us", "uk", "www",
    "index", "home", "category", "categories", "tag", "tags", "page", "p",
    "blog", "news", "apps", "app", "html", "htm", "sitemap", "rss", "feed",
    "privacy", "policy", "terms", "about", "contact", "dmca", "login",
    "register", "signup", "sign-up", "help", "faq", "support", "search",
}
_HEXISH = re.compile(r"^[0-9a-f]{12,}$")

# Common two-level public suffixes, so eTLD+1 stays correct for these.
_TWO_LEVEL = {
    "co.uk", "org.uk", "gov.uk", "ac.uk", "co.jp", "ne.jp", "or.jp",
    "com.au", "net.au", "org.au", "com.br", "com.cn", "com.tw", "com.hk",
    "com.sg", "com.mx", "co.nz", "co.kr", "com.tr", "co.za", "com.ar",
    "com.co", "com.pe", "co.in", "com.my", "com.ph", "com.vn", "com.pk",
    "com.bd", "com.sa", "com.ng", "com.ke",
}


def default_state_path() -> Path:
    return config.STATE_DIR / STATE_NAME


# ---------------------------------------------------------------------------
# URL -> keyword
# ---------------------------------------------------------------------------
def extract_keyword(url: str) -> str | None:
    """Best-effort keyword from a page URL slug, or None if it looks like junk."""
    try:
        path = urlsplit(url).path
    except ValueError:
        return None
    words: list[str] = []
    for seg in [s for s in path.split("/") if s]:
        seg = seg.lower()
        seg = re.sub(r"\.(html?|php|aspx?)$", "", seg)
        if not seg or seg in _SKIP_SEG or seg.isdigit():
            continue
        toks = [t for t in re.split(r"[-_+.]", seg) if t]
        if not toks:
            continue
        if len(toks) == 1:
            t = toks[0]
            # drop hash-like ids and too-short filler fragments
            if (not t.isalpha()) or len(t) < 3 or _HEXISH.match(t):
                continue
        words.extend(toks)
    kw = " ".join(words).strip()
    return kw if len(kw) >= 3 else None


def _registrable(host: str) -> str:
    """Reduce a host to its registrable domain (eTLD+1), so subdomains of the
    same site (e.g. every `*.itch.io` creator page) count as one domain."""
    parts = [p for p in host.split(".") if p]
    if len(parts) <= 2:
        return host
    if ".".join(parts[-2:]) in _TWO_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _host(url: str) -> str:
    netloc = (urlsplit(url).netloc or "").lower()
    netloc = netloc.split("@")[-1].split(":")[0]
    return _registrable(netloc) if netloc else ""


# ---------------------------------------------------------------------------
# Sitemap fetch + parse
# ---------------------------------------------------------------------------
def parse_sitemap(text: str | None) -> list[str]:
    """Ordered, de-duplicated <loc> URLs from a sitemap/sitemapindex."""
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in re.findall(r"<loc>\s*(.*?)\s*</loc>", text, re.IGNORECASE | re.DOTALL):
        loc = raw.strip()
        if loc and loc not in seen:
            seen.add(loc)
            out.append(loc)
    return out


def _get(session, url: str, timeout: int = 30) -> str | None:
    try:
        r = session.get(url, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        log.warning("sitemap %s failed: %s", url, e)
        return None
    if getattr(r, "status_code", 200) != 200:
        log.warning("sitemap %s -> HTTP %s", url, getattr(r, "status_code", "?"))
        return None
    content = getattr(r, "content", None)
    if content and (url.endswith(".gz") or content[:2] == b"\x1f\x8b"):
        try:
            return gzip.decompress(content).decode("utf-8", "ignore")
        except OSError:
            pass
    return getattr(r, "text", "") or ""


def fetch_sitemap(session, url: str, *, max_children: int = 30, throttle: float = 0.0, depth: int = 0) -> list[str]:
    """Return the page URLs in a sitemap, following a sitemapindex one level deep."""
    text = _get(session, url)
    if text is None:
        return []
    locs = parse_sitemap(text)
    if not locs:
        return []
    if "<sitemapindex" in text.lower() and depth < 2:
        pages: list[str] = []
        for child in locs[:max_children]:
            if throttle:
                time.sleep(throttle)
            pages.extend(fetch_sitemap(session, child, max_children=max_children, throttle=throttle, depth=depth + 1))
        return pages
    return locs


# ---------------------------------------------------------------------------
# State + aggregation
# ---------------------------------------------------------------------------
def load_state(path: Path) -> dict:
    d = _load_json(Path(path), {"watches": {}})
    d.setdefault("watches", {})
    return d


def save_state(path: Path, state: dict) -> None:
    save_json(Path(path), state)


def _prune_urls(urls: dict, days: int, cap: int = 50000) -> dict:
    ref = datetime.now(timezone.utc)
    keep: dict[str, str] = {}
    for u, first in (urls or {}).items():
        if first == BASELINE_ISO:
            keep[u] = first  # baseline entries never age out
            continue
        try:
            if (ref - datetime.fromisoformat(first)).total_seconds() <= days * 86400:
                keep[u] = first
        except (ValueError, TypeError):
            keep[u] = first
    if len(keep) > cap:
        # recent (dated) entries sort first; baseline sentinels last.
        keep = dict(sorted(keep.items(), key=lambda kv: kv[1], reverse=True)[:cap])
    return keep


def _count_within(domains: dict[str, str], now: str, days: int) -> int:
    try:
        ref = datetime.fromisoformat(now)
    except (ValueError, TypeError):
        return len(domains)
    n = 0
    for first in domains.values():
        try:
            if (ref - datetime.fromisoformat(first)).total_seconds() <= days * 86400:
                n += 1
        except (ValueError, TypeError):
            pass
    return n


def export_items(watches: dict, now: str, *, min_words: int = 1) -> dict:
    """Aggregate keyword -> {domain: first_seen}, then rank by spread + recency."""
    agg: dict[str, dict[str, str]] = {}
    for url, w in watches.items():
        host = w.get("host") or _host(url)
        for page, first in (w.get("urls") or {}).items():
            kw = extract_keyword(page)
            if not kw or len(kw.split()) < min_words:
                continue
            d = agg.setdefault(kw, {})
            if host not in d or first < d[host]:
                d[host] = first

    items: list[dict] = []
    for kw, domains in agg.items():
        firsts = sorted(domains.values())
        earliest = firsts[0] if firsts else now
        is_baseline = earliest == BASELINE_ISO
        items.append({
            "q": kw,
            "domains": sorted(domains)[:8],
            "domain_count": len(domains),
            "domain_count_7d": _count_within(domains, now, 7),
            "domain_count_15d": _count_within(domains, now, 15),
            "domain_count_30d": _count_within(domains, now, 30),
            "first": None if is_baseline else earliest,
            "baseline": is_baseline,
            "new": (not is_baseline) and _count_within({"_": earliest}, now, 1) > 0,
        })
    items.sort(key=lambda r: (r["domain_count_7d"], r["domain_count"]), reverse=True)
    return {
        "updated": now,
        "watch_count": len(watches),
        "count": len(items),
        "items": items[:MAX_ITEMS],
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run(
    *,
    proxy: str | None,
    watch_urls: list[str],
    out_dir: Path,
    state_path: Path,
    throttle: float = 1.0,
    budget: int = 2000,
    max_sitemaps: int = 30,
    min_words: int = 1,
    use_llm: bool = True,
    llm_limit: int = 300,
    retention_days: int = 90,
) -> int:
    urls = [u.strip() for u in (watch_urls or []) if u.strip()]
    if not urls:
        log.info("sitemap: no watch_urls configured, nothing to do")
        return 0

    session = build_session(proxy)
    state = load_state(state_path)
    watches = state["watches"]
    now = now_iso()

    for u in urls:
        watch = watches.get(u) or {}
        prev = watch.get("urls", {})
        baselined = bool(watch.get("baselined"))
        pages = fetch_sitemap(session, u, max_children=max_sitemaps, throttle=throttle)
        if not pages:
            continue
        pages = pages[:budget]
        cur = dict(prev)
        added = 0
        for p in pages:
            if p not in cur:
                # First snapshot = baseline (old sentinel); later = real time.
                cur[p] = now if baselined else BASELINE_ISO
                if baselined:
                    added += 1
        cur = _prune_urls(cur, retention_days)
        watches[u] = {"host": _host(u), "urls": cur, "last": now,
                      "added": added, "total": len(cur), "baselined": True}
        log.info("sitemap %s: %d urls (+%d new)", u, len(cur), added)

    state["updated"] = now
    save_state(state_path, state)

    payload = export_items(watches, now, min_words=min_words)
    items = payload["items"]
    if items:
        enrich(items, use_llm=use_llm, cache_path=state_path.parent / "llm_cache.json", llm_limit=llm_limit)
    save_json(out_dir / "sitemap.json", payload)
    log.info("sitemap: %d watch domain(s), %d keywords exported", len(watches), len(items))
    return len(items)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Competitor-sitemap first-mover keyword radar.")
    p.add_argument("--proxy", default=None)
    p.add_argument("--watch", default="", help="comma-separated sitemap URLs")
    p.add_argument("--out", default=str(config.WEB_DATA_DIR))
    p.add_argument("--state", default=str(default_state_path()))
    p.add_argument("--throttle", type=float, default=1.0)
    p.add_argument("--budget", type=int, default=2000)
    p.add_argument("--max-sitemaps", type=int, default=30)
    p.add_argument("--min-words", type=int, default=1)
    p.add_argument("--max-llm", type=int, default=300)
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from . import settings

    cfg = settings.apply_environment()
    if args.watch:
        watch = [u.strip() for u in args.watch.split(",") if u.strip()]
    else:
        watch = [u.strip() for u in ((cfg.get("sitemap") or {}).get("watch_urls") or []) if u.strip()]

    run(
        proxy=args.proxy,
        watch_urls=watch,
        out_dir=Path(args.out),
        state_path=Path(args.state),
        throttle=args.throttle,
        budget=args.budget,
        max_sitemaps=args.max_sitemaps,
        min_words=args.min_words,
        use_llm=not args.no_llm,
        llm_limit=args.max_llm,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
