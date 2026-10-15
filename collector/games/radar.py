"""New-game radar CLI.

Discover new-game candidates, snapshot each one's 7-day Google Trends curve,
track first-seen / sightings, and write web/data/games.json.

Usage:
    python -m collector.games.radar --proxy http://127.0.0.1:7897
    python -m collector.games.radar --sources steam,trending --max-chart 15
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from .. import config
from ..gtrends_api import BlockedError
from ..http import build_session
from ..store import now_iso, save_json
from .gtrends import GoogleTrendsInterest
from .sources import appstore_new, from_trending_games, steam_new
from .store import (
    downsample,
    export_games,
    is_junk,
    is_web_game,
    load_games_state,
    prune_state,
    rise_ratio,
    save_games_state,
    score_series,
)

log = logging.getLogger("trends.games.radar")

DEFAULT_SOURCES = ["steam", "appstore", "trending"]


def _norm(name: str) -> str:
    return " ".join(name.strip().lower().split())


def _priority(m: dict) -> int:
    """Lower = chart sooner. Web games and trending (real Google volume) first."""
    if is_web_game(m["name"]):
        return -1
    best = 99
    for s in m["sources"]:
        if s.startswith("trending"):
            best = min(best, 0)
        elif s == "steam_new":
            best = min(best, 1)
        elif s == "steam_soon":
            best = min(best, 2)
        elif s.startswith("appstore"):
            best = min(best, 3)
    return best


def _is_due(chart_at: str | None, now: str, hours: float) -> bool:
    """True if a game hasn't been (re)sampled within `hours`."""
    if not chart_at:
        return True
    try:
        return (datetime.fromisoformat(now) - datetime.fromisoformat(chart_at)).total_seconds() >= hours * 3600
    except (ValueError, TypeError):
        return True


def rank_change(prev_rank, prev_size, rank, size) -> float:
    """Normalised climb in a ranked list, run-to-run.

    Positions are normalised to 0..1 (0 = top, 1 = bottom) so lists of
    different depths compare; the result is *previous position - current
    position*, so a positive value means the game moved **toward the top**
    (a real, pre-curve growth signal for Steam/App Store list candidates).
    Returns 0.0 when either side lacks a rank.
    """
    if not (prev_rank and prev_size and rank and size):
        return 0.0
    return round((prev_rank - 1) / prev_size - (rank - 1) / size, 4)


def _rank_pos(m: dict) -> float:
    """0..1 position in the best ranked list this candidate appeared in (1 = top)."""
    rank, size = m.get("rank"), m.get("size")
    if rank and size:
        return round(1.0 - (rank - 1) / size, 4)
    return 0.0


def _heat(m: dict) -> float:
    """Magnitude of the rise; used to decide who gets sampled first.

    Takes the larger of the 环比 (rise, a ratio -> %), Google's 涨幅 (growth %)
    and the normalised chart climb (rankChange, scaled) so *every* candidate —
    including Steam/App Store ones with no Google growth — has a pre-curve score
    to sort by.
    """
    base = max((m.get("rise") or 0) * 100, m.get("growth") or 0)
    return max(base, (m.get("rankChange") or 0) * 1000)


def _base_key(kv) -> tuple:
    """Default ordering: biggest heat, then best source, then best chart position."""
    m = kv[1]
    return (-_heat(m), _priority(m), -_rank_pos(m))


def _age_hours(key: str, games: dict, now: str) -> float:
    """Hours a candidate has been waiting (uncharted) since first seen."""
    first = (games.get(key) or {}).get("first")
    if not first:
        return 0.0
    try:
        return max(0.0, (datetime.fromisoformat(now) - datetime.fromisoformat(first)).total_seconds() / 3600)
    except (ValueError, TypeError):
        return 0.0


def _rec_candidate(rec: dict) -> dict:
    """A merged-style candidate built from a stored (pending) game rec.

    Lets the chart pool include games that are no longer offered by any source
    (e.g. a one-off Google-trending term) so they still get a curve eventually.
    """
    return {
        "name": rec.get("name", ""),
        "sources": rec.get("sources") or [],
        "url": rec.get("url"),
        "growth": rec.get("growth") or 0,
        "hasGrowth": bool(rec.get("growth")),
        "zh": rec.get("zh", ""),
        "rank": rec.get("rank"),
        "size": rec.get("size"),
        "rankChange": rec.get("rankChange") or 0.0,
        "rise": rec.get("rise") or 0,
    }


