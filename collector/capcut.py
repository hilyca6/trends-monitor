"""CapCut template hotness monitor (the "most-used" template ranking).

Scrapes ``https://www.capcut.com/explore/most-used`` (the video template
ranking by default) and tracks *increments*: use-count growth, rank moves, and
newly entering templates. Runs on its own exit, independent of the Google/Bing
budgets.

The page ships a ``__MODERN_ROUTER_DATA__`` JSON payload with the SSR data, so
we read exact numeric counts (no "3M" abbreviation) and a stable ``templateId``
straight from it — the visible cards are only a fallback. Video templates carry
``useCount`` / ``likeCount`` / ``commentCount``; image templates have no usage
metric (empty interaction stats), so they are tracked by rank / entry only
(pass ``--types video,image`` to include them).

State (``state/capcut_state.json``) keeps per-template first/last seen,
sightings, current + previous use/like counts and rank. Output goes to
``web/data/capcut.json``:

    {updated, source, pages, types, baselined, count, items:[{
        id, title, url, type, uses, usesDelta, deltaPerHour, growth,
        likes, likesDelta, comments, rank, rankDelta,
        first, baseline, last, sightings, new, level
    }]}

Usage:
    python -m collector.capcut
    python -m collector.capcut --types video --pages 3
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urljoin

from . import config
from .http import build_session, get_text
from .store import _load_json, now_iso, prune_records, save_json

log = logging.getLogger("trends.capcut")

SOURCE_URL = "https://www.capcut.com/explore/most-used"
STATE_NAME = "capcut_state.json"
MAX_ITEMS = 500

# Level thresholds on the use-count growth ratio (fraction since the last run).
_SURGE_GROWTH = 0.10
_RISE_GROWTH = 0.02

# Embedded SSR data (exact counts + stable ids), preferred over the DOM.
_ROUTER_RE = re.compile(
    r'<script type="application/json" id="__MODERN_ROUTER_DATA__">(.*?)</script>', re.S
)
# DOM fallback (only stable class-name prefixes; the ``-xxxxxx`` hash changes).
_CARD_ANCHOR = re.compile(r'class="templates-lists__card[^"]*"\s+href="([^"]+)"')
_TITLE_RE = re.compile(r'templates-lists__titleText">([^<]*)<')
_USES_RE = re.compile(r'templates-lists__useCount">([^<]*)<')
_ID_RE = re.compile(r"/(\d+)(?:[?#].*)?$")
_USES_NUM_RE = re.compile(r"([\d.]+)\s*([KMB]?)", re.IGNORECASE)
_USES_MULT = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


def default_state_path() -> Path:
    return config.STATE_DIR / STATE_NAME


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def _as_int(v) -> int:
    try:
        return int(v)
    except (ValueError, TypeError):
        return 0


def _find_lists(obj, key: str) -> list:
    """Every list stored under ``key`` anywhere in the (nested) payload."""
    found: list = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and isinstance(v, list):
                found.append(v)
            else:
                found.extend(_find_lists(v, key))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(_find_lists(v, key))
    return found


def _normalize(rec: dict, template_type: str) -> dict | None:
    if not isinstance(rec, dict):
        return None
    tid = rec.get("templateId")
    if not tid:
        return None
    sd = rec.get("structuredData") or {}
    ic = sd.get("interactionStatistic") or {}
    uses = rec.get("useCount")
    if uses is None:
        uses = ic.get("useCount")
    likes = rec.get("likeCount")
    if likes is None:
        likes = ic.get("likeCount")
    url = sd.get("url") or sd.get("mainEntityOfPage") or ""
    if template_type == "video" and not url:
        url = f"https://www.capcut.com/template-detail/{tid}"
    title = (rec.get("title") or rec.get("titleDesc") or "").strip()
    desc = (rec.get("titleDesc") or sd.get("description") or "").strip()
    return {
        "id": str(tid),
        "title": title,
        "desc": desc if desc != title else "",
        "type": template_type,
        "url": url,
        "uses": _as_int(uses),
        "likes": _as_int(likes),
        "comments": _as_int(rec.get("commentCount")),
    }


def parse_router_data(html_text: str | None) -> list[dict]:
    """Templates from the embedded SSR JSON (both video and image lists)."""
    m = _ROUTER_RE.search(html_text or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return []
    cards: list[dict] = []
    for ttype, key in (("video", "videoTemplates"), ("image", "imageTemplates")):
        for lst in _find_lists(data, key):
            for rec in lst:
                c = _normalize(rec, ttype)
                if c:
                    cards.append(c)
    return cards


def parse_uses_text(text: str) -> int:
    """Parse an abbreviated use-count (``3M`` -> 3000000) from the DOM fallback."""
    m = _USES_NUM_RE.search(text or "")
    if not m:
        return 0
    try:
        return int(float(m.group(1)) * _USES_MULT.get(m.group(2).upper(), 1))
    except ValueError:
        return 0


def parse_cards_dom(html_text: str | None, template_type: str) -> list[dict]:
    """Fallback card parse (video only — image cards carry no template id)."""
    if not html_text or template_type != "video":
        return []
    matches = list(_CARD_ANCHOR.finditer(html_text))
    out: list[dict] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(html_text)
        block = html_text[start:end]
        href = urljoin(SOURCE_URL, m.group(1))
        tid = _ID_RE.search(href)
        if not tid or href == SOURCE_URL:
            continue
        mt = _TITLE_RE.search(block)
        mu = _USES_RE.search(block)
        out.append({
            "id": tid.group(1),
            "title": html_mod.unescape(mt.group(1)).strip() if mt else "",
            "type": template_type,
            "url": href,
            "uses": parse_uses_text(mu.group(1) if mu else ""),
            "likes": 0,
            "comments": 0,
        })
    return out


def build_url(template_type: str, page: int) -> str:
    q: dict[str, object] = {}
    if template_type:
        q["templateType"] = template_type
    if page and page > 1:
        q["page"] = page
    return SOURCE_URL + ("?" + urlencode(q) if q else "")


def fetch_cards(session, template_types: list[str], max_pages: int, throttle: float = 1.5) -> list[dict]:
    """Fetch each type's pages, then rank.

    The embedded JSON carries exact counts but lists templates in CapCut's raw
    (unsorted) order; the page itself displays them sorted by use-count desc.
    So rank each type by use-count descending to match what users see. Image
    templates have no use-count (all zero) and keep their source order.
    """
    seen: set[str] = set()
    per_type: dict[str, list[dict]] = {t: [] for t in template_types}
    for ttype in template_types:
        for page in range(1, max_pages + 1):
            text = get_text(session, build_url(ttype, page), throttle=throttle)
            cards = [c for c in parse_router_data(text) if c["type"] == ttype]
            if not cards:
                cards = parse_cards_dom(text, ttype)
            if not cards:
                break
            for c in cards:
                if c["id"] in seen:
                    continue
                seen.add(c["id"])
                per_type[ttype].append(c)

    out: list[dict] = []
    for ttype in template_types:
        lst = per_type[ttype]
        if any(c["uses"] for c in lst):
            lst.sort(key=lambda c: c["uses"], reverse=True)
        for i, c in enumerate(lst, 1):
            c["rank"] = i
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# State + export
# ---------------------------------------------------------------------------
def load_state(path: Path) -> dict:
    d = _load_json(Path(path), {"items": {}})
    if not isinstance(d, dict):
        d = {"items": {}}
    d.setdefault("items", {})
    return d


def save_state(path: Path, state: dict) -> None:
    save_json(Path(path), state)


def update(state: dict, cards: list[dict], now: str) -> set[str]:
    """Merge this round's cards into state; return ids seen for the first time.

    On the very first (baseline) run every card gets ``first=None`` so it is not
    later mistaken for a genuinely new entry. Each card also accumulates a
    ``history`` list of ``[iso, uses]`` points for the trend line.
    """
    items = state.setdefault("items", {})
    was_baseline = bool(state.get("baselined"))
    new_ids: set[str] = set()
    for c in cards:
        rec = items.get(c["id"])
        if rec is None:
            items[c["id"]] = {
                "title": c["title"],
                "desc": c.get("desc", ""),
                "url": c["url"],
                "type": c["type"],
                "uses": c["uses"],
                "likes": c["likes"],
                "comments": c["comments"],
                "prevUses": c["uses"],
                "prevLikes": c["likes"],
                "peakUses": c["uses"],
                "rank": c.get("rank"),
                "prevRank": c.get("rank"),
                "first": now if was_baseline else None,
                "prevLast": now,
                "last": now,
                "sightings": 1,
                "history": [[now, c["uses"]]],
            }
            new_ids.add(c["id"])
        else:
            rec["title"] = c["title"] or rec.get("title")
            rec["desc"] = c.get("desc") or rec.get("desc")
            rec["url"] = c["url"] or rec.get("url")
            rec["type"] = c["type"] or rec.get("type")
            rec["prevUses"] = rec.get("uses", c["uses"])
            rec["prevLikes"] = rec.get("likes", c["likes"])
            rec["peakUses"] = max(rec.get("peakUses", 0), c["uses"])
            rec["uses"] = c["uses"]
            rec["likes"] = c["likes"]
            rec["comments"] = c["comments"]
            rec["prevRank"] = rec.get("rank")
            rec["rank"] = c.get("rank")
            rec["prevLast"] = rec.get("last", now)
            rec["last"] = now
            rec["sightings"] = rec.get("sightings", 1) + 1
            hist = rec.setdefault("history", [])
            if not hist:
                hist.append([rec.get("prevLast") or now, rec.get("prevUses", c["uses"])])
            if hist[-1][1] != c["uses"] or hist[-1][0] != now:
                hist.append([now, c["uses"]])
    state["baselined"] = True
    return new_ids


def prune_history(state: dict, now: str, days: float, max_points: int = 500) -> int:
    """Drop history points older than ``days`` (and cap total points)."""
    try:
        cutoff = datetime.fromisoformat(now).timestamp() - days * 86400
    except (ValueError, TypeError):
        return 0
    removed = 0
    for rec in state.get("items", {}).values():
        hist = rec.get("history")
        if not hist:
            continue
        kept = []
        for p in hist:
            try:
                ts = datetime.fromisoformat(p[0]).timestamp()
            except (ValueError, TypeError, IndexError):
                kept.append(p)
                continue
            if ts >= cutoff:
                kept.append(p)
        if len(kept) > max_points:
            kept = kept[-max_points:]
        removed += len(hist) - len(kept)
        rec["history"] = kept
    return removed


def _hours_between(a: str, b: str) -> float:
    try:
        return max(0.0, (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 3600.0)
    except (ValueError, TypeError):
        return 0.0


def _level(delta: int, growth: float, is_new: bool) -> str:
    if is_new:
        return "new"
    if delta <= 0:
        return "steady"
    if growth >= _SURGE_GROWTH:
        return "surging"
    if growth >= _RISE_GROWTH:
        return "rising"
    return "up"


def export_items(state: dict, now: str, types: list[str], pages: int, max_items: int = MAX_ITEMS) -> dict:
    items: list[dict] = []
    for tid, rec in state.get("items", {}).items():
        uses = rec.get("uses", 0)
        prev = rec.get("prevUses", uses)
        delta = max(0, uses - prev)
        growth = (delta / prev) if prev else 0.0
        likes = rec.get("likes", 0)
        likes_delta = max(0, likes - rec.get("prevLikes", likes))
        hours = _hours_between(rec.get("prevLast") or now, rec.get("last") or now)
        dph = (delta / hours) if hours > 0 else 0.0
        first = rec.get("first")
        rank = rec.get("rank")
        prev_rank = rec.get("prevRank")
        rank_delta = (prev_rank - rank) if isinstance(prev_rank, int) and isinstance(rank, int) else 0
        hist = rec.get("history") or []
        base_uses = hist[0][1] if hist else uses
        gain_3d = max(0, uses - base_uses)
        growth_3d = (gain_3d / base_uses) if base_uses else 0.0
        items.append({
            "id": tid,
            "title": rec.get("title", ""),
            "desc": rec.get("desc", ""),
            "url": rec.get("url", ""),
            "type": rec.get("type", ""),
            "uses": uses,
            "usesDelta": delta,
            "deltaPerHour": round(dph, 1),
            "growth": round(growth, 4),
            "usesGain3d": gain_3d,
            "growth3d": round(growth_3d, 4),
            "likes": likes,
            "likesDelta": likes_delta,
            "comments": rec.get("comments", 0),
            "rank": rank,
            "rankDelta": rank_delta,
            "first": first,
            "baseline": first is None,
            "last": rec.get("last"),
            "sightings": rec.get("sightings", 1),
            "new": bool(first) and first == now,
            "level": _level(delta, growth, bool(first) and first == now),
            "history": hist,
        })
    items.sort(
        key=lambda r: (r["growth3d"], r["usesGain3d"], r["uses"]),
        reverse=True,
    )
    return {
        "updated": now,
        "source": SOURCE_URL,
        "pages": pages,
        "types": types,
        "baselined": bool(state.get("baselined")),
        "count": len(items),
        "items": items[:max_items],
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run(
    *,
    proxy: str | None = None,
    out_dir: Path,
    state_path: Path,
    template_types: list[str] | None = None,
    max_pages: int = 12,
    throttle: float = 1.5,
    retention_days: int = 90,
    history_days: float = 3.0,
) -> int:
    types = [t.strip() for t in (template_types or ["video"]) if t.strip()]
    if not types:
        log.info("capcut: no template types configured, nothing to do")
        return 0

    session = build_session(proxy)
    cards = fetch_cards(session, types, max(1, int(max_pages)), throttle)
    log.info("capcut: %d templates across %s (x%d pages)", len(cards), types, max_pages)
    if not cards:
        log.warning("capcut: no templates parsed (page layout changed or blocked)")
        return 0

    state = load_state(state_path)
    now = now_iso()
    new_ids = update(state, cards, now)
    prune_history(state, now, history_days)

    removed = prune_records(state.get("items", {}), retention_days)
    if removed:
        log.info("capcut: pruned %d stale templates", removed)

    state["updated"] = now
    save_state(state_path, state)

    payload = export_items(state, now, types, max(1, int(max_pages)))
    save_json(out_dir / "capcut.json", payload)
    log.info(
        "capcut: %d exported, %d new, %d tracked",
        len(payload["items"]), len(new_ids), len(state.get("items", {})),
    )
    return len(payload["items"])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="CapCut most-used template monitor.")
    p.add_argument("--proxy", default=None)
    p.add_argument("--types", default="video", help="comma-separated templateType values")
    p.add_argument("--pages", type=int, default=12)
    p.add_argument("--throttle", type=float, default=1.5)
    p.add_argument("--history-days", type=float, default=3.0)
    p.add_argument("--out", default=str(config.WEB_DATA_DIR))
    p.add_argument("--state", default=str(default_state_path()))
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run(
        proxy=args.proxy,
        out_dir=Path(args.out),
        state_path=Path(args.state),
        template_types=[t.strip() for t in args.types.split(",") if t.strip()],
        max_pages=args.pages,
        throttle=args.throttle,
        history_days=args.history_days,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
