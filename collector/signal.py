"""Baseline-relative Google Trends signal (the "GPTs multiple" + game-radar model).

Google normalises every Trends timeseries to 0-100, so a keyword's absolute
value means nothing across requests. Comparing a term against a reference word
(anchor/baseline) inside the *same* explore request yields a stable relative
multiple — the basis of "hockey-stick" detection.

For each candidate this module fetches ``[term, <support queries>, anchor]`` in
one request over several windows (7d/30d/90d) and computes:

  * per-window stats (momentum, coverage, slope, peak, recent/earlier coverage)
  * a relative ratio vs the anchor
  * a *qualified* ratio (max of the support queries, e.g. "{q} guide") that
    confirms the demand is really about this term, not another entity
  * classification (breakout/rising/strong/moderate/weak/none), with isolated
    single-day spikes demoted to weak
  * freshness (new/existing/unknown) and entityConflict
  * a 0-100 score with caps

Results are cached per ``(geo|channel|term)`` in ``state/signal_cache.json``.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .gtrends_api import BlockedError, GoogleTrendsClient

log = logging.getLogger("trends.signal")

DEFAULT_BASELINE = "gpts"
# Ordered short -> long; the key names are used in the exported signal object.
DEFAULT_WINDOWS = {"7d": "now 7-d", "30d": "today 1-m", "90d": "today 3-m"}

# Per-channel query profile (mirrors the game-radar tools): an anchor keyword
# plus one or more "qualified" support queries that confirm topical intent.
DEFAULT_ANCHORS = {"generic": "gpts", "wiki": "steam", "online": "crazy games"}
CHANNEL_SUPPORT = {
    "generic": ["{q} guide"],
    "wiki": ["{q} wiki", "{q} guide"],
    "online": ["{q} online", "play {q}"],
}
_ENTITY_CONFLICT_RATIO = 0.25

CLASSIFICATION_RANK = {
    "breakout": 6,
    "rising": 5,
    "strong": 4,
    "moderate": 3,
    "weak": 2,
    "none": 1,
}
CLASSIFICATION_LABEL = {
    "breakout": "爆发",
    "rising": "明显上涨",
    "strong": "需求较强",
    "moderate": "需求中等",
    "weak": "需求较弱",
    "none": "无需求",
}

_ONLINE_HINT = re.compile(r"\.(?:app|io|game|games|gg|play|xyz)$", re.I)
_GAME_HINT = re.compile(r"\b(?:games?|roblox|minecraft|fortnite|wordle|sprunki)\b", re.I)


def guess_channel(q: str) -> str:
    """Rough site-type guess used to pick a query profile/anchor."""
    ql = (q or "").strip().lower()
    if _ONLINE_HINT.search(ql):
        return "online"
    if _GAME_HINT.search(ql):
        return "wiki"
    return "generic"


def build_profile(q: str, channel: str = "generic", anchor: str | None = None, anchors: dict | None = None) -> dict:
    """Build the query list [term, *support, anchor] for one channel."""
    channel = channel if channel in CHANNEL_SUPPORT else "generic"
    anchors = anchors or {}
    a = anchor or anchors.get(channel) or DEFAULT_ANCHORS.get(channel, DEFAULT_BASELINE)
    support = [tpl.format(q=q) for tpl in CHANNEL_SUPPORT[channel]]
    return {"q": q, "channel": channel, "anchor": a, "support": support, "queries": [q, *support, a]}


# ---------------------------------------------------------------------------
# Series statistics
# ---------------------------------------------------------------------------
def _slope(series: list[int]) -> float:
    """Least-squares slope of value vs index (per point)."""
    n = len(series)
    if n < 2:
        return 0.0
    mean_x = (n - 1) / 2
    mean_y = sum(series) / n
    num = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(series))
    den = sum((i - mean_x) ** 2 for i in range(n))
    return (num / den) if den else 0.0


def series_stats(series) -> dict:
    """Summarise a 0-100 series: level, momentum, coverage, slope, peak."""
    s = [int(x) for x in (series or [])]
    n = len(s)
    if n == 0:
        return {
            "points": 0, "average": 0.0, "recentAverage": 0.0, "earlierAverage": 0.0,
            "coverage": 0.0, "earlierCoverage": 0.0, "recentCoverage": 0.0,
            "momentum": 0.0, "slope": 0.0, "peak": 0, "nonzero": 0,
        }
    nonzero = sum(1 for v in s if v > 0)
    third = max(1, n // 3)
    recent = s[-third:]
    earlier = s[:third]
    ra = sum(recent) / len(recent)
    ea = sum(earlier) / len(earlier)
    # momentum = recent/earlier; a flat-zero earlier but active recent is a spike.
    momentum = ra / ea if ea > 0 else (float(n) if ra > 0 else 0.0)
    return {
        "points": n,
        "average": round(sum(s) / n, 2),
        "recentAverage": round(ra, 2),
        "earlierAverage": round(ea, 2),
        "coverage": round(nonzero / n, 3),
        "earlierCoverage": round(sum(1 for v in earlier if v > 0) / len(earlier), 3),
        "recentCoverage": round(sum(1 for v in recent if v > 0) / len(recent), 3),
        "momentum": round(momentum, 3),
        "slope": round(_slope(s), 4),
        "peak": max(s),
        "nonzero": nonzero,
    }


def _max_series(*series) -> list[int]:
    """Element-wise max of several series (the "qualified" combined signal)."""
    length = max((len(s) for s in series), default=0)
    out: list[int] = []
    for i in range(length):
        vals = [int(s[i]) for s in series if i < len(s)]
        out.append(max(vals) if vals else 0)
    return out


def ratio(term_series, base_series) -> float:
    """Recent level of ``term`` relative to ``anchor`` (same request)."""
    t = series_stats(term_series)
    b = series_stats(base_series)
    if b["recentAverage"] > 0:
        return round(t["recentAverage"] / b["recentAverage"], 3)
    if b["peak"] > 0:
        return round(t["peak"] / b["peak"], 3)
    return 0.0


def isolated_spike(stats: dict) -> bool:
    """True when a series is a single-day spike rather than sustained demand."""
    if stats.get("points", 0) <= 0:
        return False
    if stats.get("nonzero", 0) <= 1:
        return True
    return stats.get("recentCoverage", 0.0) < 0.34 and stats.get("peak", 0) > stats.get("recentAverage", 0) * 3


def classify(ratio_val: float, stats: dict, *, breakout_ratio: float = 5.0, rising_ratio: float = 1.0, spike: bool = False) -> str:
    """Turn a ratio + series stats (+ isolated spike) into a coarse demand label."""
    if stats.get("peak", 0) <= 0:
        return "none"
    if spike:
        return "weak"
    if ratio_val >= breakout_ratio and stats.get("momentum", 0) >= 1.0:
        return "breakout"
    if ratio_val >= rising_ratio and stats.get("momentum", 0) >= 1.15:
        return "rising"
    if ratio_val >= 0.5:
        return "strong"
    if ratio_val >= 0.2:
        return "moderate"
    return "weak"


def freshness(long_stats: dict) -> str:
    """new = silent early in the long window then grew; existing = always had volume."""
    if not long_stats or long_stats.get("points", 0) < 6:
        return "unknown"
    if long_stats.get("earlierAverage", 0) > 1.0 or long_stats.get("earlierCoverage", 0.0) >= 0.45:
        return "existing"
    if long_stats.get("recentAverage", 0) >= 3.0:
        return "new"
    return "unknown"


def entity_conflict(freshness_val: str, ratio_val: float, qualified_ratio: float) -> bool:
    """Existing term whose qualified demand is far below the headline demand:
    the main-word volume likely comes from a different entity."""
    return freshness_val == "existing" and qualified_ratio < max(0.01, ratio_val * _ENTITY_CONFLICT_RATIO)


def _score(seven: dict, thirty: dict, head: dict, fresh: str, conflicted: bool) -> int:
    """0-100 demand score (ported from the game-radar trend-verifier)."""
    s = 0
    s += min(20, round(thirty.get("ratio", 0.0) * 100))
    s += min(16, round(seven.get("ratio", 0.0) * 80))
    s += min(14, round(thirty.get("qualifiedRatio", 0.0) * 160))
    s += round(thirty.get("coverage", 0.0) * 8) + round(seven.get("coverage", 0.0) * 6)
    if head.get("classification") == "breakout":
        s += 20
    elif head.get("classification") == "rising":
        s += 18
    if fresh == "new":
        s += 8
    elif fresh == "existing":
        s -= 8
    if conflicted:
        s -= 12
    if seven.get("spike") and thirty.get("spike"):
        s -= 12
    return max(0, min(100, s))


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
def _load_cache(path) -> dict:
    if path and Path(path).exists():
        try:
            d = json.loads(Path(path).read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (ValueError, OSError):
            return {}
    return {}


def _prune_cache(cache: dict, days: int) -> dict:
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    out: dict = {}
    for k, rec in cache.items():
        if not isinstance(rec, dict):
            continue
        ts = rec.get("ts")
        if not ts:
            out[k] = rec
            continue
        try:
            fresh = datetime.fromisoformat(ts).timestamp() >= cutoff
        except (ValueError, TypeError):
            fresh = True
        if fresh:
            out[k] = rec
    return out


def _save_cache(path, cache: dict, days: int = 90) -> None:
    if not path:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(_prune_cache(cache, days), ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Fetch + analyse
# ---------------------------------------------------------------------------
def _fetch_term(
    client: GoogleTrendsClient,
    profile: dict,
    geo: str,
    windows: dict[str, str],
    *,
    breakout_ratio: float,
    rising_ratio: float,
) -> dict | None:
    """Fetch [term, *support, anchor] per window; return the signal record or None."""
    q = profile["q"]
    anchor = profile["anchor"]
    support = profile["support"]

    per_window: dict[str, dict] = {}
    best_key: str | None = None
    best_rank = -1
    for key, window in windows.items():
        data = client.interest_multi(profile["queries"], geo=geo, window=window)
        if not data:
            continue
        main = data.get(q, [])
        anchor_series = data.get(anchor, [])
        qual = _max_series(*[data.get(s, []) for s in support]) if support else []
        stats = series_stats(main)
        spike = isolated_spike(stats)
        r = ratio(main, anchor_series)
        qr = ratio(qual, anchor_series) if qual else 0.0
        cls = classify(r, stats, breakout_ratio=breakout_ratio, rising_ratio=rising_ratio, spike=spike)
        per_window[key] = {
            "window": window, "ratio": r, "qualifiedRatio": qr,
            "classification": cls, "spike": spike, **stats,
        }
        rank = CLASSIFICATION_RANK.get(cls, 1)
        if rank > best_rank:
            best_rank = rank
            best_key = key
    if not per_window:
        return None

    # Headline = the window with the strongest classification.
    head_key = best_key or next(iter(per_window))
    head = per_window[head_key]
    long_stats = per_window.get("90d") or per_window.get("30d") or head
    fresh = freshness(long_stats)
    ref = per_window.get("30d") or per_window.get("90d") or head
    conflicted = entity_conflict(fresh, ref["ratio"], ref.get("qualifiedRatio", 0.0))
    seven = per_window.get("7d", head)
    thirty = per_window.get("30d", head)
    return {
        "channel": profile["channel"],
        "anchor": anchor,
        "ratio": head["ratio"],
        "qualifiedRatio": head["qualifiedRatio"],
        "classification": head["classification"],
        "label": CLASSIFICATION_LABEL.get(head["classification"], ""),
        "freshness": fresh,
        "entityConflict": conflicted,
        "score": _score(seven, thirty, head, fresh, conflicted),
        "window": head.get("window"),
        "windows": per_window,
    }


def analyze(
    client: GoogleTrendsClient,
    terms: list,
    *,
    geo: str = "US",
    baseline: str = DEFAULT_BASELINE,
    anchors: dict | None = None,
    channel: str = "generic",
    windows: dict[str, str] | None = None,
    throttle: float = 4.0,
    breakout_ratio: float = 5.0,
    rising_ratio: float = 1.0,
    cache_path=None,
    budget: int | None = None,
    cache_days: int = 90,
) -> dict[str, dict]:
    """Return ``{term: signal}`` for each term, using the cache when possible.

    ``terms`` items may be plain strings (uses ``channel``) or dicts with
    ``q``/``channel``/``anchor``. ``budget`` caps how many *uncached* terms are
    fetched this run; a ``BlockedError`` stops the run immediately.
    """
    windows = windows or dict(DEFAULT_WINDOWS)
    merged_anchors = dict(DEFAULT_ANCHORS)
    if baseline:
        merged_anchors["generic"] = baseline
    if anchors:
        merged_anchors.update({k: v for k, v in anchors.items() if v})

    cache = _load_cache(cache_path)
    now = datetime.now(timezone.utc).isoformat()
    out: dict[str, dict] = {}
    fetched = 0
    for item in terms:
        if isinstance(item, str):
            q, ch, anc = item.strip(), channel, None
        else:
            q = (item.get("q") or "").strip()
            ch = item.get("channel", channel)
            anc = item.get("anchor")
        if not q:
            continue
        profile = build_profile(q, ch, anc, merged_anchors)
        key = f"{geo}|{profile['channel']}|{q}"
        rec = cache.get(key)
        if rec is None:
            if budget is not None and fetched >= budget:
                continue
            if throttle:
                time.sleep(throttle)
            try:
                rec = _fetch_term(
                    client, profile, geo, windows,
                    breakout_ratio=breakout_ratio, rising_ratio=rising_ratio,
                )
            except BlockedError:
                log.warning("signal: Google blocked us (/sorry/); stopping")
                break
            if rec is None:
                continue
            rec["ts"] = now
            rec["geo"] = geo
            cache[key] = rec
            fetched += 1
        out[q] = {k: v for k, v in rec.items() if k not in ("ts", "geo")}
    if fetched and cache_path:
        _save_cache(cache_path, cache, cache_days)
    if fetched:
        log.info("signal: fetched %d new term(s), cache %d, %d returned", fetched, len(cache), len(out))
    return out
