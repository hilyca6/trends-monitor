"""Fast-heat layer: baseline snapshots of autocomplete + SERP results.

Cheap, quota-free signals that respond before Trends shows anything: when a
query that was quiet starts *gaining* new autocomplete long-tails or new SERP
pages, it is heating up. The first time a query is seen we only store a
baseline; from the next run on we diff against it and report the deltas.

State (``state/fast_state.json``)::

    {"updated": iso, "entries": {q: {"suggest": [...], "serp": [...], "ts": iso}}}

Ported from the game-radar tools' fast-signals layer (baseline + new counts),
kept deliberately simple for a personal Python tool.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("trends.fastheat")

DEFAULT_CAP = 50
PASS_SCORE = 40
WATCH_SCORE = 12


def _load(path) -> dict:
    if path and Path(path).exists():
        try:
            d = json.loads(Path(path).read_text(encoding="utf-8"))
            if isinstance(d, dict):
                d.setdefault("entries", {})
                return d
        except (ValueError, OSError):
            pass
    return {"entries": {}}


def load(path) -> dict:
    return _load(path)


def _prune(state: dict, days: int) -> dict:
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    out: dict = {}
    for q, rec in (state.get("entries") or {}).items():
        if not isinstance(rec, dict):
            continue
        ts = rec.get("ts")
        try:
            fresh = (not ts) or datetime.fromisoformat(ts).timestamp() >= cutoff
        except (ValueError, TypeError):
            fresh = True
        if fresh:
            out[q] = rec
    state["entries"] = out
    return state


def save(path, state: dict, days: int = 90) -> None:
    if not path:
        return
    _prune(state, days)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def compute(state: dict, q: str, cur_suggest, cur_serp, *, suggestion_cap: int = DEFAULT_CAP) -> dict | None:
    """Return the heat dict for ``q``, or None if this is its first (baseline) run."""
    rec = (state.get("entries") or {}).get(q)
    if rec is None:
        return None
    prev_s = set(rec.get("suggest") or [])
    prev_p = set(rec.get("serp") or [])
    # Truncate the current values with the SAME cap used by update(), otherwise
    # any query with more than `cap` entries would look "new" every run.
    cur_s = [s for s in dict.fromkeys(cur_suggest or []) if s][:suggestion_cap]
    cur_p = [s for s in dict.fromkeys(cur_serp or []) if s][:suggestion_cap]
    new_s = sorted(set(cur_s) - prev_s)
    new_p = sorted(set(cur_p) - prev_p)
    n_s, n_p = len(new_s), len(new_p)
    score = min(100, n_s * 8 + n_p * 4)
    if score >= PASS_SCORE:
        classification = "pass"
    elif score >= WATCH_SCORE:
        classification = "watch"
    else:
        classification = "weak"
    reasons = []
    if n_s:
        reasons.append(f"自动补全新增 {n_s} 条长尾")
    if n_p:
        reasons.append(f"SERP 新增 {n_p} 个页面")
    if not reasons:
        reasons.append("搜索生态暂无明显新增")
    return {
        "score": score,
        "classification": classification,
        "newSuggestionCount": n_s,
        "newSerpPageCount": n_p,
        "newSuggestions": new_s[:5],
        "newSerpPages": new_p[:5],
        "reasons": reasons,
    }


def update(state: dict, q: str, cur_suggest, cur_serp, now: str, *, cap: int = DEFAULT_CAP) -> None:
    """Store the latest snapshot for ``q`` (bounded, de-duped)."""
    state.setdefault("entries", {})[q] = {
        "suggest": list(dict.fromkeys([s for s in (cur_suggest or []) if s]))[:cap],
        "serp": list(dict.fromkeys([s for s in (cur_serp or []) if s]))[:cap],
        "ts": now,
    }


def heat(state: dict, q: str, cur_suggest, cur_serp, now: str) -> dict | None:
    """Diff against the baseline (if any), then advance the snapshot; return heat."""
    result = compute(state, q, cur_suggest, cur_serp)
    update(state, q, cur_suggest, cur_serp, now)
    return result