def _skip_low_value(
    m: dict,
    min_growth: float = config.GAMES_CHART_MIN_GROWTH,
    min_pos: float = config.GAMES_CHART_MIN_POS,
) -> bool:
    """True when a candidate is already known to be too weak for a curve.

    Only applies when the signal actually exists: ``hasGrowth`` means a Google
    growth came with the candidate, and a rank only exists for list-sourced
    games. Web games (no growth, no rank) are never skipped.
    """
    if m.get("hasGrowth") and (m.get("growth") or 0) < min_growth:
        return True
    if m.get("rank") and m.get("size") and _rank_pos(m) <= min_pos:
        return True
    return False


def _is_hot(
    m: dict,
    hot_growth: float = config.GAMES_HOT_GROWTH,
    hot_rank_change: float = config.GAMES_HOT_RANK_CHANGE,
) -> bool:
    """A big, fresh mover worth re-pulling *every* run: a clear Google growth
    (>= hot_growth) or a sharp chart climb (rankChange >= hot_rank_change)."""
    if m.get("hasGrowth") and (m.get("growth") or 0) >= hot_growth:
        return True
    return (m.get("rankChange") or 0) >= hot_rank_change


def _chart_plan(
    merged: dict,
    games: dict,
    max_chart: int,
    now: str,
    min_growth: float = config.GAMES_CHART_MIN_GROWTH,
    min_pos: float = config.GAMES_CHART_MIN_POS,
) -> list[str]:
    """Choose, in priority order, which *uncharted* games get a curve this run.

    1. web games (special)                     — always first
    2. Google breakouts (growth >= 500%)
    3. the 5 biggest chart climbs (rankChange)
    4. the rest — existing logic, plus an aging term so games that never charted
       (no curve yet) eventually win a slot, i.e. coverage converges over runs.

    Candidates already known to be weak are dropped first (see
    :func:`_skip_low_value`): a known Google growth below 200% or a ranked-list
    position in the bottom 30% gives up its curve to a more promising game.
    """
    if max_chart <= 0:
        return set()
    specials, growths, climbs, rest = [], [], [], []
    for key, m in merged.items():
        rec = games.get(key)
        if rec is not None and rec.get("chart_at") is not None:
            continue  # already has a curve
        if _skip_low_value(m, min_growth, min_pos):
            continue  # weak signal / buried in the list -> don't spend a curve
        if is_web_game(m["name"]):
            specials.append((key, m))
        elif (m.get("growth") or 0) >= 500:
            growths.append((key, m))
        elif (m.get("rankChange") or 0) > 0:
            climbs.append((key, m))
        else:
            rest.append((key, m))

    for lst in (specials, growths, climbs):
        lst.sort(key=_base_key)
    plan = [k for k, _ in specials] + [k for k, _ in growths] + [k for k, _ in climbs[:5]]

    if len(plan) < max_chart:
        # Remaining climbers + everything else, by existing logic (heat -> source
        # -> rank) with an aging boost so the uncharted tail keeps getting
        # sampled instead of starving forever (coverage converges over runs).
        tail = climbs[5:] + rest
        tail.sort(key=lambda kv: (-(_heat(kv[1]) + _age_hours(kv[0], games, now) * 2.0),
                                  _priority(kv[1]), -_rank_pos(kv[1])))
        plan += [k for k, _ in tail]
    return plan[:max_chart]


def discover(session, sources: list[str]) -> list[dict]:
    cands: list[dict] = []
    if "steam" in sources:
        cands += steam_new(session)
    if "appstore" in sources:
        cands += appstore_new(session)
    if "trending" in sources:
        tp = config.WEB_DATA_DIR / "trends.json"
        if tp.exists():
            try:
                cands += from_trending_games(json.loads(tp.read_text(encoding="utf-8")))
            except (ValueError, OSError) as e:  # noqa: BLE001
                log.warning("cannot read trends.json for game candidates: %s", e)
    return cands


