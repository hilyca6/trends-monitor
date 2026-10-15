"""Games state + games.json export.

games.json contract (mirrors findnews.me):
  {updated, items:[{name, series[], chart_at, first, last, score, sightings,
                    source, url, growth, rise, rank, size, rankChange}]}
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from ..store import prune_records, save_json

log = logging.getLogger("trends.games.store")

# Domain-style names that signal a (usually viral) browser/web game.
WEB_GAME_SUFFIXES = (".app", ".io", ".game", ".games", ".gg", ".play", ".xyz")

# "Content queries" about games that are not game names.
_JUNK_PHRASES = (
    "server status", "server down", "game pass", "release date", "tier list",
    "patch notes", "system requirements", "spelling bee", "how to",
)
_JUNK_WORDS = frozenset(
    {"hints", "hint", "strands", "wordle", "quordle", "codes", "cheats", "wiki", "redeem", "leaks"}
)
_WORD_RE = re.compile(r"[a-z0-9]+")


def is_web_game(name: str) -> bool:
    return name.strip().lower().endswith(WEB_GAME_SUFFIXES)


def is_junk(name: str) -> bool:
    """True for game-related \"content queries\" that are not game names."""
    ql = name.lower()
    if any(p in ql for p in _JUNK_PHRASES):
        return True
    return bool(set(_WORD_RE.findall(ql)) & _JUNK_WORDS)


def game_group(rec: dict) -> str:
    """``trending`` (a real search-heat source) vs ``listed`` (chart-only source).

    ``trending`` = came from Google Trends / a web-game term (has search heat);
    ``listed`` = only appeared on Steam / App Store ranked lists (rank-only).
    """
    if is_web_game(rec.get("name", "")):
        return "trending"
    return "trending" if any(str(s).startswith("trending") for s in (rec.get("sources") or [])) else "listed"


def growth_window(rec: dict, default: float = 0.0) -> float:
    """Growth ratio across the retained history window (latest vs earliest point)."""
    hist = rec.get("history") or []
    if len(hist) < 2 or not hist[0][1]:
        return default
    return round((hist[-1][1] - hist[0][1]) / hist[0][1], 4)


def growth_1d(rec: dict, default: float = 0.0) -> float:
    """Growth ratio over the last ~24h of the retained history (latest vs the
    most recent point at least a day old; falls back to the earliest point)."""
    hist = rec.get("history") or []
    if len(hist) < 2:
        return default
    try:
        t_last = datetime.fromisoformat(hist[-1][0])
    except (ValueError, TypeError):
        return default
    base = hist[0][1]
    for p in reversed(hist):
        try:
            t = datetime.fromisoformat(p[0])
        except (ValueError, TypeError):
            continue
        if (t_last - t).total_seconds() >= 86400:
            base = p[1]
            break
    if not base:
        return default
    return round((hist[-1][1] - base) / base, 4)


def _rank(r: dict) -> tuple:
    """Sort key: sudden/early signals first, then growth, score, peak, sightings."""
    new = r.get("new")
    breakout = r.get("breakout")
    web = r.get("web_game")
    if (new and breakout) or (web and (new or breakout)):
        tier = 0
    elif breakout or new:
        tier = 1
    elif web:
        tier = 2
    else:
        tier = 3
    peak = max(r["series"]) if r.get("series") else 0
    return (
        tier,
        -(r.get("growth") or 0),
        -(r.get("rankChange") or 0),
        -(r.get("rise") or 0),
        -(r.get("score") or 0),
        -peak,
        -(r.get("sightings") or 0),
    )


def prune_state(state: dict, days: int = 90) -> int:
    """Drop games not sighted within `days` so the games state stays bounded."""
    n = prune_records(state.get("games", {}), days)
    if n:
        log.info("pruned %d stale games", n)
    return n


