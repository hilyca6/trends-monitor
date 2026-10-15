"""State + output writer.

Mirrors findnews.me data contract:
  web/data/trends.json   {updated, geos[], cats{id:name}, items{geo:[...]}}
  web/data/history.json  {updated, chunks, items[...]}
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("trends.store")

# Optional classification fields enriched by the LLM (P5).
_META_FIELDS = ("cat", "subcat", "entity", "trigger")


def _meta(it: dict) -> dict:
    return {f: it[f] for f in _META_FIELDS if it.get(f)}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:  # noqa: BLE001
            log.warning("cannot read %s: %s", path, e)
    return default


def load_state(path: Path) -> dict:
    state = _load_json(path, {"seen": {}})
    state.setdefault("seen", {})
    return state


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def prune_records(records: dict, days: int = 90) -> int:
    """Drop records not sighted within `days` (keeps state.json from growing forever).

    Records with a missing/unparseable `last` are kept (we never delete what we
    cannot date). Returns the number of records removed.
    """
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    drop: list[str] = []
    for key, rec in records.items():
        try:
            last_ts = datetime.fromisoformat(rec.get("last", "")).timestamp()
        except (ValueError, TypeError):
            continue
        if last_ts < cutoff:
            drop.append(key)
    for key in drop:
        del records[key]
    return len(drop)


def prune_state(state: dict, days: int = 90) -> int:
    n = prune_records(state.get("seen", {}), days)
    if n:
        log.info("pruned %d stale state entries", n)
    return n


def update_state(state: dict, items_by_geo: dict[str, list[dict]], now: str) -> dict:
    """Mark items new/old and update the rolling archive."""
    seen = state["seen"]
    for geo, items in items_by_geo.items():
        for it in items:
            key = f"{geo}|{it['q']}"
            rec = seen.get(key)
            if rec is None:
                it["new"] = True
                seen[key] = {
                    "first": now,
                    "last": now,
                    "vol_peak": it.get("vol") or 0,
                    "growth_peak": it.get("growth") or 0,
                    "zh": it.get("zh", ""),
                    "noise": it.get("noise", ""),
                    "cats": it.get("cats", []),
                    **_meta(it),
                }
            else:
                it["new"] = False
                rec["last"] = now
                rec["vol_peak"] = max(rec.get("vol_peak", 0), it.get("vol") or 0)
                rec["growth_peak"] = max(rec.get("growth_peak", 0), it.get("growth") or 0)
                if it.get("zh"):
                    rec["zh"] = it["zh"]
                if it.get("noise"):
                    rec["noise"] = it["noise"]
                if it.get("cats"):
                    rec["cats"] = it["cats"]
                for f in _META_FIELDS:
                    if it.get(f):
                        rec[f] = it[f]
                # Carry post-pass results forward so exports keep showing them.
                if rec.get("seo") and not it.get("seo"):
                    it["seo"] = rec["seo"]
                if rec.get("longtails") and not it.get("longtails"):
                    it["longtails"] = rec["longtails"]
                if rec.get("related") and not it.get("related"):
                    it["related"] = rec["related"]
                if rec.get("signal") and not it.get("signal"):
                    it["signal"] = rec["signal"]
                if it.get("fast"):
                    rec["fast"] = it["fast"]
                if rec.get("fast") and not it.get("fast"):
                    it["fast"] = rec["fast"]
                for f in _META_FIELDS:
                    if rec.get(f) and not it.get(f):
                        it[f] = rec[f]
    return state


def _trend_row(it: dict) -> dict:
    row = {
        "q": it["q"],
        "zh": it.get("zh", ""),
        "vol": it.get("vol") or 0,
        "growth": it.get("growth") or 0,
        "cats": it.get("cats", []),
        "noise": it.get("noise", ""),
        "new": bool(it.get("new")),
    }
    if it.get("seo"):
        row["seo"] = it["seo"]
    if it.get("longtails"):
        row["longtails"] = it["longtails"]
    if it.get("related"):
        row["related"] = it["related"]
    if it.get("signal"):
        row["signal"] = it["signal"]
    if it.get("fast"):
        row["fast"] = it["fast"]
    row.update(_meta(it))
    return row


def export_trends(
    items_by_geo: dict[str, list[dict]],
    geos: list[str],
    cat_names: dict[int, str],
    now: str,
) -> dict:
    present: list[str] = []
    items: dict[str, list[dict]] = {}
    for geo in geos:
        arr = items_by_geo.get(geo) or []
        if arr:
            present.append(geo)
        items[geo] = [_trend_row(it) for it in arr]
    return {
        "updated": now,
        "geos": present,
        "cats": {str(k): v for k, v in cat_names.items()},
        "items": items,
    }


def export_history(state: dict, now: str, days: int = 30) -> dict:
    seen = state.get("seen", {})
    try:
        cutoff = datetime.fromisoformat(now).timestamp() - days * 86400
    except (ValueError, TypeError):
        cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    out: list[dict] = []
    for key, rec in seen.items():
        geo, _, q = key.partition("|")
        try:
            last_ts = datetime.fromisoformat(rec.get("last", "")).timestamp()
        except (ValueError, TypeError):
            last_ts = 0
        if last_ts < cutoff:
            continue
        row = {
            "q": q,
            "zh": rec.get("zh", ""),
            "geo": geo,
            "cats": rec.get("cats", []),
            "vol_peak": rec.get("vol_peak", 0),
            "growth_peak": rec.get("growth_peak", 0),
            "first": rec.get("first"),
            "last": rec.get("last"),
            "noise": rec.get("noise", ""),
        }
        if rec.get("seo"):
            row["seo"] = rec["seo"]
        if rec.get("longtails"):
            row["longtails"] = rec["longtails"]
        if rec.get("related"):
            row["related"] = rec["related"]
        if rec.get("signal"):
            row["signal"] = rec["signal"]
        if rec.get("fast"):
            row["fast"] = rec["fast"]
        for f in _META_FIELDS:
            if rec.get(f):
                row[f] = rec[f]
        out.append(row)
    out.sort(key=lambda r: r.get("vol_peak", 0), reverse=True)
    return {"updated": now, "chunks": 1, "items": out}
