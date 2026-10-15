"""Game-radar state + ``gameradar.json`` export.

State shape (``state/gameradar_state.json``)::

    {"games": {"<norm name>": {name, first, last, sightings, sources[], url}}}

Export shape (``web/data/gameradar.json``), matching related/discovered/sitemap::

    {"updated": iso, "items": [{name, first, last, sightings, sources[], url,
                                web_game, new}]}
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..games.store import is_junk, is_web_game
from ..store import prune_records, save_json
from . import signals
from .sources import is_probably_movie

log = logging.getLogger("trends.gameradar.store")

# Enrichment fields added by the pipeline post-passes; carried forward across
# re-discovery so a fresh export does not drop them.
_CARRY_FIELDS = ("seo", "advice", "signal", "social", "opportunity")

# Actionability ordering: put names you can actually decide on first (a real
# opportunity/stars/trend), and only then the raw "newest" ones.
_DECISION_RANK = {"independent": 4, "test-now": 3, "watch": 2, "reject": 1}
_SIGNAL_RANK = {"breakout": 6, "rising": 5, "strong": 4, "moderate": 3, "weak": 2, "none": 1}
_MARKET_RANK = {"greenfield": 3, "contested": 2, "occupied": 1}


def _sort_key(r: dict) -> tuple:
    opp = r.get("opportunity") or {}
    adv = r.get("advice") or {}
    sig = r.get("signal") or {}
    seo = r.get("seo") or {}
    market = (seo.get("market") or {}).get("status")
    return (
        _DECISION_RANK.get(opp.get("decision"), 0),
        opp.get("score") or 0,
        adv.get("stars") or 0,
        _MARKET_RANK.get(market, 0),      # blank market beats already-occupied
        seo.get("score") or 0,            # easier to rank first
        _SIGNAL_RANK.get(sig.get("classification"), 0),
        bool(r.get("new")),
        bool(r.get("web_game")),
        r.get("sightings") or 0,
    )


def sort_items(items: list[dict]) -> list[dict]:
    """Sort in place: most actionable / easiest-to-rank first."""
    items.sort(key=_sort_key, reverse=True)
    return items


def norm(name: str) -> str:
    return " ".join((name or "").strip().lower().split())


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            import json

            data = json.loads(path.read_text(encoding="utf-8"))
            data.setdefault("games", {})
            return data
        except (ValueError, OSError) as e:  # noqa: BLE001
            log.warning("cannot read %s: %s", path, e)
    return {"games": {}}


def save_state(path: Path, state: dict) -> None:
    save_json(path, state)


def update(state: dict, candidates: list[dict], now: str) -> set[str]:
    """Merge this round's candidates into state; return the keys seen for the first time.

    A candidate is ``{name, source, url}``. ``sightings`` counts the number of
    rounds a name was seen (a rough popularity/confidence proxy when there is
    no traffic data yet).
    """
    games = state.setdefault("games", {})

    merged: dict[str, dict] = {}
    for c in candidates:
        key = norm(c.get("name", ""))
        if not key:
            continue
        m = merged.setdefault(key, {"name": (c["name"] or "").strip(), "sources": [], "url": None})
        if c.get("source") and c["source"] not in m["sources"]:
            m["sources"].append(c["source"])
        if c.get("url") and not m["url"]:
            m["url"] = c["url"]

    new_keys: set[str] = set()
    for key, m in merged.items():
        rec = games.get(key)
        if rec is None:
            new_keys.add(key)
            games[key] = {
                "name": m["name"],
                "first": now,
                "last": now,
                "sightings": 1,
                "sources": list(m["sources"]),
                "sourceFirst": {s: now for s in m["sources"]},
                "url": m["url"],
            }
        else:
            rec["last"] = now
            rec["sightings"] = rec.get("sightings", 0) + 1
            rec.setdefault("sourceFirst", {})
            for s in m["sources"]:
                if s not in rec.setdefault("sources", []):
                    rec["sources"].append(s)
                    rec["sourceFirst"][s] = now
                else:
                    # Legacy records: backfill with the name's first-seen so an
                    # old source is not mistaken for a fresh platform today.
                    rec["sourceFirst"].setdefault(s, rec.get("first") or now)
            if m["url"] and not rec.get("url"):
                rec["url"] = m["url"]
    return new_keys


def prune_state(state: dict, days: int = 90) -> int:
    n = prune_records(state.get("games", {}), days)
    if n:
        log.info("pruned %d stale game names", n)
    return n


def export(state: dict, now: str, previous: dict | None = None, max_items: int = 300) -> dict:
    """Build gameradar.json from state.

    ``previous`` is the last exported payload (``name -> item``); the enrichment
    fields that the pipeline post-passes add (seo/advice/signal/social/
    opportunity) are carried forward so a re-discovery run does not wipe them.
    """
    now_ms = signals.to_ms(now)
    items: list[dict] = []
    for rec in state.get("games", {}).values():
        name = rec.get("name", "")
        if not name or is_junk(name) or is_probably_movie(name):
            continue
        item = {
            "name": name,
            "q": name,  # alias so the q-keyed post-passes (advice/fast) can reuse it
            "first": rec.get("first"),
            "last": rec.get("last"),
            "sightings": rec.get("sightings", 1),
            "sources": rec.get("sources", []),
            "url": rec.get("url"),
            "web_game": is_web_game(name),
            "new": rec.get("first") == now,
            "fast": signals.compute_fast(rec, now_ms),
            "youtube": rec.get("youtube"),
        }
        prev = (previous or {}).get(norm(name))
        if prev:
            for field in _CARRY_FIELDS:
                if prev.get(field) is not None:
                    item[field] = prev[field]
            if item.get("youtube") is None and prev.get("youtube"):
                item["youtube"] = prev["youtube"]
        # Enrichment persisted in state (survives even when an item falls out of
        # the export window on a run that adds many new names).
        for field in _CARRY_FIELDS:
            if item.get(field) is None and rec.get(field) is not None:
                item[field] = rec[field]
        items.append(item)
    # Most actionable first (opportunity/market/competition/stars/trend).
    sort_items(items)
    return {"updated": now, "items": items[:max_items]}


def persist_enrichment(state: dict, items: list[dict]) -> int:
    """Write post-pass enrichment from an exported payload back into state.

    Called at the end of the pipeline so seo/advice/signal/social/opportunity
    survive the next re-discovery export even for items outside the top window.
    """
    games = state.get("games", {})
    n = 0
    for it in items:
        key = norm(it.get("name", ""))
        rec = games.get(key)
        if not rec:
            continue
        for field in _CARRY_FIELDS:
            if it.get(field) is not None:
                rec[field] = it[field]
        if it.get("youtube"):
            rec["youtube"] = it["youtube"]
        n += 1
    return n