def _heat_value(rec: dict) -> int:
    """0-100 heat for the history line: our normalized score when charted, else
    the position on the game's ranked list (0..100, 100 = top)."""
    series = rec.get("series") or []
    score = rec.get("score") or 0
    if series and score:
        return int(score)
    rank, size = rec.get("rank"), rec.get("size")
    if rank and size:
        return round((1.0 - (rank - 1) / size) * 100)
    return int(rec.get("heat") or 0)


def _record_history(games: dict, now: str, days: float = 7.0, max_points: int = 500) -> int:
    """Append this run's heat to every game's ``history`` and prune to `days`.

    Gives each game a self-recorded curve (Trends itself is rate-limited and its
    absolute level is re-normalised per request, so we track a comparable 0-100
    heat instead).
    """
    try:
        cutoff = datetime.fromisoformat(now).timestamp() - days * 86400
    except (ValueError, TypeError):
        cutoff = 0.0
    for rec in games.values():
        rec["heat"] = _heat_value(rec)
        hist = rec.setdefault("history", [])
        hist.append([now, rec["heat"]])
        kept = []
        for p in hist:
            try:
                if datetime.fromisoformat(p[0]).timestamp() >= cutoff:
                    kept.append(p)
            except (ValueError, TypeError, IndexError):
                kept.append(p)
        rec["history"] = kept[-max_points:]
    return len(games)


