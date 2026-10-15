"""Game-name radar orchestration.

Discover new web-game names from game portals, track first-seen / sightings,
verify YouTube heat, and write ``web/data/gameradar.json``. The remaining
verification (Bing SERP competition, market gap, name risk, ★stars) is applied
afterwards by the existing ``advice`` / ``fastheat`` post-passes, which read
gameradar.json too.

Usage:
    python -m collector.gameradar.radar
    python -m collector.gameradar.radar --sources itch,crazygames,newgrounds
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from ..games.store import is_junk, is_web_game
from ..http import build_session
from ..store import _load_json, now_iso, save_json
from . import sources as src
from . import youtube
from .store import export, load_state, persist_enrichment, prune_state, save_state, update

log = logging.getLogger("trends.gameradar.radar")

_HOUR_MS = 3_600_000


def _to_ms(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp() * 1000
    except (ValueError, TypeError):
        return 0.0


def _youtube_pass(state: dict, api_key: str, budget: int, throttle: float, max_age_hours: float) -> int:
    """Verify YouTube heat for the top game names not checked recently."""
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    max_age_ms = max_age_hours * _HOUR_MS

    def _due(rec: dict) -> bool:
        checked = (rec.get("youtube") or {}).get("checkedAt")
        if not checked:
            return True
        t = _to_ms(checked)
        return not t or (now_ms - t) > max_age_ms

    cands = [
        rec for rec in state.get("games", {}).values()
        if rec.get("name") and not is_junk(rec["name"]) and _due(rec)
    ]
    # Web games and more-sighted names first.
    cands.sort(key=lambda r: (is_web_game(r["name"]), r.get("sightings", 0)), reverse=True)

    verified = 0
    for rec in cands:
        if verified >= budget:
            break
        try:
            result = youtube.verify_youtube(rec["name"], api_key, throttle=throttle, now_ms=now_ms)
        except youtube.YouTubeError as e:
            log.warning("youtube: %s failed: %s", rec["name"], e)
            continue
        if result:
            rec["youtube"] = result
            verified += 1
    return verified


def run(
    *,
    proxy: str | None = None,
    sources: list[str] | None = None,
    out_dir: Path,
    state_path: Path,
    throttle: float = 1.0,
    retention_days: int = 90,
    youtube_api_key: str = "",
    youtube_budget: int = 0,
    youtube_throttle: float = 1.0,
    youtube_max_age_hours: float = 72,
) -> int:
    session = build_session(proxy)
    candidates = src.discover(session, sources or src.DEFAULT_SOURCES, throttle)
    log.info("gameradar: %d raw candidates", len(candidates))

    state = load_state(state_path)
    now = now_iso()
    new_keys = update(state, candidates, now)

    yt_verified = 0
    if youtube_api_key and youtube_budget > 0:
        yt_verified = _youtube_pass(state, youtube_api_key, youtube_budget, youtube_throttle, youtube_max_age_hours)

    prune_state(state, retention_days)
    save_state(state_path, state)

    # Carry forward post-pass enrichment (seo/advice/signal/social/opportunity)
    # from the previous export so re-discovery does not wipe it.
    prev_payload = _load_json(out_dir / "gameradar.json", None)
    previous: dict = {}
    if isinstance(prev_payload, dict):
        for it in prev_payload.get("items") or []:
            key = " ".join(str(it.get("name") or "").strip().lower().split())
            if key:
                previous[key] = it

    payload = export(state, now, previous=previous)
    save_json(out_dir / "gameradar.json", payload)
    log.info(
        "gameradar: %d candidates, %d new, %d youtube, %d exported",
        len(candidates), len(new_keys), yt_verified, len(payload["items"]),
    )
    return len(payload["items"])


def persist(out_dir: Path, state_path: Path) -> int:
    """Persist post-pass enrichment from gameradar.json back into state.

    Run at the end of the pipeline so the next re-discovery export keeps the
    seo/advice/signal/social/opportunity fields.
    """
    payload = _load_json(out_dir / "gameradar.json", None)
    if not isinstance(payload, dict):
        return 0
    state = load_state(state_path)
    n = persist_enrichment(state, payload.get("items") or [])
    if n:
        save_state(state_path, state)
    return n


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Game-name radar collector.")
    p.add_argument("--proxy", default=None)
    p.add_argument("--sources", default=",".join(src.DEFAULT_SOURCES))
    p.add_argument("--throttle", type=float, default=1.0)
    p.add_argument("--out", default=str(config.WEB_DATA_DIR))
    p.add_argument("--state", default=str(config.GAMERADAR_STATE_PATH))
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from .. import settings

    cfg = settings.apply_environment()
    yt = cfg.get("youtube") or {}
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    run(
        proxy=args.proxy,
        sources=sources,
        out_dir=Path(args.out),
        state_path=Path(args.state),
        throttle=args.throttle,
        youtube_api_key=str(yt.get("api_key") or ""),
        youtube_budget=int(yt.get("budget", 3)),
        youtube_throttle=float(yt.get("throttle", 1.0)),
        youtube_max_age_hours=float(yt.get("max_age_hours", 72)),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
