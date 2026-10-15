"""One-shot scheduled job: collect trends + games, then optionally push.

Called by Windows Task Scheduler (see scripts/install_task.ps1). Reads
config.local.json for proxy + LLM key. Safe to run manually:

    python scripts/run_all.py
    python scripts/run_all.py --no-games
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from collector import (  # noqa: E402
    advice,  # noqa: E402
    capcut,  # noqa: E402
    config,
    discover,  # noqa: E402
    fastheat,  # noqa: E402
    proxy,  # noqa: E402
    settings,
    signal,  # noqa: E402
    sitemaps,  # noqa: E402
)
from collector.collect import resolve_geos  # noqa: E402
from collector.collect import run as run_trends  # noqa: E402
from collector.enrich import rule_noise  # noqa: E402
from collector.gameradar import opportunity as gameradar_opportunity  # noqa: E402
from collector.gameradar import radar as gameradar_radar  # noqa: E402
from collector.gameradar import social as gameradar_social  # noqa: E402
from collector.gameradar import store as gameradar_store  # noqa: E402
from collector.games import radar  # noqa: E402
from collector.gtrends_api import BlockedError, GoogleTrendsClient, last_blocked  # noqa: E402
from collector.http import build_session  # noqa: E402
from collector.push import send_text  # noqa: E402
from collector.related import expand as related_expand  # noqa: E402
from collector.related import is_useful_rising  # noqa: E402
from collector.seocheck import check_queries  # noqa: E402
from collector.store import _load_json, load_state, now_iso, save_json  # noqa: E402
from collector.suggest import expand as suggest_expand  # noqa: E402

LOG_DIR = ROOT / "logs"

log = logging.getLogger("trends.run")

# Stage groups for staggered scheduling (see scripts/install_task.ps1).
# "all" (default) runs every stage in one pass, like a manual `run_all.py`.
# Split groups let the heavy Google-Trends stages run in *different minutes* so
# the 3-hourly game radar does not burst against the same per-IP limit as the
# hourly signal check.
GROUPS: dict[str, "set[str] | None"] = {
    "all": None,
    "hourly": {"collect", "seo", "signal", "advice", "fast", "social", "opportunity", "capcut", "push", "persist"},
    "games": {"games", "gameradar", "advice", "opportunity", "persist"},
    "sitemap": {"sitemap"},
}


def _google_blocked() -> bool:
    """True once any Google path (explore API or SSR/suggest HTTP) got blocked.

    Used as a process-wide circuit breaker: after the first block the remaining
    Google-consuming stages are skipped this run instead of hammering the same
    (still blocked) exit IP. Non-Google stages (sitemap, gameradar) still run.
    """
    return last_blocked() is not None


def setup_logging(verbose: bool = False) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = RotatingFileHandler(LOG_DIR / "run.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)


def seo_postpass(cfg: dict, out_dir, state_path) -> int:
    """SERP opportunity + suggest long-tails + fast-heat for hot terms.

    Candidates = non-noise items that are new OR were seen within
    ``seo.refresh_hours`` (so a term keeps getting re-sampled for a rolling
    window, which is what lets the fast-heat layer diff new autocomplete/SERP
    entries). Results go back into trends.json (frontend/push) and state.json.
    """
    s = cfg.get("seo") or {}
    if not s.get("enabled", True):
        return 0

    trends = _load_json(out_dir / "trends.json", None)
    if not trends:
        return 0

    state = load_state(state_path)
    seen = state["seen"]
    now = now_iso()
    refresh_hours = float(s.get("refresh_hours", 24))

    def _recent(geo: str, q: str) -> bool:
        rec = seen.get(f"{geo}|{q}")
        if not rec:
            return False
        try:
            age = (datetime.fromisoformat(now) - datetime.fromisoformat(rec.get("last", ""))).total_seconds()
        except (ValueError, TypeError):
            return False
        return age <= refresh_hours * 3600

    rows: list[tuple[str, dict]] = [
        (g, it)
        for g, arr in (trends.get("items") or {}).items()
        for it in arr
        if not it.get("noise") and (it.get("new") or _recent(g, it["q"]))
    ]
    if not rows:
        return 0
    rows.sort(key=lambda r: r[1].get("vol") or 0, reverse=True)

    serp_budget = int(s.get("serp_budget", 20))
    suggest_budget = int(s.get("suggest_budget", 30))
    serp_qs: list[str] = []
    sug_qs: list[str] = []
    for _, it in rows:
        q = it["q"]
        if len(serp_qs) < serp_budget and q not in serp_qs:
            serp_qs.append(q)
        if len(sug_qs) < suggest_budget and q not in sug_qs:
            sug_qs.append(q)
    if not serp_qs and not sug_qs:
        return 0

    session = build_session(None)
    # Bing (SERP) must be reached directly; None makes seocheck build its own
    # proxy-free session. Suggest keeps the proxied session for Google.
    seo_map = (
        check_queries(
            None,
            serp_qs,
            throttle=float(s.get("serp_throttle", 3.0)),
            geo=str(s.get("geo", "US")),
            cache_path=state_path.parent / "seo_cache.json",
            cache_days=float(s.get("cache_days", 7)),
        )
        if serp_qs
        else {}
    )
    # Suggest hits Google, so skip it once we are already blocked; Bing above is
    # independent and still ran.
    lt_map: dict[str, list[str]] = {}
    if sug_qs and not _google_blocked():
        try:
            lt_map = suggest_expand(session, sug_qs)
        except BlockedError:
            log.warning("seo postpass: Google suggest blocked; skipping long-tails this run")

    n = 0
    for g, it in rows:
        q = it["q"]
        changed = False
        if q in seo_map:
            it["seo"] = seo_map[q]
            changed = True
        if q in lt_map:
            it["longtails"] = lt_map[q]
            changed = True
        if not changed:
            continue
        n += 1
        rec = seen.get(f"{g}|{q}")
        if rec is not None:
            if q in seo_map:
                rec["seo"] = seo_map[q]
            if q in lt_map:
                rec["longtails"] = lt_map[q]

    ts = now_iso()
    trends["updated"] = ts
    save_json(out_dir / "trends.json", trends)
    state["updated"] = ts
    save_json(state_path, state)
    log.info(
        "seo postpass: scored %d, longtails %d (of %d rows)",
        len(seo_map), len(lt_map), len(rows),
    )
    return n


def fast_postpass(cfg: dict, out_dir, state_path) -> int:
    """Fast-heat layer across all lists: diff autocomplete/SERP snapshots per term.

    Runs after advice so related/discovered/sitemap items already carry ``seo``
    (advice fills it for candidates missing it). Trends items also carry
    ``longtails`` (refreshed on a rolling window in seo_postpass).

    Steps: (1) gather the current autocomplete/SERP per query across all lists
    (a query appears under many geos — merge, don't diff per copy); (2) diff each
    query ONCE against its stored baseline; (3) write ``fast`` back to every item
    with that query. This keeps the geo-independent signal stable.
    """
    f = cfg.get("fast") or {}
    if not f.get("enabled", True):
        return 0

    now = now_iso()

    # 1) gather current values per query.
    files: dict[str, dict] = {}
    current: dict[str, dict] = {}
    for name, nested in (("trends.json", True), ("related.json", False),
                         ("discovered.json", False), ("sitemap.json", False),
                         ("gameradar.json", False)):
        d = _load_json(out_dir / name, None)
        if not isinstance(d, dict):
            continue
        files[name] = d
        items = ([it for arr in (d.get("items") or {}).values() for it in arr]
                 if nested else list(d.get("items") or []))
        for it in items:
            q = it.get("q")
            if not q:
                continue
            sug = it.get("longtails") or []
            serp = (it.get("seo") or {}).get("serpUrls") or []
            if not sug and not serp:
                continue
            e = current.setdefault(q, {"sug": [], "serp": []})
            for s in sug:
                if s not in e["sug"]:
                    e["sug"].append(s)
            for s in serp:
                if s not in e["serp"]:
                    e["serp"].append(s)

    if not current:
        return 0

    # 2) diff once per query.
    fast_state = fastheat.load(state_path.parent / "fast_state.json")
    heat_by_q: dict[str, dict] = {}
    for q, e in current.items():
        h = fastheat.heat(fast_state, q, e["sug"], e["serp"], now)
        if h and (h["newSuggestionCount"] or h["newSerpPageCount"]):
            heat_by_q[q] = h

    # 3) annotate every item carrying a query that had new activity.
    state = load_state(state_path)
    seen = state["seen"]
    n = 0
    for name, d in files.items():
        changed = False
        if name == "trends.json":
            for g, arr in (d.get("items") or {}).items():
                for it in arr:
                    h = heat_by_q.get(it.get("q"))
                    if not h:
                        continue
                    it["fast"] = h
                    n += 1
                    changed = True
                    rec = seen.get(f"{g}|{it['q']}")
                    if rec is not None:
                        rec["fast"] = h
        else:
            for it in (d.get("items") or []):
                h = heat_by_q.get(it.get("q"))
                if not h:
                    continue
                it["fast"] = h
                n += 1
                changed = True
                if name == "discovered.json":
                    rec = seen.get(it["q"])
                    if rec is not None:
                        rec["fast"] = h
        if changed:
            d["updated"] = now
            save_json(out_dir / name, d)

    fast_state["updated"] = now
    fastheat.save(state_path.parent / "fast_state.json", fast_state, days=int(cfg.get("retention_days", 90)))
    state["updated"] = now
    save_json(state_path, state)
    log.info("fast postpass: %d queries tracked, %d items with new activity",
             len(fast_state.get("entries") or {}), n)
    return n


def _non_latin(s: str) -> bool:
    """True if the string contains non-ASCII letters (CJK/Thai/... ).

    This project targets English SEO, so non-Latin rising words are dropped.
    """
    return any(ord(c) > 127 and c.isalpha() for c in s)


def _aggregate_rising(rows, rel) -> list[dict]:
    """Merge rising related queries across terms into a de-duped new-word list.

    Drops non-Latin words (English target) and tags rule-based noise so the UI
    and push can hide it.
    """
    agg: dict[str, dict] = {}
    for g, it in rows:
        data = rel.get(it["q"])
        if not data:
            continue
        for rq in data.get("rising", []):
            rq = (rq or "").strip()
            if not rq or _non_latin(rq) or not is_useful_rising(rq):
                continue
            e = agg.setdefault(rq.lower(), {"q": rq, "geos": [], "from": []})
            if g not in e["geos"]:
                e["geos"].append(g)
            if it["q"] not in e["from"]:
                e["from"].append(it["q"])
    items = list(agg.values())
    for e in items:
        e["noise"] = rule_noise(e["q"], [])
    items.sort(key=lambda e: (len(e["geos"]), len(e["from"])), reverse=True)
    return items


def related_postpass(cfg: dict, out_dir, state_path) -> int:
    """Mine Google Trends related queries for this round's discovered niche words.

    Re-sourced from discovered.json (niche, English, from your seeds) rather than
    the daily hot list — the latter's related queries are almost all brand /
    news / sports noise. Writes the aggregated rising words to related.json.
    """
    r = cfg.get("related") or {}
    if not r.get("enabled", True):
        return 0
    if _google_blocked():
        log.warning("related: skipped (Google already blocked this run)")
        return 0

    disc = _load_json(out_dir / "discovered.json", None)
    if not disc:
        return 0

    min_stars = int(r.get("min_stars", 0) or 0)

    def _stars(it: dict) -> int:
        return (it.get("advice") or {}).get("stars") or 0

    rows = [
        it
        for it in (disc.get("items") or [])
        if it.get("q") and not it.get("noise") and not _non_latin(it["q"])
        and _stars(it) >= min_stars
    ]
    if not rows:
        return 0
    # highest "worth doing" first (stars exist from a previous advice run)
    rows.sort(key=_stars, reverse=True)

    budget = int(r.get("budget", 20))
    window = str(r.get("window", "now 7-d"))
    throttle = float(r.get("throttle", 4.0))
    geo = str((cfg.get("seo") or {}).get("geo", "US"))

    qs: list[str] = []
    seen: set[str] = set()
    for it in rows:
        q = it["q"]
        if q in seen:
            continue
        seen.add(q)
        qs.append(q)
        if len(qs) >= budget:
            break
    if not qs:
        return 0

    session = build_session(None)
    client = GoogleTrendsClient(session, throttle)
    rel = related_expand(
        session, qs, geo=geo, window=window, throttle=throttle,
        cache_path=state_path.parent / "related_cache.json", client=client,
    )

    rising = _aggregate_rising([(geo, it) for it in rows], rel)
    if not rising:
        log.info("related postpass: no rising words")
        return 0
    save_json(out_dir / "related.json", {"updated": now_iso(), "items": rising})
    log.info("related postpass: %d niche words -> %d rising words", len(qs), len(rising))
    return len(rising)


def _signal_rank(name: str, it: dict) -> float:
    """Demand proxy used to spend the limited Trends budget on the best first."""
    if name == "trends.json":
        return float(it.get("vol") or 0)
    if name == "related.json":
        return float(len(it.get("geos") or [])) * 1000.0
    if name == "sitemap.json":
        return float((it.get("domain_count_7d") or 0) * 1000 + (it.get("domain_count") or 0))
    if name == "gameradar.json":
        # Games are the focus: web games / fresh finds / more-seen names first.
        return (
            4000.0 * (1 if it.get("web_game") else 0)
            + 1500.0 * (1 if it.get("new") else 0)
            + float(it.get("sightings") or 0) * 10.0
        )
    return 0.0  # discovered: no demand signal


def signal_postpass(cfg: dict, out_dir, state_path) -> int:
    """Baseline-relative Trends check (the "GPTs multiple") for this round's words.

    Fetches [term, support..., anchor] in one request over 7d/30d/90d, then writes
    a ``signal`` object (ratio / qualifiedRatio / classification / freshness /
    entityConflict / score) back into trends.json, related.json, discovered.json,
    sitemap.json and state.json.
    """
    s = cfg.get("signal") or {}
    if not s.get("enabled", True):
        return 0
    if _google_blocked():
        log.warning("signal: skipped (Google already blocked this run)")
        return 0

    budget = int(s.get("budget", 20))
    geo = str((cfg.get("seo") or {}).get("geo", "US"))
    recent_hours = float(s.get("recent_hours", 0) or 0)

    state = load_state(state_path)
    seen_recs = state["seen"]
    now = now_iso()

    def _recent(geo_key: str, q: str) -> bool:
        if recent_hours <= 0:
            return False
        rec = seen_recs.get(f"{geo_key}|{q}")
        if not rec:
            return False
        try:
            age = (datetime.fromisoformat(now) - datetime.fromisoformat(rec.get("last", ""))).total_seconds()
        except (ValueError, TypeError):
            return False
        return age <= recent_hours * 3600

    # Candidates: fresh / recent, non-noise words across the lists.
    cands: list[tuple[float, str, str, str]] = []  # (rank, q, channel, file)
    for name in ("trends.json", "related.json", "discovered.json", "sitemap.json", "gameradar.json"):
        d = _load_json(out_dir / name, None)
        if not isinstance(d, dict):
            continue
        if name == "trends.json":
            items = [(g, it) for g, arr in (d.get("items") or {}).items() for it in arr
                     if not it.get("noise") and (it.get("new") or _recent(g, it["q"]))]
        elif name in ("discovered.json", "gameradar.json"):
            # gameradar.json is a flat list like discovered; only fresh finds are checked.
            items = [(None, it) for it in (d.get("items") or [])
                     if not it.get("noise") and (it.get("new") or _recent(geo, it["q"]))]
        elif name == "sitemap.json":
            items = [(None, it) for it in (d.get("items") or []) if not it.get("noise")]
        else:  # related: no new flag, all non-noise are fair game
            items = [(None, it) for it in (d.get("items") or []) if not it.get("noise")]
        for _g, it in items:
            if it.get("q"):
                # Browser-game names use the online profile (anchor "crazy games").
                ch = "online" if name == "gameradar.json" else signal.guess_channel(it["q"])
                cands.append((_signal_rank(name, it), it["q"], ch, name))

    if not cands:
        return 0
    gr_cap = int(s.get("gameradar_budget", 8))
    gr_used = 0
    seen: set[str] = set()
    terms: list[dict] = []
    for _, q, ch, name in sorted(cands, key=lambda c: c[0], reverse=True):
        if q in seen:
            continue
        # Games are prioritised but capped so they can't crowd out every other list.
        if name == "gameradar.json":
            if gr_used >= gr_cap:
                continue
            gr_used += 1
        seen.add(q)
        terms.append({"q": q, "channel": ch})
        if len(terms) >= budget:
            break

    session = build_session(None)
    client = GoogleTrendsClient(session, float(s.get("throttle", 4.0)))
    result = signal.analyze(
        client,
        terms,
        geo=geo,
        baseline=str(s.get("baseline", "gpts")),
        anchors=s.get("anchors") or None,
        windows=s.get("windows") or None,
        throttle=float(s.get("throttle", 4.0)),
        breakout_ratio=float(s.get("breakout_ratio", 5.0)),
        rising_ratio=float(s.get("rising_ratio", 1.0)),
        cache_path=state_path.parent / "signal_cache.json",
        budget=budget,
        cache_days=int(cfg.get("retention_days", 90)),
    )
    if not result:
        log.info("signal postpass: no signal produced")
        return 0

    seenrec = seen_recs
    n = 0

    tp = out_dir / "trends.json"
    d = _load_json(tp, None)
    if isinstance(d, dict):
        for g, arr in (d.get("items") or {}).items():
            for it in arr:
                if result.get(it.get("q")):
                    it["signal"] = result[it["q"]]
                    rec = seenrec.get(f"{g}|{it['q']}")
                    if rec is not None:
                        rec["signal"] = it["signal"]
                    n += 1
        d["updated"] = now_iso()
        save_json(tp, d)

    for name, keyfn in (
        ("related.json", None),
        ("discovered.json", lambda it: it["q"]),
        ("sitemap.json", None),
        ("gameradar.json", None),
    ):
        p = out_dir / name
        d = _load_json(p, None)
        if not isinstance(d, dict):
            continue
        for it in (d.get("items") or []):
            sig = result.get(it.get("q"))
            if not sig:
                continue
            it["signal"] = sig
            n += 1
            if keyfn is not None:
                rec = seenrec.get(keyfn(it))
                if rec is not None:
                    rec["signal"] = sig
        d["updated"] = now_iso()
        save_json(p, d)

    state["updated"] = now_iso()
    save_json(state_path, state)
    log.info("signal postpass: %d terms checked, %d items annotated", len(result), n)
    return n


def discover_task(cfg: dict, out_dir, state_path) -> int:
    """Run off-list keyword discovery at most once per `interval_hours`."""
    d = cfg.get("discover") or {}
    if not d.get("enabled", True):
        return 0
    if _google_blocked():
        log.warning("discover: skipped (Google already blocked this run)")
        return 0
    sp = state_path.parent / "discover_state.json"
    state = discover.load_state(sp)
    now = now_iso()
    if not discover.is_due(state, now, float(d.get("interval_hours", 24))):
        log.info("discover: not due yet, skipping")
        return 0
    seeds = [s.strip() for s in (d.get("seeds") or []) if s.strip()]
    return discover.run(
        proxy=None,
        seeds=seeds,
        out_dir=out_dir,
        state_path=sp,
        throttle=float(d.get("throttle", 0.5)),
        budget=int(d.get("budget", 800)),
        use_llm=True,
        llm_limit=int(d.get("max_llm", 600)),
        retention_days=int(cfg.get("retention_days", 90)),
    )


def games_task(cfg: dict, out_dir) -> int:
    """Run the new-game radar at most once per games.interval_hours (default 3h).

    Games hit Google's interest API, which rate-limits hard, so it is decoupled
    from the hourly hot-word run and only triggers every few hours.
    """
    g = cfg.get("games") or {}
    if not g.get("enabled", True):
        return 0
    if _google_blocked():
        log.warning("games: skipped (Google already blocked this run)")
        return 0
    hours = float(g.get("interval_hours", 3))
    sp = config.STATE_DIR / "games_schedule.json"
    st = _load_json(sp, {})
    now = now_iso()
    if not discover.is_due(st if isinstance(st, dict) else {}, now, hours):
        log.info("games: not due yet (every %.0fh), skipping", hours)
        return 0
    n = radar.run(
        proxy=None,
        sources=[s.strip() for s in g.get("sources", "steam,appstore,trending").split(",") if s.strip()],
        max_chart=int(g.get("max_chart", 6)),
        throttle=float(g.get("throttle", 10.0)),
        out_dir=out_dir,
        state_path=config.GAMES_STATE_PATH,
        max_refresh=int(g.get("max_refresh", 6)),
        refresh_hours=float(g.get("refresh_hours", 12)),
        min_growth=float(g.get("min_growth", config.GAMES_CHART_MIN_GROWTH)),
        min_pos=float(g.get("min_pos", config.GAMES_CHART_MIN_POS)),
        max_hot=int(g.get("max_hot", config.GAMES_MAX_HOT_PER_RUN)),
        hot_growth=float(g.get("hot_growth", config.GAMES_HOT_GROWTH)),
        hot_rank_change=float(g.get("hot_rank_change", config.GAMES_HOT_RANK_CHANGE)),
        retention_days=int(cfg.get("retention_days", 90)),
        history_days=float(g.get("history_days", 7)),
    )
    save_json(sp, {"last_run": now})
    return n


def gameradar_task(cfg: dict, out_dir) -> int:
    """Run the game-name radar at most once per gameradar.interval_hours (default 3h).

    Discovers emerging web-game names from browser-game portals (itch.io,
    CrazyGames, Poki, Y8, GamePix, Lagged, Newgrounds) and writes
    web/data/gameradar.json. The advice / fastheat post-passes then enrich it.
    """
    g = cfg.get("gameradar") or {}
    if not g.get("enabled", True):
        return 0
    hours = float(g.get("interval_hours", 3))
    sp = config.STATE_DIR / "gameradar_schedule.json"
    st = _load_json(sp, {})
    now = now_iso()
    if not discover.is_due(st if isinstance(st, dict) else {}, now, hours):
        log.info("gameradar: not due yet (every %.0fh), skipping", hours)
        return 0
    yt = cfg.get("youtube") or {}
    yt_key = str(yt.get("api_key") or os.getenv("YOUTUBE_API_KEY") or "").strip()
    n = gameradar_radar.run(
        proxy=None,
        sources=[s.strip() for s in str(g.get("sources", "")).split(",") if s.strip()],
        out_dir=out_dir,
        state_path=config.GAMERADAR_STATE_PATH,
        throttle=float(g.get("throttle", 1.0)),
        retention_days=int(cfg.get("retention_days", 90)),
        youtube_api_key=yt_key if yt.get("enabled", True) else "",
        youtube_budget=int(yt.get("budget", 3)),
        youtube_throttle=float(yt.get("throttle", 1.0)),
        youtube_max_age_hours=float(yt.get("max_age_hours", 72)),
    )
    save_json(sp, {"last_run": now})
    return n


def capcut_task(cfg: dict, out_dir) -> int:
    """Run the CapCut most-used template monitor at most once per interval.

    Independent of the Google/Bing budgets, so it may run alongside the
    Trends-heavy hourly stages. Self-gates by ``capcut.interval_hours`` (default 4h).
    """
    c = cfg.get("capcut") or {}
    if not c.get("enabled", True):
        return 0
    hours = float(c.get("interval_hours", 4))
    sp = config.STATE_DIR / "capcut_schedule.json"
    st = _load_json(sp, {})
    now = now_iso()
    if not discover.is_due(st if isinstance(st, dict) else {}, now, hours):
        log.info("capcut: not due yet (every %.0fh), skipping", hours)
        return 0
    n = capcut.run(
        proxy=None,
        out_dir=out_dir,
        state_path=config.CAPCUT_STATE_PATH,
        template_types=[t.strip() for t in (c.get("template_types") or ["video", "image"]) if t.strip()],
        max_pages=int(c.get("max_pages", 12)),
        throttle=float(c.get("throttle", 1.5)),
        retention_days=int(cfg.get("retention_days", 90)),
        history_days=float(c.get("history_days", 3)),
    )
    save_json(sp, {"last_run": now})
    return n


def social_postpass(cfg: dict, out_dir) -> int:
    """Verify cross-platform social spillover for game-radar candidates.

    Runs after advice/fast so game names carry seo/signal/trends. Providers with
    no credentials are skipped. YouTube reuses the game radar's fresh data.
    """
    s = cfg.get("social") or {}
    if not s.get("enabled", True):
        return 0
    p = out_dir / "gameradar.json"
    d = _load_json(p, None)
    if not isinstance(d, dict):
        return 0
    items = d.get("items") or []
    if not items:
        return 0

    yt = cfg.get("youtube") or {}
    yt_key = str(yt.get("api_key") or os.getenv("YOUTUBE_API_KEY") or "").strip() if yt.get("enabled", True) else ""
    reddit_id = str(s.get("reddit_client_id") or os.getenv("REDDIT_CLIENT_ID") or "").strip()
    reddit_secret = str(s.get("reddit_client_secret") or os.getenv("REDDIT_CLIENT_SECRET") or "").strip()
    x_token = str(s.get("x_bearer_token") or os.getenv("X_BEARER_TOKEN") or "").strip()
    tt_token = str(s.get("tiktok_research_token") or os.getenv("TIKTOK_RESEARCH_TOKEN") or "").strip()
    if not (yt_key or (reddit_id and reddit_secret) or x_token or tt_token):
        log.info("social: no provider configured, skipping")
        return 0

    now = now_iso()
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    budget = max(0, int(s.get("budget", 5)))
    max_age_ms = float(s.get("max_age_hours", 6)) * 3600 * 1000
    reuse_ms = float(yt.get("max_age_hours", 72)) * 3600 * 1000
    timeout = float(s.get("timeout_ms", 20000)) / 1000.0
    throttle = float(s.get("throttle", 0.5))

    def _needs(it: dict) -> bool:
        checked = (it.get("social") or {}).get("checkedAt")
        if not checked:
            return True
        try:
            t = datetime.fromisoformat(checked).timestamp() * 1000
        except (ValueError, TypeError):
            return True
        return (now_ms - t) > max_age_ms

    def _priority(it: dict) -> float:
        sig = (it.get("signal") or {}).get("classification")
        return ((45 if sig in ("rising", "breakout") else 0)
                + float((it.get("fast") or {}).get("score") or 0)
                + (10 if it.get("new") else 0))

    cands = sorted((it for it in items if it.get("name") and _needs(it)), key=_priority, reverse=True)[:budget]
    if not cands:
        return 0

    n = 0
    for it in cands:
        providers, errors = gameradar_social.collect(
            it["name"],
            youtube_api_key=yt_key,
            reddit_client_id=reddit_id,
            reddit_client_secret=reddit_secret,
            reddit_user_agent=str(s.get("reddit_user_agent") or "trendradar/1.0"),
            x_bearer_token=x_token,
            tiktok_research_token=tt_token,
            timeout=timeout,
            throttle=throttle,
            existing_youtube=it.get("youtube"),
            youtube_max_age_ms=reuse_ms,
        )
        it["social"] = gameradar_social.analyze(providers, errors, min_platforms=int(s.get("min_platforms", 2)))
        yt_new = providers.get("youtube") or {}
        if yt_new.get("configured") and not yt_new.get("error"):
            it["youtube"] = {k: v for k, v in yt_new.items() if k != "configured"}
        n += 1
    d["updated"] = now
    save_json(p, d)
    log.info("social postpass: %d items checked", n)
    return n


def opportunity_postpass(cfg: dict, out_dir) -> int:
    """Opportunity score + hard gates for game-radar candidates.

    Runs after advice/signal/fast so each game name already carries ``seo``,
    ``signal``, ``advice`` and ``fast``. Social spillover is not wired yet, so
    ``allowsIndependent`` stays False; ``allowsTest`` works today.
    """
    o = cfg.get("opportunity") or {}
    if not o.get("enabled", True):
        return 0
    p = out_dir / "gameradar.json"
    d = _load_json(p, None)
    if not isinstance(d, dict):
        return 0
    items = d.get("items") or []
    if not items:
        return 0
    now = now_iso()
    n = 0
    for it in items:
        opp = gameradar_opportunity.analyze(it)
        opp["checkedAt"] = now
        it["opportunity"] = opp
        n += 1
    # Re-sort now that every enrichment field is present, so the exported order
    # reflects opportunity/market/competition (the export ran before them).
    gameradar_store.sort_items(items)
    d["items"] = items
    d["updated"] = now
    save_json(p, d)
    log.info("opportunity postpass: %d items scored", n)
    return n


def sitemap_task(cfg: dict, out_dir, state_path) -> int:
    """Run the competitor-sitemap radar at most once per sitemap.interval_hours.

    Watches a list of competitor sitemaps and ranks keywords by how many
    independent domains list them. A leading indicator, independent of Trends,
    so it does not consume the Google budget.
    """
    sm = cfg.get("sitemap") or {}
    if not sm.get("enabled", True):
        return 0
    urls = [u.strip() for u in (sm.get("watch_urls") or []) if u.strip()]
    if not urls:
        return 0
    sp = config.STATE_DIR / "sitemap_schedule.json"
    st = _load_json(sp, {})
    now = now_iso()
    if not discover.is_due(st if isinstance(st, dict) else {}, now, float(sm.get("interval_hours", 24))):
        log.info("sitemap: not due yet, skipping")
        return 0
    n = sitemaps.run(
        proxy=None,
        watch_urls=urls,
        out_dir=out_dir,
        state_path=config.STATE_DIR / "sitemap_state.json",
        throttle=float(sm.get("throttle", 1.0)),
        budget=int(sm.get("budget", 2000)),
        max_sitemaps=int(sm.get("max_sitemaps", 30)),
        min_words=int(sm.get("min_words", 1)),
        use_llm=True,
        llm_limit=int(sm.get("max_llm", 300)),
        retention_days=int(cfg.get("retention_days", 90)),
    )
    save_json(sp, {"last_run": now})
    return n


def _load_pushed(path) -> dict:
    d = _load_json(path, {})
    return d if isinstance(d, dict) else {}


def _save_pushed(path, pushed: dict, days: int = 90) -> None:
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    clean = {}
    for k, ts in pushed.items():
        try:
            if datetime.fromisoformat(ts).timestamp() >= cutoff:
                clean[k] = ts
        except (ValueError, TypeError):
            continue
    save_json(path, clean)


def _mark_pushed(path, keys, now) -> None:
    pushed = _load_pushed(path)
    for k in keys:
        pushed[k] = now
    _save_pushed(path, pushed)


def _in_quiet(cfg: dict) -> bool:
    """True when the current hour is inside push.quiet_hours (e.g. '23-8')."""
    q = (cfg.get("push") or {}).get("quiet_hours")
    if not q:
        return False
    try:
        a, b = (int(x) for x in str(q).split("-"))
    except (ValueError, TypeError):
        return False
    h = datetime.now().hour
    return (a <= h < b) if a <= b else (h >= a or h < b)


def _push_state_path(state_path):
    return Path(state_path).parent / "push_state.json"


def _hours(v):
    """Parse push.daily_at_hour into a sorted list of hours, or None.

    Accepts an int (7), a list ([5, 17]) or a string ("5,17").
    """
    if v is None:
        return None
    if isinstance(v, str):
        try:
            out = {int(x) for x in v.replace(" ", "").split(",") if x != ""}
        except (ValueError, TypeError):
            return None
    elif isinstance(v, (list, tuple)):
        out = set()
        for x in v:
            try:
                out.add(int(x))
            except (ValueError, TypeError):
                pass
    else:
        try:
            out = {int(v)}
        except (ValueError, TypeError):
            return None
    return sorted(h for h in out if 0 <= h <= 23) or None


def _due_slot(state_path, slots):
    """The latest configured slot <= now not yet pushed today, or None."""
    if not slots:
        return None
    today = datetime.now().date().isoformat()
    st = _load_json(_push_state_path(state_path), {})
    done = set(st.get("slots") or []) if isinstance(st, dict) and st.get("day") == today else set()
    h = datetime.now().hour
    due = [s for s in slots if s <= h and s not in done]
    return max(due) if due else None


def _stamp_slot(state_path, slot: int, slots) -> None:
    """Mark this slot (and every earlier one) as pushed for today."""
    today = datetime.now().date().isoformat()
    p = _push_state_path(state_path)
    st = _load_json(p, {})
    if not isinstance(st, dict) or st.get("day") != today:
        st = {"day": today, "slots": []}
    for s in slots:
        if s <= slot and s not in st["slots"]:
            st["slots"].append(s)
    st["last_ts"] = now_iso()
    save_json(p, st)


def _block_alert_path(state_path):
    return Path(state_path).parent / "block_alert.json"


def maybe_block_alert(cfg: dict, state_path) -> bool:
    """Send a one-off 'Google is rate-limiting' alert, at most once per day."""
    if not (cfg.get("push") or {}).get("enabled"):
        return False
    if not last_blocked():
        return False
    today = datetime.now().date().isoformat()
    p = _block_alert_path(state_path)
    st = _load_json(p, {})
    if isinstance(st, dict) and st.get("day") == today:
        return False
    pool = proxy.pool()
    rotated = f"本次已轮换代理出口 {pool.rotations} 次。" if pool.rotations else ""
    can_rotate = pool.can_rotate()
    hint = (
        "已配置多出口/切换端点，会自动轮换重试。"
        if can_rotate
        else "可在 config.local.json 配置 proxy 列表或 proxy_switch_url 以自动轮换。"
    )
    msg = (
        "⚠️ Google explore 开始限流（上升相关词 / 游戏曲线暂缓，非故障）。"
        + rotated + "\n" + hint + "通常几分钟~几小时自动恢复。"
    )
    if send_text(cfg, msg):
        save_json(p, {"day": today, "at": now_iso()})
        return True
    return False


_DEC_LABEL = {"test-now": "测试局", "independent": "正式独立站"}
_MARKET_LABEL = {"greenfield": "空白", "unconfirmed-new": "疑似新词", "contested": "早期竞争",
                 "occupied": "已被占", "established": "老词"}


def build_digest(state_path, top_trends: int = 8, top_games: int = 5,
                 top_gameradar: int = 5, top_sitemap: int = 5, min_stars: int = 0):
    """Return (text, new_keys). Items already pushed are skipped (anti-spam)."""
    lines: list[str] = []
    pushed_path = Path(state_path).parent / "pushed.json"
    pushed = _load_pushed(pushed_path)
    marked: list[str] = []
    now = now_iso()

    def star_num(it: dict) -> int:
        return (it.get("advice") or {}).get("stars") or 0

    def stars(it: dict) -> str:
        n = star_num(it)
        return ("★" * n + "☆" * (5 - n) + " ") if n else ""

    def brief(it: dict) -> str:
        a = it.get("advice") or {}
        s = ""
        if a.get("main_keyword"):
            s += " 主攻:" + a["main_keyword"]
        if a.get("window"):
            s += " 窗口:" + a["window"]
        return s

    def select(items, kind, key_fn, cap, sort_key=None):
        """By (sort_key, default stars) desc, skip already-pushed, cap, remember."""
        sk = sort_key or star_num
        chosen = []
        for it in sorted(items, key=sk, reverse=True):
            if len(chosen) >= cap:
                break
            k = kind + "|" + str(key_fn(it))
            if k in pushed:
                continue
            chosen.append(it)
            marked.append(k)
            pushed[k] = now
        return chosen

    def actionable(it: dict) -> bool:
        """Keep hot words only if ★ passes the bar or the trend is rising."""
        sig = it.get("signal") or {}
        return star_num(it) >= min_stars or sig.get("classification") in ("breakout", "rising")

    # 1) fresh hot words (noise + low-signal filtered)
    tp = config.WEB_DATA_DIR / "trends.json"
    if tp.exists():
        try:
            d = json.loads(tp.read_text(encoding="utf-8"))
            rows = [
                dict(it, _geo=g)
                for g, arr in (d.get("items") or {}).items()
                for it in arr
                if it.get("new") and not it.get("noise") and actionable(it)
            ]
            chosen = select(rows, "trend", lambda it: it["q"], top_trends)
            if chosen:
                lines.append(f"🌍 新热词 Top{len(chosen)}")
                for it in chosen:
                    zh = f" {it['zh']}" if it.get("zh") else ""
                    lines.append(f"· {stars(it)}[{it['_geo']}] {it['q']}{zh} (+{it.get('growth', 0)}%){brief(it)}")
        except (ValueError, OSError):
            pass

    # 2) new games
    gp = config.WEB_DATA_DIR / "games.json"
    if gp.exists():
        try:
            d = json.loads(gp.read_text(encoding="utf-8"))
            games = [it for it in (d.get("items") or []) if it.get("chart_at")]
            chosen = select(games, "game", lambda it: it["name"], top_games)
            if chosen:
                lines.append(f"\n🎮 新游戏 Top{len(chosen)}")
                for it in chosen:
                    bits = []
                    if it.get("breakout"):
                        bits.append("🔥爆发")
                    if it.get("rise"):
                        bits.append("环比+" + str(round(it["rise"] * 100)) + "%")
                    if it.get("growth"):
                        bits.append("涨幅+" + str(it["growth"]) + "%")
                    suf = (" " + " · ".join(bits)) if bits else ""
                    lines.append(f"· {stars(it)}{it['name']}{suf}{brief(it)}")
        except (ValueError, OSError):
            pass

    gr_items: list[dict] = []
    gr = config.WEB_DATA_DIR / "gameradar.json"
    if gr.exists():
        try:
            gr_items = json.loads(gr.read_text(encoding="utf-8")).get("items") or []
        except (ValueError, OSError):
            gr_items = []

    # 3) game radar: actionable decisions (test-now / independent)
    DEC = _DEC_LABEL
    MARKET = _MARKET_LABEL
    actionable_gr = [it for it in gr_items if (it.get("opportunity") or {}).get("decision") in DEC]
    chosen = select(
        actionable_gr, "gameradar", lambda it: it["name"], top_gameradar,
        sort_key=lambda it: (it.get("opportunity") or {}).get("score") or 0,
    )
    if chosen:
        lines.append(f"\n🎯 游戏雷达·可行动 Top{len(chosen)}")
        for it in chosen:
            opp = it.get("opportunity") or {}
            seo = it.get("seo") or {}
            dec = DEC.get(opp.get("decision"), opp.get("decision"))
            mkt = MARKET.get((seo.get("market") or {}).get("status"), "?")
            why = ((opp.get("reasons") or [None])[0]) or ""
            lines.append(
                f"· [{dec}] {opp.get('score')}分 {it['name']} · 市场:{mkt} · 风险:{seo.get('nameRisk', '?')}"
                + (f" · {why}" if why else "")
            )

    # 4) social spillover
    viral = [it for it in gr_items if (it.get("social") or {}).get("hasExternalSpillover")]
    chosen = select(
        viral, "social", lambda it: it["name"], top_gameradar,
        sort_key=lambda it: (it.get("social") or {}).get("score") or 0,
    )
    if chosen:
        lines.append(f"\n🔥 社媒外溢传播 Top{len(chosen)}")
        for it in chosen:
            soc = it.get("social") or {}
            lines.append(
                f"· {it['name']} · {soc.get('crossPlatformCount', 0)}平台/"
                f"{soc.get('independentCreatorCount', 0)}创作者 · {soc.get('score', 0)}分"
            )

    # 5) sitemap warming (first-mover lead)
    sp = config.WEB_DATA_DIR / "sitemap.json"
    if sp.exists():
        try:
            d = json.loads(sp.read_text(encoding="utf-8"))
            warm = [it for it in (d.get("items") or []) if (it.get("domain_count_7d") or 0) >= 1]
            chosen = select(
                warm, "sitemap", lambda it: it["q"], top_sitemap,
                sort_key=lambda it: (it.get("domain_count_7d") or 0, it.get("domain_count") or 0),
            )
            if chosen:
                lines.append(f"\n🚀 站点信号升温（别人开始抢）Top{len(chosen)}")
                for it in chosen:
                    lines.append(f"· {it['q']} · 收录{it.get('domain_count', 0)}站 · 7日+{it.get('domain_count_7d', 0)}")
        except (ValueError, OSError):
            pass

    return ("\n".join(lines) if lines else ""), marked


def build_urgent_alert(state_path, urgent_cfg: dict):
    """Return (text, keys) for immediate high-value alerts not yet pushed.

    Only the strongest, most time-sensitive signals — everything else waits for
    the scheduled digest. Dedup shares `pushed.json` with the digest (same kinds),
    so an item pushed here never re-appears in the digest.
    """
    if not (urgent_cfg or {}).get("enabled"):
        return "", []
    pushed = _load_pushed(Path(state_path).parent / "pushed.json")
    now = now_iso()
    lines: list[str] = []
    marked: list[str] = []

    def take(kind: str, key, line: str) -> None:
        k = f"{kind}|{key}"
        if k in pushed:
            return
        pushed[k] = now
        marked.append(k)
        lines.append(line)

    def load(name: str):
        p = config.WEB_DATA_DIR / name
        if not p.exists():
            return []
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            return d.get("items") or []
        except (ValueError, OSError):
            return []

    gr_items = load("gameradar.json")
    dec_allow = set(urgent_cfg.get("gameradar") or [])
    test_score = int(urgent_cfg.get("gameradar_test_score", 70))
    social_allow = set(urgent_cfg.get("social") or [])

    # 1) game-radar decisions
    for it in gr_items:
        opp = it.get("opportunity") or {}
        dec = opp.get("decision")
        if dec in dec_allow or (dec == "test-now" and (opp.get("score") or 0) >= test_score):
            seo = it.get("seo") or {}
            mkt = _MARKET_LABEL.get((seo.get("market") or {}).get("status"), "?")
            take("gameradar", it["name"],
                 f"🏆 {_DEC_LABEL.get(dec, dec)} {opp.get('score')}分 {it['name']} · 市场:{mkt} · 风险:{seo.get('nameRisk', '?')}")

    # 2) social buzz
    for it in gr_items:
        soc = it.get("social") or {}
        if soc.get("classification") in social_allow:
            take("social", it["name"],
                 f"🔥 社媒{soc.get('classification')} {it['name']} · {soc.get('crossPlatformCount', 0)}平台/"
                 f"{soc.get('independentCreatorCount', 0)}创作者 · {soc.get('score', 0)}分")

    # 3) new-game breakout
    if urgent_cfg.get("game_breakout", True):
        for it in load("games.json"):
            if it.get("chart_at") and it.get("breakout"):
                take("game", it["name"],
                     f"🎮 新游戏爆发 {it['name']} · 涨幅+{it.get('growth', 0)}% · 环比+{round((it.get('rise') or 0) * 100)}%")

    # 4) competitor sitemaps spiking
    sm_7d = int(urgent_cfg.get("sitemap_7d", 3) or 0)
    if sm_7d > 0:
        for it in load("sitemap.json"):
            if (it.get("domain_count_7d") or 0) >= sm_7d:
                take("sitemap", it["q"],
                     f"🚀 站点突增 {it['q']} · 收录{it.get('domain_count', 0)}站 · 7日+{it.get('domain_count_7d', 0)}")

    # 5) breakout hot words
    tb_stars = int(urgent_cfg.get("trend_breakout_stars", 4) or 0)
    if tb_stars > 0:
        trends = config.WEB_DATA_DIR / "trends.json"
        if trends.exists():
            try:
                d = json.loads(trends.read_text(encoding="utf-8"))
                for g, arr in (d.get("items") or {}).items():
                    for it in arr:
                        sig = it.get("signal") or {}
                        stars = (it.get("advice") or {}).get("stars") or 0
                        if it.get("new") and not it.get("noise") and sig.get("classification") == "breakout" and stars >= tb_stars:
                            take("trend", it["q"], f"🌍 强爆发新词 [{g}] {it['q']} (+{it.get('growth', 0)}%) ★{stars}")
            except (ValueError, OSError):
                pass

    if not lines:
        return "", []
    return "🚨【紧急】即时提醒\n" + "\n".join(lines), marked


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="TrendRadar scheduled run.")
    p.add_argument("--no-trends", action="store_true")
    p.add_argument("--no-games", action="store_true")
    p.add_argument("--no-gameradar", action="store_true")
    p.add_argument("--no-capcut", action="store_true")
    p.add_argument("--no-opportunity", action="store_true")
    p.add_argument("--no-social", action="store_true")
    p.add_argument("--no-push", action="store_true")
    p.add_argument("--no-discover", action="store_true")
    p.add_argument("--no-advice", action="store_true")
    p.add_argument("--no-signal", action="store_true")
    p.add_argument("--no-sitemap", action="store_true")
    p.add_argument("--no-fast", action="store_true")
    p.add_argument("--group", choices=sorted(GROUPS), default="all",
                   help="run only one stage group (for staggered scheduling)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    setup_logging(args.verbose)
    log = logging.getLogger("trends.run")
    started = time.time()

    active = GROUPS.get(args.group)

    def on(name: str) -> bool:
        return active is None or name in active

    cfg = settings.apply_environment()
    log.info("=== TrendRadar run start (group=%s) ===", args.group)
    log.info("geos=%s games=%s llm_key=%s", cfg.get("geos"), (cfg.get("games") or {}).get("enabled"), bool((cfg.get("llm") or {}).get("api_key")))

    if not args.no_trends and on("collect"):
        try:
            n = run_trends(
                resolve_geos(cfg.get("geos", "default")),
                proxy=None,
                provider_name="ssr",
                use_llm=True,
                out_dir=config.WEB_DATA_DIR,
                state_path=config.STATE_DIR / "state.json",
                retention_days=int(cfg.get("retention_days", 90)),
            )
            log.info("trends done: %d items", n)
        except Exception as e:  # noqa: BLE001
            log.exception("trends failed: %s", e)

    if on("seo"):
        try:
            n = seo_postpass(cfg, config.WEB_DATA_DIR, config.STATE_DIR / "state.json")
            if n:
                log.info("seo postpass done: %d items annotated", n)
        except Exception as e:  # noqa: BLE001
            log.exception("seo postpass failed: %s", e)

    if not args.no_games and on("games"):
        try:
            n = games_task(cfg, config.WEB_DATA_DIR)
            if n:
                log.info("games done: %d items", n)
        except Exception as e:  # noqa: BLE001
            log.exception("games failed: %s", e)

    if not args.no_gameradar and on("gameradar"):
        try:
            n = gameradar_task(cfg, config.WEB_DATA_DIR)
            if n:
                log.info("gameradar done: %d items", n)
        except Exception as e:  # noqa: BLE001
            log.exception("gameradar failed: %s", e)

    if not args.no_sitemap and on("sitemap"):
        try:
            n = sitemap_task(cfg, config.WEB_DATA_DIR, config.STATE_DIR / "state.json")
            if n:
                log.info("sitemap done: %d keywords", n)
        except Exception as e:  # noqa: BLE001
            log.exception("sitemap failed: %s", e)

    if not args.no_capcut and on("capcut"):
        try:
            n = capcut_task(cfg, config.WEB_DATA_DIR)
            if n:
                log.info("capcut done: %d templates", n)
        except Exception as e:  # noqa: BLE001
            log.exception("capcut failed: %s", e)

    if not args.no_discover and on("discover"):
        try:
            n = discover_task(cfg, config.WEB_DATA_DIR, config.STATE_DIR / "state.json")
            if n:
                log.info("discover done: %d words exported", n)
        except Exception as e:  # noqa: BLE001
            log.exception("discover failed: %s", e)

    # related runs AFTER discover so it mines the fresh niche words.
    if on("related"):
        try:
            n = related_postpass(cfg, config.WEB_DATA_DIR, config.STATE_DIR / "state.json")
            if n:
                log.info("related postpass done: %d rising words", n)
        except Exception as e:  # noqa: BLE001
            log.exception("related postpass failed: %s", e)

    # signal runs after related so it can compare every fresh word vs the baseline.
    if not args.no_signal and on("signal"):
        try:
            n = signal_postpass(cfg, config.WEB_DATA_DIR, config.STATE_DIR / "state.json")
            if n:
                log.info("signal postpass done: %d items annotated", n)
        except Exception as e:  # noqa: BLE001
            log.exception("signal postpass failed: %s", e)

    if not args.no_advice and on("advice"):
        try:
            n = advice.run(cfg, config.WEB_DATA_DIR, config.STATE_DIR / "state.json")
            if n:
                log.info("advice done: %d keywords", n)
        except Exception as e:  # noqa: BLE001
            log.exception("advice failed: %s", e)

    # fast runs after advice so related/discovered/sitemap already carry `seo`.
    if not args.no_fast and on("fast"):
        try:
            n = fast_postpass(cfg, config.WEB_DATA_DIR, config.STATE_DIR / "state.json")
            if n:
                log.info("fast postpass done: %d items", n)
        except Exception as e:  # noqa: BLE001
            log.exception("fast postpass failed: %s", e)

    # social runs before opportunity so `social` is present for the gates.
    if not args.no_social and on("social"):
        try:
            n = social_postpass(cfg, config.WEB_DATA_DIR)
            if n:
                log.info("social postpass done: %d items", n)
        except Exception as e:  # noqa: BLE001
            log.exception("social postpass failed: %s", e)

    # opportunity runs last so game names carry seo/signal/advice/fast.
    if not args.no_opportunity and on("opportunity"):
        try:
            n = opportunity_postpass(cfg, config.WEB_DATA_DIR)
            if n:
                log.info("opportunity postpass done: %d items", n)
        except Exception as e:  # noqa: BLE001
            log.exception("opportunity postpass failed: %s", e)

    # Persist game-radar enrichment into state so the next re-discovery export
    # (gameradar_task) does not wipe seo/advice/signal/social/opportunity.
    if not args.no_gameradar and on("persist"):
        try:
            n = gameradar_radar.persist(config.WEB_DATA_DIR, config.GAMERADAR_STATE_PATH)
            if n:
                log.info("gameradar persist done: %d items", n)
        except Exception as e:  # noqa: BLE001
            log.exception("gameradar persist failed: %s", e)

    if not args.no_push and on("push"):
        try:
            if maybe_block_alert(cfg, config.STATE_DIR / "state.json"):
                log.info("block alert sent (Google rate-limiting)")
        except Exception as e:  # noqa: BLE001
            log.exception("block alert failed: %s", e)

    # Immediate high-value alerts (do not wait for the scheduled digest).
    if not args.no_push and on("push"):
        urgent = (cfg.get("push") or {}).get("urgent") or {}
        if urgent.get("enabled"):
            state_path = config.STATE_DIR / "state.json"
            try:
                utext, ukeys = build_urgent_alert(state_path, urgent)
                if utext and (urgent.get("ignore_quiet") or not _in_quiet(cfg)):
                    if send_text(cfg, utext):
                        _mark_pushed(Path(state_path).parent / "pushed.json", ukeys, now_iso())
                        log.info("urgent push sent (%d items)", len(ukeys))
                    else:
                        log.info("urgent push: no channel or send failed")
                elif utext:
                    log.info("urgent alert held (quiet hours)")
            except Exception as e:  # noqa: BLE001
                log.exception("urgent push failed: %s", e)

    if not args.no_push and on("push"):
        p = cfg.get("push") or {}
        state_path = config.STATE_DIR / "state.json"
        slots = _hours(p.get("daily_at_hour"))
        if slots:
            slot = _due_slot(state_path, slots)
            allowed = slot is not None
            top_trends, top_games = 20, 10
            reason = f"slots {slots}"
        else:
            slot = None
            allowed = not _in_quiet(cfg)
            top_trends, top_games = 8, 5
            reason = "quiet hours"
        if not allowed:
            log.info("push: %s not due, skipped", reason)
        else:
            try:
                digest, keys = build_digest(
                    state_path,
                    top_trends=top_trends,
                    top_games=top_games,
                    top_gameradar=int(p.get("top_gameradar", 5)),
                    top_sitemap=int(p.get("top_sitemap", 5)),
                    min_stars=int(p.get("min_stars", 0)),
                )
                if digest:
                    digest = "📊 每日摘要\n" + digest
                if last_blocked():
                    warn = "⚠️ Google explore 限流中：上升相关词 / 游戏曲线本轮暂缓（非故障，稍后自动恢复）"
                    digest = (warn + "\n\n" + digest).rstrip() if digest else warn
                if digest and send_text(cfg, digest):
                    _mark_pushed(config.STATE_DIR / "pushed.json", keys, now_iso())
                    if slot is not None:
                        _stamp_slot(state_path, slot, slots)
                    log.info("push sent (%d new items)", len(keys))
                elif digest:
                    log.info("push disabled or no channel configured")
                else:
                    log.info("push: nothing new to send")
            except Exception as e:  # noqa: BLE001
                log.exception("push failed: %s", e)

    log.info("=== run finished in %.1fs ===", time.time() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
