"""CLI orchestrator: fetch trending terms -> enrich -> state -> write JSON.

Usage:
    python -m collector.collect --geos default --proxy http://127.0.0.1:7897
    python -m collector.collect --geos US,GB,JP --no-llm
    python -m collector.collect --sample          # write demo data, no network
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone

from . import config
from .enrich import enrich
from .gtrends_api import BlockedError
from .http import build_session
from .providers import GoogleTrendsRSS, GoogleTrendsSSR
from .providers.base import TrendProvider
from .store import (
    _load_json,
    export_history,
    export_trends,
    load_state,
    now_iso,
    prune_state,
    save_json,
    update_state,
)

log = logging.getLogger("trends.collect")

CHECKPOINT_NAME = "collect_partial.json"


def _cache_path(state_path) -> "object":
    return state_path.parent / "llm_cache.json"


def _checkpoint_path(state_path):
    return state_path.parent / CHECKPOINT_NAME


def _load_partial(path, geos: list[str], max_age_hours: float = 6.0) -> dict[str, list[dict]]:
    """Return {geo: items} from a recent checkpoint (crash resume), else {}.

    Stale checkpoints (older than `max_age_hours`) are ignored so a task that
    keeps crashing doesn't resume from ancient data.
    """
    saved = _load_json(path, None)
    if not saved or not isinstance(saved.get("items"), dict):
        return {}
    try:
        age = datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(saved["updated"]).timestamp()
    except (ValueError, TypeError, KeyError):
        return {}
    if age > max_age_hours * 3600:
        log.info("ignoring stale checkpoint (%.1fh old)", age / 3600)
        return {}
    return {g: arr for g, arr in saved["items"].items() if g in geos and arr}


def fetch_geo(primary: TrendProvider, fallback: TrendProvider | None, geo: str) -> list[dict]:
    """Fetch one geo, falling back when the primary returns empty or raises.

    Returns the items plus which provider actually served them (via .name).
    A global "primary is broken" signal is emitted once the fallback has been
    used for several geos in a row.
    """
    try:
        raw = primary.fetch(geo)
    except BlockedError:
        raise  # /sorry/ or 429: let the caller stop the whole run
    except Exception as e:  # noqa: BLE001
        log.warning("provider %s failed for %s: %s", primary.name, geo, e)
        raw = []
    if raw or fallback is None or fallback is primary:
        return raw

    log.warning("%s empty for %s; falling back to %s", primary.name, geo, fallback.name)
    try:
        raw = fallback.fetch(geo)
    except BlockedError:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("fallback %s failed for %s: %s", fallback.name, geo, e)
        return []
    if raw:
        for it in raw:
            it["src"] = fallback.name
    return raw


def resolve_geos(spec: str) -> list[str]:
    spec = (spec or "default").strip().lower()
    if spec in ("all", "*"):
        return list(config.ALL_GEOS)
    if spec in ("default", ""):
        return list(config.DEFAULT_GEOS)
    return [g.strip().upper() for g in spec.split(",") if g.strip()]


def cat_names(lang: str) -> dict[int, str]:
    return config.CATEGORY_NAMES_EN if lang == "en" else config.CATEGORY_NAMES_ZH


def dedupe_sort(items: list[dict], cap: int = 1000) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for it in items:
        if it["q"] in seen:
            continue
        seen.add(it["q"])
        out.append(it)
    out.sort(key=lambda r: (-(r.get("vol") or 0), -(r.get("growth") or 0)))
    return out[:cap]


def run(
    geos: list[str],
    *,
    proxy: str | None,
    provider_name: str,
    use_llm: bool,
    out_dir,
    state_path,
    throttle: float = 0.6,
    retention_days: int = 90,
) -> int:
    session = build_session(proxy)
    primary: TrendProvider = (
        GoogleTrendsRSS(session) if provider_name == "rss" else GoogleTrendsSSR(session)
    )
    fallback: TrendProvider | None = (
        GoogleTrendsRSS(session) if provider_name == "ssr" else GoogleTrendsSSR(session)
    )
    log.info("provider=%s (fallback=%s) geos=%d", primary.name, fallback.name, len(geos))

    state = load_state(state_path)
    items_by_geo: dict[str, list[dict]] = {}
    cats = cat_names("zh")
    fallback_used = 0

    # Resume from a checkpoint left by a crashed run so we don't refetch geos.
    checkpoint = _checkpoint_path(state_path)
    items_by_geo = _load_partial(checkpoint, geos)
    if items_by_geo:
        log.info("resuming from checkpoint: %d geo(s) already fetched", len(items_by_geo))

    # Phase 1: fetch every geo (network only, no LLM yet). Persist a checkpoint
    # after each geo so a crash/kill doesn't lose the work.
    for idx, geo in enumerate(geos):
        if geo in items_by_geo:
            continue
        if idx and throttle:
            time.sleep(throttle)
        try:
            raw = fetch_geo(primary, fallback, geo)
        except BlockedError as e:
            log.error("collect: Google blocked us (/sorry/ or 429); stopping this run: %s", e)
            break
        if raw and all(it.get("src") == fallback.name for it in raw):
            fallback_used += 1
            if fallback_used == 3:
                log.error(
                    "%s has failed for %d geos; it is probably broken (Google changed the page?)",
                    primary.name,
                    fallback_used,
                )
        raw = dedupe_sort(raw)
        if not raw:
            log.warning("no data for %s", geo)
            continue
        items_by_geo[geo] = raw
        save_json(checkpoint, {"updated": now_iso(), "items": items_by_geo})

    # Phase 2: enrich ONCE across all geos so the LLM batches (50/req, 8 workers)
    # instead of firing one slow request per geo. Cache persists partial progress.
    if items_by_geo:
        flat = [it for arr in items_by_geo.values() for it in arr]
        enrich(flat, use_llm=use_llm, cache_path=_cache_path(state_path))

    # Phase 3: persist state + exports.
    ts = now_iso()
    for geo, raw in items_by_geo.items():
        update_state(state, {geo: raw}, ts)
    prune_state(state, retention_days)
    save_json(state_path, state)
    save_json(out_dir / "trends.json", export_trends(items_by_geo, geos, cats, ts))
    save_json(out_dir / "history.json", export_history(state, ts))

    # Full run succeeded: the checkpoint is no longer needed.
    try:
        checkpoint.unlink(missing_ok=True)
    except OSError:
        pass

    games_path = out_dir / "games.json"
    if not games_path.exists():
        save_json(games_path, {"updated": now_iso(), "items": []})

    total = sum(len(a) for a in items_by_geo.values())
    done = [g for g in geos if g in items_by_geo]
    log.info("wrote %d trends across %d geos -> %s", total, len(done), out_dir)
    return total


def enrich_only(out_dir, state_path, use_llm: bool = True) -> int:
    """Enrich an existing trends.json/history.json without re-fetching."""
    trends = _load_json(out_dir / "trends.json", None)
    if not trends:
        log.error("no trends.json found in %s", out_dir)
        return 0
    history = _load_json(out_dir / "history.json", {"items": []})

    uniq: dict[str, dict] = {}
    for arr in (trends.get("items") or {}).values():
        for it in arr:
            uniq.setdefault(it["q"], {"q": it["q"], "cats": it.get("cats", [])})
    for it in history.get("items", []):
        uniq.setdefault(it["q"], {"q": it["q"], "cats": it.get("cats", [])})

    items = list(uniq.values())
    enrich(items, use_llm=use_llm, cache_path=_cache_path(state_path))
    lut = {it["q"]: it for it in items}

    for arr in (trends.get("items") or {}).values():
        for it in arr:
            src = lut.get(it["q"])
            if src:
                it["zh"] = src.get("zh", "")
                it["noise"] = src.get("noise", "")
    for it in history.get("items", []):
        src = lut.get(it["q"])
        if src:
            it["zh"] = src.get("zh", "")
            it["noise"] = src.get("noise", "")

    state = load_state(state_path)
    for key, rec in state.get("seen", {}).items():
        src = lut.get(key.partition("|")[2])
        if src:
            if src.get("zh"):
                rec["zh"] = src["zh"]
            if src.get("noise"):
                rec["noise"] = src["noise"]

    ts = now_iso()
    trends["updated"] = ts
    history["updated"] = ts
    save_json(out_dir / "trends.json", trends)
    save_json(out_dir / "history.json", history)
    save_json(state_path, state)
    log.info("enriched %d unique queries", len(items))
    return len(items)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Collect Google Trends trending terms.")
    p.add_argument("--geos", default="default", help="default | all | comma list e.g. US,GB,JP")
    p.add_argument("--provider", choices=["ssr", "rss"], default="ssr")
    p.add_argument("--proxy", default=None, help="e.g. http://127.0.0.1:7897")
    p.add_argument("--out", default=str(config.WEB_DATA_DIR))
    p.add_argument("--state", default=str(config.STATE_DIR / "state.json"))
    p.add_argument("--no-llm", action="store_true", help="disable optional LLM enrichment")
    p.add_argument("--sample", action="store_true", help="write demo data (no network)")
    p.add_argument("--enrich-only", action="store_true", help="enrich existing data without re-fetching")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from . import settings

    settings.apply_environment()

    from pathlib import Path

    if args.sample:
        from . import sample_data

        sample_data.write_demo(Path(args.out))
        return 0

    if args.enrich_only:
        return 0 if enrich_only(Path(args.out), Path(args.state), use_llm=not args.no_llm) else 1

    return run(
        resolve_geos(args.geos),
        proxy=args.proxy,
        provider_name=args.provider,
        use_llm=not args.no_llm,
        out_dir=Path(args.out),
        state_path=Path(args.state),
    )


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
