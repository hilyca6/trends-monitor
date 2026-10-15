"""Local runtime settings loaded from config.local.json.

This is how the scheduled task gets its proxy + LLM key without hardcoding
anything. Priority: existing environment variables > config.local.json >
defaults. Note: this loader never sets DEEPSEEK_* (we deliberately avoid it).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import proxy as proxy_mod

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.local.json"

DEFAULTS: dict = {
    "proxy": "",
    "proxy_switch_url": "",
    "proxy_rotate_on_block": True,
    "proxy_switch": {},
    "geos": "default",
    "retention_days": 90,
    "llm": {
        "enabled": True,
        "api_key": "",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
    },
    "games": {
        "enabled": True,
        "sources": "steam,appstore,trending",
        "interval_hours": 3,
        "max_chart": 6,
        "max_refresh": 6,
        "refresh_hours": 12,
        "throttle": 10.0,
        # Skip a curve when a known Google growth (%) is below min_growth, or the
        # ranked position is in the bottom min_pos fraction (0..1).
        "min_growth": 200,
        "min_pos": 0.30,
        # Games this strong (Google growth >= hot_growth, or a chart climb >=
        # hot_rank_change) get their curve refreshed first every run.
        "hot_growth": 500,
        "hot_rank_change": 0.20,
        "max_hot": 3,
        "history_days": 7,
    },
    "gameradar": {
        # Game-name radar: discover emerging web-game names from browser-game
        # portals, then reuse the advice/fastheat layers for verification.
        "enabled": True,
        "sources": "itch,itch-popular,itch-new-feed,itch-featured-feed,crazygames,poki,y8,gamepix,lagged,newgrounds",
        "interval_hours": 3,
        "throttle": 1.0,
    },
    "youtube": {
        # YouTube Data API v3 key (Google Cloud Console). Adds a video-side heat
        # signal to the game radar. Empty key = skipped. Free quota 10k units/day.
        "enabled": True,
        "api_key": "",
        "budget": 3,
        "throttle": 1.0,
        "max_age_hours": 72,
    },
    "opportunity": {
        # Opportunity score + hard gates (test-now / independent) for the game
        # radar. Independent stays off until social spillover (#4) is wired.
        "enabled": True,
    },
    "social": {
        # Cross-platform spillover for the game radar. Providers without
        # credentials are skipped. YouTube reuses the game radar's own data.
        "enabled": True,
        "budget": 5,
        "max_age_hours": 6,
        "min_platforms": 2,
        "throttle": 0.5,
        "timeout_ms": 20000,
        "reddit_client_id": "",
        "reddit_client_secret": "",
        "reddit_user_agent": "trendradar/1.0",
        "x_bearer_token": "",
        "tiktok_research_token": "",
    },
    "seo": {
        "enabled": True,
        "serp_budget": 20,
        "suggest_budget": 20,
        "serp_throttle": 3.0,
        "geo": "US",
        "refresh_hours": 24,
        "cache_days": 7,
    },
    "fast": {
        # Fast-heat layer: baseline snapshots of autocomplete + SERP results,
        # then report *new* long-tails/pages per term (state/fast_state.json).
        "enabled": True,
    },
    "related": {
        # Rising related words mined from the (now-disabled) discovered words.
        # Disabled by default; re-enable together with `discover` to use it.
        "enabled": False,
        "budget": 20,
        "min_stars": 0,
        "window": "now 7-d",
        "throttle": 4.0,
    },
    "signal": {
        # Baseline-relative Google Trends check (the "GPTs multiple" idea).
        # Fetches [term, baseline] in ONE request over several windows, then
        # reports ratio / classification / freshness. Cache: state/signal_cache.json
        "enabled": True,
        "baseline": "gpts",
        "anchors": {"generic": "gpts", "wiki": "steam", "online": "crazy games"},
        "windows": {"7d": "now 7-d", "30d": "today 1-m", "90d": "today 3-m"},
        "budget": 20,
        "gameradar_budget": 8,
        "throttle": 4.0,
        "breakout_ratio": 5.0,
        "rising_ratio": 1.0,
        "recent_hours": 0,
    },
    "capcut": {
        # CapCut "most-used" template monitor (-> web/data/capcut.json).
        # Independent of the Google/Bing budgets; use-counts are exact (read from
        # the embedded SSR JSON). Video only by default (image templates carry no
        # use-count); set template_types to ["video", "image"] to add them.
        "enabled": True,
        "interval_hours": 4,
        "template_types": ["video"],
        "max_pages": 12,
        "throttle": 1.5,
        "history_days": 3,
    },
    "sitemap": {
        # First-mover signal: watch competitors' sitemaps, rank keywords by how
        # many independent domains list them (state/sitemap_state.json).
        # Default seeds are public web-game portals; add your own niche sites.
        "enabled": True,
        "interval_hours": 24,
        "watch_urls": [
            "https://itch.io/sitemaps/games.xml",
            "https://www.silvergames.com/sitemap-games-1.xml",
            "https://www.addictinggames.com/sitemap.xml",
        ],
        "throttle": 1.0,
        "budget": 2000,
        "max_sitemaps": 30,
        "min_words": 1,
        "max_llm": 120,
    },
    "advice": {
        "enabled": True,
        "budget": 20,
        "seo_budget": 50,
        "gameradar_seo_budget": 40,
        "ttl_days": 7,
        "batch": 8,
        "workers": 6,
        "weights": {"demand": 0.35, "competition": 0.40, "window": 0.25},
    },
    "discover": {
        # Off-list keyword discovery via autocomplete enumeration. Disabled by
        # default: the AI/finance seed words produced low-value results. Turn on
        # and tune `seeds` if you want it back.
        "enabled": False,
        "interval_hours": 24,
        "throttle": 0.5,
        "budget": 500,
        "max_llm": 250,
        "seeds": [
            # AI tools
            "best ai",
            "ai tool for",
            "best ai tool for",
            "free ai",
            "ai for",
            "chatgpt vs",
            "ai vs",
            "how to use ai",
            "chatgpt",
            "claude",
            "gemini",
            "midjourney",
            "perplexity",
            "stable diffusion",
            "notion ai",
            # finance
            "best credit card",
            "best credit card for",
            "how to invest",
            "how to invest in",
            "how to save",
            "how to budget",
            "best savings",
            "high yield",
            "how to file tax",
            "best cd",
            "best index fund",
            "etf vs",
            "mortgage",
            "crypto",
            "how to buy",
        ],
    },
    "push": {
        "enabled": False,
        "daily_at_hour": [5, 17],
        "quiet_hours": "23-8",
        "min_stars": 3,
        "top_gameradar": 5,
        "top_sitemap": 5,
        "urgent": {
            # Immediate alerts that skip the scheduled digest. Shares pushed.json
            # dedup with the digest. ignore_quiet=True pushes even at night.
            "enabled": True,
            "ignore_quiet": False,
            "gameradar": ["independent"],
            "gameradar_test_score": 70,
            "social": ["viral"],
            "game_breakout": True,
            "sitemap_7d": 3,
            "trend_breakout_stars": 4,
        },
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Path | None = None) -> dict:
    path = path or CONFIG_PATH
    local: dict = {}
    if path.exists():
        try:
            local = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            local = {}
    return _merge(DEFAULTS, local)


def apply_environment(cfg: dict | None = None) -> dict:
    """Export config to env vars so the collector modules pick them up."""
    cfg = cfg or load_config()

    # Proxy may be a single string or a list; a list enables IP rotation on block.
    proxy_cfg = cfg.get("proxy")
    if isinstance(proxy_cfg, list):
        proxies = proxy_cfg
    elif proxy_cfg:
        proxies = [proxy_cfg]
    else:
        proxies = []
    proxy_mod.configure(
        proxies,
        switch_url=str(cfg.get("proxy_switch_url") or ""),
        rotate_on_block=bool(cfg.get("proxy_rotate_on_block", True)),
        switch=cfg.get("proxy_switch") or {},
    )

    llm = cfg.get("llm") or {}
    key = (llm.get("api_key") or "").strip()
    if key and llm.get("enabled", True):
        os.environ["OPENAI_API_KEY"] = key
        if llm.get("base_url"):
            os.environ["OPENAI_BASE_URL"] = llm["base_url"]
        if llm.get("model"):
            os.environ["OPENAI_MODEL"] = llm["model"]
        # honour "don't use DeepSeek" — make sure its key can't be picked up
        os.environ.pop("DEEPSEEK_API_KEY", None)

    try:
        os.environ["LLM_CACHE_DAYS"] = str(int(cfg.get("retention_days", 90)))
    except (ValueError, TypeError):
        pass

    return cfg