def load_games_state(path: Path) -> dict:
    if path.exists():
        try:
            import json

            data = json.loads(path.read_text(encoding="utf-8"))
            data.setdefault("games", {})
            return data
        except (ValueError, OSError) as e:  # noqa: BLE001
            log.warning("cannot read %s: %s", path, e)
    return {"games": {}}


def save_games_state(path: Path, state: dict) -> None:
    save_json(path, state)


def export_games(
    state: dict,
    now: str,
    min_peak: int = 0,
    min_signal: float = 0.2,
    max_pending: int = 80,
    rise_breakout: float = 1.0,
) -> dict:
    ready: list[dict] = []
    pending: list[dict] = []
    for rec in state.get("games", {}).values():
        if is_junk(rec["name"]):
            continue
        series = rec.get("series") or []
        peak = max(series) if series else 0
        signal = (sum(1 for v in series if v > 0) / len(series)) if series else 0.0
        growth = rec.get("growth") or 0
        rise = rec.get("rise") or 0
        item = {
            "name": rec["name"],
            "series": series,
            "chart_at": rec.get("chart_at"),
            "first": rec.get("first"),
            "last": rec.get("last"),
            "score": rec.get("score", 0),
            "growth": growth,
            "rise": rise,
            "rank": rec.get("rank"),
            "size": rec.get("size"),
            "rankChange": rec.get("rankChange", 0.0),
            "sightings": rec.get("sightings", 1),
            "source": (rec.get("sources") or [None])[0],
            "url": rec.get("url"),
            "zh": rec.get("zh", ""),
            "new": rec.get("first") == now,
            "web_game": is_web_game(rec["name"]),
            "group": game_group(rec),
            "heat": rec.get("heat", 0),
            "history": rec.get("history") or [],
            "growth7d": growth_window(rec),
            "growth1d": growth_1d(rec),
        }
        item["breakout"] = bool(growth >= 500 or rise >= rise_breakout)
        if rec.get("chart_at") is None:
            pending.append(item)
        elif peak >= min_peak and signal >= min_signal:
            ready.append(item)

    # Ready (real charted signals) + highlight pending (new/breakout/web), then
    # a capped backlog of the remaining pending candidates.
    hl_pending = [r for r in pending if _rank(r)[0] <= 1]
    rest_pending = [r for r in pending if _rank(r)[0] > 1]
    combined = ready + hl_pending
    combined.sort(key=_rank)
    rest_pending.sort(key=_rank)
    items = combined + rest_pending[:max_pending]
    return {"updated": now, "items": items}


def downsample(series: list[int], k: int) -> list[int]:
    n = len(series)
    if n <= k or k <= 0:
        return list(series)
    step = n / k
    out: list[int] = []
    for i in range(k):
        a = int(i * step)
        b = int((i + 1) * step)
        chunk = series[a:b] or [series[min(a, n - 1)]]
        out.append(round(sum(chunk) / len(chunk)))
    return out


def rise_ratio(series: list[int]) -> float:
    """Recent-vs-prior growth (higher = more sudden).

    Compares the latest third of the curve to the preceding third. A flat or
    already-high curve scores ~0; a curve that was quiet and just spiked scores
    high — exactly the "no traffic before, suddenly booming today" case.
    """
    n = len(series)
    if n < 6:
        return 0.0
    k = n // 3
    recent = series[-k:]
    prior = series[-2 * k : -k]
    ra = sum(recent) / len(recent)
    pa = sum(prior) / len(prior)
    return round((ra - pa) / (pa + 1.0), 3)


def score_series(series: list[int]) -> int:
    """0-100 score: recent level + momentum (recent half vs prior half)."""
    if not series:
        return 0
    n = len(series)
    half = n // 2
    recent = sum(series[half:]) / max(1, n - half)
    prior = sum(series[:half]) / max(1, half)
    momentum = (recent - prior) / (prior + 1)
    score = recent * 0.6 + max(0.0, momentum) * 100 * 0.4
    return max(0, min(100, round(score)))
