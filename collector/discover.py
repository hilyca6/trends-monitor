"""Off-list keyword discovery via autocomplete prefix enumeration.

Where suggest.py expands *known* trending terms, this walks the alphabet tree
from a set of niche seed terms: for every seed we query
``seed + " " + letter`` (a-z) against Google's free autocomplete endpoint and
collect the suggestions. Those are real search queries — often long-tails that
never appear in Trending Now, i.e. the window where a small site can still win.

Candidates are noise-filtered/translated (enrich), tracked in
state/discover_state.json (first/last seen) and exported to
web/data/discovered.json. Intended to run at most once a day (rate limited).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .enrich import EXTRA_FIELDS, enrich
from .http import build_session
from .store import _load_json, now_iso, prune_state, save_json
from .suggest import fetch_suggest

log = logging.getLogger("trends.discover")

CHARSET = "abcdefghijklmnopqrstuvwxyz"
STATE_NAME = "discover_state.json"


def default_state_path() -> Path:
    return config.STATE_DIR / STATE_NAME


def enumerate_seeds(
    session,
    seeds: list[str],
    *,
    charset: str = CHARSET,
    throttle: float = 0.5,
    budget: int = 800,
    hl: str = "en",
) -> dict[str, dict]:
    """Return {query: {"seeds": [...], "chars": [...]}} for all suggestions.

    Walks ``seed + " " + char`` for each seed/char. Stops once ``budget``
    requests have been made (safety against rate limits). The bare seed and any
    echo of the exact request string are dropped.
    """
    found: dict[str, dict] = {}
    made = 0
    for seed in seeds:
        seed = seed.strip()
        if not seed:
            continue
        for ch in charset:
            if made >= budget:
                log.warning("discover: budget %d reached, stopping early", budget)
                return found
            made += 1
            if throttle:
                time.sleep(throttle)
            sent = f"{seed} {ch}"
            for s in fetch_suggest(session, sent, hl=hl):
                q = s.strip()
                if not q or q.lower() == seed.lower() or q.lower() == sent.lower():
                    continue
                entry = found.setdefault(q, {"seeds": [], "chars": []})
                if seed not in entry["seeds"]:
                    entry["seeds"].append(seed)
                if ch not in entry["chars"]:
                    entry["chars"].append(ch)
        log.info("discover: seed %r -> %d cumulative candidates", seed, len(found))
    return found


def load_state(path: Path) -> dict:
    state = _load_json(path, {"seen": {}})
    state.setdefault("seen", {})
    return state


def is_due(state: dict, now: str, interval_hours: float) -> bool:
    last = state.get("last_run")
    if not last:
        return True
    try:
        delta = datetime.fromisoformat(now) - datetime.fromisoformat(last)
    except (ValueError, TypeError):
        return True
    return delta.total_seconds() >= interval_hours * 3600


def export_discovered(seen: dict, seeds: list[str], now: str, days: int = 30, cap: int = 4000) -> dict:
    """Build discovered.json: recent words, newest first, `new` = first seen now."""
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    items: list[dict] = []
    for q, rec in seen.items():
        try:
            last_ts = datetime.fromisoformat(rec.get("last", "")).timestamp()
        except (ValueError, TypeError):
            last_ts = 0
        if last_ts < cutoff:
            continue
        items.append(
            {
                "q": q,
                "zh": rec.get("zh", ""),
                "noise": rec.get("noise", ""),
                "seed": (rec.get("seeds") or [None])[0],
                "seeds": rec.get("seeds", []),
                "new": rec.get("first") == now,
                "sightings": rec.get("sightings", 1),
                "first": rec.get("first"),
                "last": rec.get("last"),
                **{f: rec[f] for f in EXTRA_FIELDS if rec.get(f)},
            }
        )
    items.sort(key=lambda r: (r["new"], r["last"] or ""), reverse=True)
    return {"updated": now, "seeds": seeds, "count": len(items), "items": items[:cap]}


def run(
    *,
    proxy: str | None,
    seeds: list[str],
    out_dir: Path,
    state_path: Path,
    throttle: float = 0.5,
    budget: int = 800,
    charset: str = CHARSET,
    use_llm: bool = True,
    days: int = 30,
    min_words: int = 2,
    llm_limit: int | None = 600,
    retention_days: int = 90,
) -> int:
    if not seeds:
        log.warning("discover: no seeds configured, nothing to do")
        return 0

    session = build_session(proxy)
    log.info(
        "discover: %d seeds x %d chars (throttle %.1fs, budget %d)",
        len(seeds), len(charset), throttle, budget,
    )
    found = enumerate_seeds(session, seeds, charset=charset, throttle=throttle, budget=budget)
    if not found:
        log.warning("discover: no candidates found")
        return 0

    # Coarse filter: single-token suggestions are usually brand/head terms.
    before = len(found)
    found = {q: v for q, v in found.items() if len(q.split()) >= min_words}
    if before != len(found):
        log.info("discover: dropped %d single-token candidate(s)", before - len(found))
    if not found:
        log.warning("discover: no candidates after filtering")
        return 0

    items = [{"q": q, "cats": []} for q in found]
    enrich(items, use_llm=use_llm, cache_path=state_path.parent / "llm_cache.json", llm_limit=llm_limit)
    meta = {it["q"]: it for it in items}

    now = now_iso()
    state = load_state(state_path)
    seen = state["seen"]
    for q, entry in found.items():
        rec = seen.get(q)
        if rec is None:
            seen[q] = {
                "first": now,
                "last": now,
                "sightings": 1,
                "seeds": entry["seeds"],
                "zh": meta[q].get("zh", ""),
                "noise": meta[q].get("noise", ""),
                **{f: meta[q][f] for f in EXTRA_FIELDS if meta[q].get(f)},
            }
        else:
            rec["last"] = now
            rec["sightings"] = rec.get("sightings", 0) + 1
            for s in entry["seeds"]:
                if s not in rec.setdefault("seeds", []):
                    rec["seeds"].append(s)
            if meta[q].get("zh"):
                rec["zh"] = meta[q]["zh"]
            if meta[q].get("noise"):
                rec["noise"] = meta[q]["noise"]
            for f in EXTRA_FIELDS:
                if meta[q].get(f):
                    rec[f] = meta[q][f]

    state["last_run"] = now
    state["updated"] = now
    prune_state(state, retention_days)
    save_json(state_path, state)

    payload = export_discovered(seen, seeds, now, days=days)
    save_json(out_dir / "discovered.json", payload)
    log.info("discover: %d candidates, %d exported", len(found), len(payload["items"]))
    return len(payload["items"])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Discover off-list keywords via autocomplete enumeration.")
    p.add_argument("--proxy", default=None)
    p.add_argument("--seeds", default="", help="comma-separated seeds (overrides config)")
    p.add_argument("--out", default=str(config.WEB_DATA_DIR))
    p.add_argument("--state", default=str(default_state_path()))
    p.add_argument("--throttle", type=float, default=0.5)
    p.add_argument("--budget", type=int, default=800)
    p.add_argument("--max-llm", type=int, default=600)
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from . import settings

    cfg = settings.apply_environment()
    if args.seeds:
        seeds = [s.strip() for s in args.seeds.split(",") if s.strip()]
    else:
        seeds = [s.strip() for s in ((cfg.get("discover") or {}).get("seeds") or []) if s.strip()]

    run(
        proxy=args.proxy,
        seeds=seeds,
        out_dir=Path(args.out),
        state_path=Path(args.state),
        throttle=args.throttle,
        budget=args.budget,
        use_llm=not args.no_llm,
        llm_limit=args.max_llm,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