def run(
    *,
    proxy: str | None,
    sources: list[str],
    max_chart: int,
    throttle: float,
    out_dir: Path,
    state_path: Path,
    max_refresh: int = config.GAMES_MAX_REFRESH_PER_RUN,
    refresh_hours: float = config.GT_REFRESH_HOURS,
    min_growth: float = config.GAMES_CHART_MIN_GROWTH,
    min_pos: float = config.GAMES_CHART_MIN_POS,
    max_hot: int = config.GAMES_MAX_HOT_PER_RUN,
    hot_growth: float = config.GAMES_HOT_GROWTH,
    hot_rank_change: float = config.GAMES_HOT_RANK_CHANGE,
    retention_days: int = 90,
    history_days: float = 7.0,
) -> int:
    session = build_session(proxy)
    candidates = discover(session, sources)

    merged: dict[str, dict] = {}
    for c in candidates:
        key = _norm(c["name"])
        if not key:
            continue
        m = merged.setdefault(
            key,
            {"name": c["name"], "sources": [], "url": None, "growth": 0, "zh": ""},
        )
        if c["source"] not in m["sources"]:
            m["sources"].append(c["source"])
        if c.get("url") and not m["url"]:
            m["url"] = c["url"]
        if c.get("growth") is not None:
            m["hasGrowth"] = True
        if (c.get("growth") or 0) > (m["growth"] or 0):
            m["growth"] = c["growth"]
        if c.get("zh") and not m["zh"]:
            m["zh"] = c["zh"]
        # Keep the best (highest = smallest normalised position) ranked slot so a
        # game listed by several charts is judged by where it ranks highest.
        if c.get("rank") and c.get("size"):
            cur = m.get("rank"), m.get("size")
            if not cur[0] or (c["rank"] / c["size"]) < (cur[0] / cur[1]):
                m["rank"], m["size"] = c["rank"], c["size"]
    log.info("merged %d unique candidates", len(merged))

    state = load_games_state(state_path)
    games = state["games"]
    gt = GoogleTrendsInterest(session, throttle)
    now = now_iso()

    # Fold last run's chart position into this run's heat: a game climbing a
    # ranked list (Steam new/coming, App Store) gets a pre-curve growth signal.
    for key, m in merged.items():
        rec = games.get(key)
        if rec:
            m["rankChange"] = rank_change(rec.get("rank"), rec.get("size"), m.get("rank"), m.get("size"))

    charted = 0
    refreshed = 0
    hot_updated = 0
    new_count = 0
    fails = 0
    blocked = False

    # State upkeep for every candidate seen THIS run (priority order).
    ordered = sorted(merged.items(), key=_base_key)
    for key, m in ordered:
        rec = games.get(key)
        if rec is None:
            new_count += 1
            rec = {
                "name": m["name"],
                "first": now,
                "last": now,
                "sightings": 1,
                "sources": list(m["sources"]),
                "url": m["url"],
                "series": [],
                "chart_at": None,
                "score": 0,
                "growth": m.get("growth") or 0,
                "zh": m.get("zh", ""),
                "rank": m.get("rank"),
                "size": m.get("size"),
                "rankChange": m.get("rankChange", 0.0),
            }
            games[key] = rec
        else:
            rec["last"] = now
            rec["sightings"] = rec.get("sightings", 0) + 1
            for s in m["sources"]:
                if s not in rec.setdefault("sources", []):
                    rec["sources"].append(s)
            if m["url"] and not rec.get("url"):
                rec["url"] = m["url"]
            if (m.get("growth") or 0) > (rec.get("growth") or 0):
                rec["growth"] = m["growth"]
            if m.get("zh") and not rec.get("zh"):
                rec["zh"] = m["zh"]
            if m.get("rank"):
                rec["rank"] = m["rank"]
                rec["size"] = m["size"]
            rec["rankChange"] = m.get("rankChange", 0.0)

    # Hot pass (first, every run): re-pull the curve for the strongest movers so
    # a clear Google spike (>= hot_growth) or a sharp chart climb
    # (rankChange >= hot_rank_change) is always up to date, ignoring the refresh
    # cooldown. Then the normal chart/refresh passes run on the rest.
    if max_hot > 0 and not blocked:
        hot = sorted(
            ((k, m) for k, m in merged.items() if _is_hot(m, hot_growth, hot_rank_change)),
            key=_base_key,
        )
        for key, m in hot:
            if blocked or hot_updated >= max_hot:
                break
            rec = games.get(key)
            if rec is None:
                continue
            try:
                series = gt.interest(rec["name"], config.GT_INTEREST_GEO, config.GT_INTEREST_WINDOW)
            except BlockedError:
                blocked = True
                series = None
                log.error("games: Google blocked us (/sorry/); skipping the rest of this run")
            hot_updated += 1
            if series:
                rec["series"] = downsample(series, config.GAMES_SERIES_POINTS)
                rec["chart_at"] = now
                rec["score"] = score_series(series)
                rec["rise"] = rise_ratio(series)
                fails = 0
                log.info("hot-refreshed %r growth=%s rankChange=%.2f score=%d rise=%.2f",
                         rec["name"], m.get("growth"), m.get("rankChange") or 0.0, rec["score"], rec["rise"])
            elif not blocked:
                fails += 1
                if fails >= 3:
                    blocked = True
                    log.error("games: Google looks rate-limited (%d straight misses); skipping the rest of this run", fails)
            save_games_state(state_path, state)  # persist progress

    # Who gets a first curve next: freshly discovered candidates + the pending
    # backlog in state (so a game that dropped off its source list still gets
    # one). Computed after the hot pass so already-updated hot games are excluded.
    pending = {
        k: _rec_candidate(r)
        for k, r in games.items()
        if r.get("chart_at") is None and not is_junk(r["name"])
    }
    pool = {**pending, **merged}  # fresh signal wins on overlap
    chart_plan = _chart_plan(pool, games, max_chart, now, min_growth, min_pos)

    # Chart pass: fetch a curve for the chosen keys (fresh or backlog), in order.
    for key in chart_plan:
        if blocked or charted >= max_chart:
            break
        rec = games.get(key)
        if rec is None or rec.get("chart_at") is not None:
            continue
        name = rec["name"]
        try:
            series = gt.interest(name, config.GT_INTEREST_GEO, config.GT_INTEREST_WINDOW)
        except BlockedError:
            blocked = True
            series = None
            log.error("games: Google blocked us (/sorry/); skipping the rest of this run")
        charted += 1
        if series:
            rec["series"] = downsample(series, config.GAMES_SERIES_POINTS)
            rec["chart_at"] = now
            rec["score"] = score_series(series)
            rec["rise"] = rise_ratio(series)
            fails = 0
            log.info("charted %r peak=%d score=%d rise=%.2f", name, max(series), rec["score"], rec["rise"])
        elif not blocked:
            log.info("no trend data for %r", name)
            fails += 1
            if fails >= 3:
                blocked = True
                log.error("games: Google looks rate-limited (%d straight misses); skipping the rest of this run", fails)
        save_games_state(state_path, state)  # persist charting progress

    for k in [k for k, r in games.items() if is_junk(r["name"])]:
        del games[k]

    # Refresh pass: re-sample already-charted games so a game that was quiet and
    # suddenly spiked is detected (rise) instead of only ever being charted once.
    if max_refresh > 0 and not blocked:
        due = [
            (k, r)
            for k, r in games.items()
            if r.get("chart_at") and _is_due(r.get("chart_at"), now, refresh_hours)
        ]
        # Refresh the biggest risers first, then the longest-unrefreshed.
        due.sort(key=lambda kr: (-_heat(kr[1]), kr[1].get("chart_at") or ""))
        for _, rec in due:
            if refreshed >= max_refresh or blocked:
                break
            try:
                series = gt.interest(rec["name"], config.GT_INTEREST_GEO, config.GT_INTEREST_WINDOW)
            except BlockedError:
                blocked = True
                log.error("games: Google blocked us (/sorry/); skipping the rest of the refresh pass")
                break
            refreshed += 1
            if series:
                rec["series"] = downsample(series, config.GAMES_SERIES_POINTS)
                rec["chart_at"] = now
                rec["score"] = score_series(series)
                rec["rise"] = rise_ratio(series)
                fails = 0
                log.info("refreshed %r rise=%.2f score=%d", rec["name"], rec["rise"], rec["score"])
                save_games_state(state_path, state)
            else:
                fails += 1
                if fails >= 3:
                    blocked = True
                    log.error("games: Google looks rate-limited; skipping the rest of the refresh pass")

    _record_history(games, now, history_days)
    prune_state(state, retention_days)
    save_games_state(state_path, state)
    payload = export_games(
        state, now,
        min_peak=config.GAMES_MIN_PEAK,
        min_signal=config.GAMES_MIN_SIGNAL,
        rise_breakout=config.GAMES_RISE_BREAKOUT,
    )
    save_json(out_dir / "games.json", payload)
    log.info(
        "games: %d candidates, %d new, %d charted, %d refreshed, %d exported",
        len(merged), new_count, charted, refreshed, len(payload["items"]),
    )
    return len(payload["items"])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="New-game radar collector.")
    p.add_argument("--proxy", default=None)
    p.add_argument("--sources", default=",".join(DEFAULT_SOURCES), help="steam,appstore,trending")
    p.add_argument("--max-chart", type=int, default=config.GAMES_MAX_CHART_PER_RUN)
    p.add_argument("--max-refresh", type=int, default=config.GAMES_MAX_REFRESH_PER_RUN)
    p.add_argument("--refresh-hours", type=float, default=config.GT_REFRESH_HOURS)
    p.add_argument("--min-growth", type=float, default=config.GAMES_CHART_MIN_GROWTH)
    p.add_argument("--min-pos", type=float, default=config.GAMES_CHART_MIN_POS)
    p.add_argument("--max-hot", type=int, default=config.GAMES_MAX_HOT_PER_RUN)
    p.add_argument("--hot-growth", type=float, default=config.GAMES_HOT_GROWTH)
    p.add_argument("--hot-rank-change", type=float, default=config.GAMES_HOT_RANK_CHANGE)
    p.add_argument("--throttle", type=float, default=config.GT_INTEREST_THROTTLE)
    p.add_argument("--out", default=str(config.WEB_DATA_DIR))
    p.add_argument("--state", default=str(config.GAMES_STATE_PATH))
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from .. import settings

    settings.apply_environment()
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    run(
        proxy=args.proxy,
        sources=sources,
        max_chart=args.max_chart,
        throttle=args.throttle,
        out_dir=Path(args.out),
        state_path=Path(args.state),
        max_refresh=args.max_refresh,
        refresh_hours=args.refresh_hours,
        min_growth=args.min_growth,
        min_pos=args.min_pos,
        max_hot=args.max_hot,
        hot_growth=args.hot_growth,
        hot_rank_change=args.hot_rank_change,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
