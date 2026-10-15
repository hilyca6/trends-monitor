"""Per-keyword SEO advice.

Two parts, deliberately split:

  * ``stars`` (1-5) are computed in **code** from signals we already have —
    Bing opportunity score (trends), trend momentum (games), coverage
    (related words), plus growth / breakout bonuses. Deterministic and free.
  * the written notes (audience / directions / main keyword / monetization /
    risk / evidence / window) are filled by the **LLM**, batched and cached in
    state/advice_cache.json with a TTL so time-sensitive fields (window,
    evidence) refresh periodically.

Targets four lists: this-hour's clean hot terms (trends.json), rising related
words (related.json), off-list discovered words (discovered.json) and new games
(games.json). Advice is written back into each item as an ``advice`` object.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

from .enrich import LLMError, _chunks, _resolve_llm
from .seocheck import check_queries
from .store import _load_json, now_iso, save_json

log = logging.getLogger("trends.advice")

TEXT_FIELDS = ("audience", "main_keyword", "risk", "evidence", "window")
LIST_FIELDS = ("directions", "monetize")

# Default weights for the unified "worth doing" score behind the 1-5 stars.
# Overridable via config.local.json -> advice.weights {demand, competition, window}
# (any relative numbers; they are normalised to sum to 1).
DEFAULT_WEIGHTS = {"demand": 0.35, "competition": 0.40, "window": 0.25}


def _weights(cfg_weights) -> dict:
    w = dict(DEFAULT_WEIGHTS)
    if isinstance(cfg_weights, dict):
        for k in w:
            v = cfg_weights.get(k)
            if isinstance(v, (int, float)) and v >= 0:
                w[k] = float(v)
    total = sum(w.values())
    if total > 0:
        w = {k: v / total for k, v in w.items()}
    return w


# ---------------------------------------------------------------------------
# Stars = "worth doing" (code-computed): demand x competition x timing
# ---------------------------------------------------------------------------
def _demand(kind: str, item: dict) -> float:
    """0-100 demand/interest signal, per kind."""
    if kind == "trend":
        vol = item.get("vol") or 0
        if vol >= 1000000:
            d = 90
        elif vol >= 500000:
            d = 75
        elif vol >= 100000:
            d = 55
        elif vol >= 20000:
            d = 30
        elif vol >= 500:
            d = 10
        else:
            d = 0
        g = item.get("growth") or 0
        if g >= 1000:
            d += 10
        elif g >= 500:
            d += 5
        return min(100.0, float(d))
    if kind == "related":
        geos = len(item.get("geos") or [])
        return float([0, 25, 45, 65, 80][min(geos, 4)])
    if kind == "game":
        d = float(item.get("score") or 0)
        if item.get("breakout"):
            d += 15
        return min(100.0, d)
    if kind == "gameradar":
        # No traffic signal yet: modest demand, small boost for a fresh find or
        # a domain-style web game (usually viral browser games). Kept below the
        # traffic-backed lists so it does not hog the shared SEO budget.
        d = 40.0
        if item.get("new"):
            d += 8
        if item.get("web_game"):
            d += 5
        return min(100.0, d)
    return 50.0  # discover: no demand signal


def _competition(item: dict, fallback: float = 50.0) -> float:
    """0-100 'easy to rank' (Bing opportunity). Higher = easier = better.

    Falls back to ``fallback`` when there is no SERP score — e.g. a sitemap-based
    ease estimate (see :func:`_sitemap_ease`), so a blocked SERP does not flatten
    every ★ to the same value.
    """
    v = (item.get("seo") or {}).get("score")
    return float(v) if isinstance(v, (int, float)) else fallback


def _window(item: dict) -> float:
    """0-100 timing: is now a good moment."""
    if item.get("new") and item.get("breakout"):
        return 100.0
    if item.get("breakout") or (item.get("rise") or 0) >= 2:
        return 85.0
    if item.get("new"):
        return 70.0
    return 50.0


# Brand / product-model words are competitive and not buildable for a small
# niche site, so they get their stars knocked down.
_BRAND_RE = re.compile(
    r"\b(samsung|galaxy|pixel|iphone|ipad|macbook|airpods|xiaomi|redmi|oppo|vivo|"
    r"huawei|honor|oneplus|realme|nokia|dazn|netflix|spotify|disney|hulu|"
    r"playstation|xbox|nintendo|gta|snapdragon|geforce|rtx|ryzen|nvidia|"
    r"whatsapp|instagram|tiktok)\b",
    re.IGNORECASE,
)
_MODEL_RE = re.compile(r"\b[a-z]{1,3}\d{1,4}[a-z]?\b", re.IGNORECASE)


def _brand_penalty(item: dict) -> float:
    q = (item.get("name") or item.get("q") or "").lower()
    if not q:
        return 0.0
    pen = 0.0
    if _BRAND_RE.search(q):
        pen -= 20.0
    if _MODEL_RE.search(q):
        pen -= 15.0
    return pen


def compute_stars(kind: str, item: dict, weights: dict | None = None, fallback: float = 50.0) -> int:
    """1-5 stars = \"worth doing\": demand x competition x timing (no LLM)."""
    w = weights or DEFAULT_WEIGHTS
    score = (
        w["demand"] * _demand(kind, item)
        + w["competition"] * _competition(item, fallback)
        + w["window"] * _window(item)
        + _brand_penalty(item)
    )
    return max(1, min(5, int(score / 20 + 0.5)))


def _sitemap_ease(out_dir) -> dict:
    """query -> 0-100 'easy to rank' estimated from competitor-sitemap spread.

    A keyword covered by many independent domains is contested, so ease drops.
    Zero-search, zero-rate-limit fallback for when Bing *and* Yahoo both fail.
    """
    d = _load_json(out_dir / "sitemap.json", None)
    m: dict[str, float] = {}
    if isinstance(d, dict):
        for it in d.get("items") or []:
            q = (it.get("q") or "").lower()
            if not q:
                continue
            dc = it.get("domain_count") or 0
            m[q] = max(10.0, min(90.0, 90.0 - dc * 15.0))
    return m


# ---------------------------------------------------------------------------
# Recommendation level + content opportunities (deterministic, no LLM)
# ---------------------------------------------------------------------------
def content_opportunities(q: str, kind: str = "trend", cap: int = 6) -> list[dict]:
    """Turn one keyword into concrete page targets a small site could build.

    A fixed, explainable set of suffix patterns per kind, rated by how directly
    they match search intent. Mirrors the game-radar tools' "content
    opportunities" (e.g. ``{q} wiki``, ``{q} how to``, ``{q} walkthrough``).
    Cheap and language-agnostic (no LLM).
    """
    q = (q or "").strip()
    if not q:
        return []
    if kind == "game" or kind == "gameradar":
        out = [
            {"query": q, "intent": "landing", "priority": 61},
            {"query": f"{q} wiki", "intent": "wiki", "priority": 55},
            {"query": f"{q} download", "intent": "download", "priority": 44},
            {"query": f"{q} free", "intent": "tool", "priority": 40},
            {"query": f"{q} walkthrough", "intent": "guide", "priority": 37},
        ]
    else:
        out = [
            {"query": q, "intent": "landing", "priority": 61},
            {"query": f"{q} how to", "intent": "guide", "priority": 50},
            {"query": f"best {q}", "intent": "list", "priority": 48},
            {"query": f"{q} vs", "intent": "compare", "priority": 45},
            {"query": f"{q} free", "intent": "tool", "priority": 42},
            {"query": f"{q} online", "intent": "tool", "priority": 40},
        ]
    out.sort(key=lambda e: e["priority"], reverse=True)
    return out[:cap]


def compute_level(kind: str, item: dict, stars: int) -> str:
    """Coarse action label: 值得做 / 测试 / 观察 / 淘汰.

    A strong fresh signal (breakout/rising + new) lifts a 3-star item into
    测试, matching the "test now" tier from the game-radar tools. An occupied /
    established SERP can't be a top "值得做".
    """
    sig = item.get("signal") or {}
    cls = sig.get("classification")
    fresh = sig.get("freshness")
    market = ((item.get("seo") or {}).get("market") or {}).get("status")
    if stars >= 4:
        return "测试" if market == "occupied" else "值得做"
    if stars >= 3 or (cls in ("breakout", "rising") and fresh == "new"):
        return "测试"
    if stars >= 2:
        return "观察"
    return "淘汰"


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
def _fresh(rec: dict | None, ttl_days: float, now: str) -> bool:
    ts = (rec or {}).get("ts")
    if not ts:
        return False
    try:
        age = datetime.fromisoformat(now) - datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return False
    return age.total_seconds() < ttl_days * 86400


def _load_cache(path) -> dict:
    d = _load_json(Path(path), {})
    return d if isinstance(d, dict) else {}


def _prune_cache(cache: dict, days: int) -> dict:
    """Drop advice entries not used within `days` (bounds advice_cache.json)."""
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    out: dict = {}
    for q, rec in cache.items():
        if not isinstance(rec, dict):
            continue
        ts = rec.get("ts")
        if not ts:
            out[q] = rec
            continue
        try:
            fresh = datetime.fromisoformat(ts).timestamp() >= cutoff
        except (ValueError, TypeError):
            fresh = True
        if fresh:
            out[q] = rec
    return out


# ---------------------------------------------------------------------------
# LLM (text fields only)
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are an SEO analyst helping a small publisher pick keywords. "
    "For each input item you get a keyword plus short context signals. "
    "Fill these fields, all in Simplified Chinese: "
    "audience (who searches for it), "
    "directions (up to 3 concrete site angles a small site could build, array), "
    "main_keyword (the single best query to target), "
    "monetize (array of realistic monetization ways), "
    "risk (the main risk, short), "
    "evidence (why it is worth doing, grounded in the given signals, short), "
    "window (estimated opportunity window, e.g. '1-2周' or '长期'). "
    'Respond ONLY with JSON {"results":[{"q":"...","audience":"...",'
    '"directions":[...],"main_keyword":"...","monetize":[...],"risk":"...",'
    '"evidence":"...","window":"..."}]} in the same order as the input items.'
)


def _clean_row(row: dict) -> dict:
    rec = {f: str(row.get(f, "") or "") for f in TEXT_FIELDS}
    dirs = [str(x) for x in (row.get("directions") or []) if str(x).strip()]
    money = [str(x) for x in (row.get("monetize") or []) if str(x).strip()]
    rec["directions"] = dirs[:3]
    rec["monetize"] = money[:4]
    return rec


def _call_batch(url, headers, model, items) -> dict:
    payload = {
        "model": model,
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({"items": items}, ensure_ascii=False)},
        ],
    }
    r = requests.post(url, headers=headers, json=payload, timeout=120)
    if r.status_code != 200:
        raise LLMError(r.status_code, r.text)
    content = r.json()["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    rows = parsed.get("results", parsed) if isinstance(parsed, dict) else parsed
    out: dict[str, dict] = {}
    for row in rows:
        if isinstance(row, dict) and row.get("q"):
            out[str(row["q"])] = _clean_row(row)
    return out


def _fetch(url, headers, model, items, depth: int = 0) -> dict:
    try:
        return _call_batch(url, headers, model, items)
    except LLMError as e:
        if e.status == 400 and len(items) > 1 and depth < 6:
            mid = len(items) // 2
            out = _fetch(url, headers, model, items[:mid], depth + 1)
            out.update(_fetch(url, headers, model, items[mid:], depth + 1))
            return out
        log.warning("advice batch failed: %s", e)
        return {}
    except requests.RequestException as e:  # noqa: BLE001
        log.warning("advice request failed: %s", e)
        return {}


def _context(kind: str, item: dict) -> dict:
    parts: list[str] = []
    if item.get("zh"):
        parts.append(item["zh"])
    if item.get("geo"):
        parts.append(item["geo"])
    if kind == "related" and item.get("geos"):
        parts.append("地区:" + "/".join((item.get("geos") or [])[:5]))
    if item.get("growth"):
        parts.append(f"涨幅+{item['growth']}%")
    if item.get("rise"):
        parts.append(f"环比+{round(item['rise'] * 100)}%")
    seo = item.get("seo") or {}
    if seo.get("label") and seo.get("label") != "?":
        parts.append(f"可做性{seo['label']}")
    if item.get("breakout"):
        parts.append("爆发中")
    sig = item.get("signal") or {}
    if sig.get("ratio"):
        parts.append(f"相对基准×{sig['ratio']}")
    if sig.get("classification") and sig["classification"] != "none":
        parts.append(f"趋势{sig.get('label') or sig['classification']}")
    if sig.get("freshness") == "new":
        parts.append("新词")
    if sig.get("entityConflict"):
        parts.append("实体冲突")
    fast = item.get("fast") or {}
    if fast.get("newSuggestionCount") or fast.get("newSerpPageCount"):
        parts.append(f"补全+{fast.get('newSuggestionCount', 0)}/SERP+{fast.get('newSerpPageCount', 0)}")
    if item.get("cat") or item.get("subcat"):
        parts.append("/".join(x for x in (item.get("cat"), item.get("subcat")) if x))
    if item.get("trigger"):
        parts.append(f"成因{item['trigger']}")
    return {"q": item.get("name") or item.get("q"), "kind": kind, "ctx": " · ".join(parts)}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _candidates(out_dir: Path):
    """Yield (path, full_data, [(kind, item_dict)])."""
    groups = []
    tp = out_dir / "trends.json"
    d = _load_json(tp, None)
    if isinstance(d, dict):
        sel = [
            ("trend", it)
            for arr in (d.get("items") or {}).values()
            for it in arr
            if not it.get("noise")
        ]
        groups.append((tp, d, sel))
    rp = out_dir / "related.json"
    d = _load_json(rp, None)
    if isinstance(d, dict):
        groups.append((rp, d, [("related", it) for it in (d.get("items") or [])]))
    dp = out_dir / "discovered.json"
    d = _load_json(dp, None)
    if isinstance(d, dict):
        sel = [("discover", it) for it in (d.get("items") or []) if it.get("new") and not it.get("noise")]
        groups.append((dp, d, sel))
    gp = out_dir / "games.json"
    d = _load_json(gp, None)
    if isinstance(d, dict):
        groups.append((gp, d, [("game", it) for it in (d.get("items") or []) if it.get("chart_at")]))
    grp = out_dir / "gameradar.json"
    d = _load_json(grp, None)
    if isinstance(d, dict):
        groups.append((grp, d, [("gameradar", it) for it in (d.get("items") or [])[:300] if it.get("name")]))
    sp = out_dir / "sitemap.json"
    d = _load_json(sp, None)
    if isinstance(d, dict):
        items = [it for it in (d.get("items") or []) if not it.get("noise")]
        items.sort(key=lambda it: (it.get("domain_count_7d") or 0, it.get("domain_count") or 0), reverse=True)
        groups.append((sp, d, [("sitemap", it) for it in items[:200]]))
    return groups


def run(cfg: dict, out_dir: Path, state_path: Path) -> int:
    a = cfg.get("advice") or {}
    if not a.get("enabled", True):
        return 0

    budget = int(a.get("budget", 20))
    ttl_days = float(a.get("ttl_days", 7))
    cache_days = int(a.get("cache_days", 90))
    batch = max(1, int(a.get("batch", 8)))
    workers = max(1, int(a.get("workers", 6)))
    weights = _weights(a.get("weights"))
    now = now_iso()
    cache_path = state_path.parent / "advice_cache.json"
    cache = _load_cache(cache_path)

    groups = _candidates(out_dir)
    by_q: dict[str, tuple] = {}
    for _path, _data, sel in groups:
        for kind, it in sel:
            q = it.get("name") or it.get("q")
            if q:
                by_q[q] = (kind, it)
    if not by_q:
        return 0

    # Competition signal: score candidates that have no Bing opportunity score
    # yet (rising words, discovered words, games, trends beyond the SEO budget).
    need = [
        q
        for q, (kind, it) in by_q.items()
        if not isinstance((it.get("seo") or {}).get("score"), (int, float))
    ]
    need.sort(key=lambda q: _demand(by_q[q][0], by_q[q][1]), reverse=True)
    # Reserve a share of the shared SEO budget for game-radar names so their
    # market / competition actually gets scored (otherwise the traffic-backed
    # lists crowd them out and every radar row looks identical).
    gr_cap = int(a.get("gameradar_seo_budget", 8))
    gr = [q for q in need if by_q[q][0] == "gameradar"][:gr_cap]
    others = [q for q in need if by_q[q][0] != "gameradar"]
    need = (gr + others)[: int(a.get("seo_budget", 20))]
    if need:
        s = cfg.get("seo") or {}
        seo_map = check_queries(
            None,
            need,
            throttle=float(s.get("serp_throttle", 3.0)),
            geo=str(s.get("geo", "US")),
            cache_path=state_path.parent / "seo_cache.json",
            cache_days=float(s.get("cache_days", 7)),
        )
        for q, res in seo_map.items():
            by_q[q][1]["seo"] = res

    smap = _sitemap_ease(out_dir)
    stars = {
        q: compute_stars(kind, it, weights, fallback=smap.get(q.lower(), 50.0))
        for q, (kind, it) in by_q.items()
    }

    # Text generation: only for the highest-star keywords not already cached.
    pending = [q for q in by_q if not _fresh(cache.get(q), ttl_days, now)]
    pending.sort(key=lambda q: stars[q], reverse=True)
    pending = pending[:budget]

    if pending:
        api_key, base, model = _resolve_llm()
        if not api_key:
            log.info("advice: no LLM key; stars only (%d pending)", len(pending))
        else:
            url = f"{base}/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            contexts = [_context(by_q[q][0], by_q[q][1]) for q in pending]
            batches = list(_chunks(contexts, batch))
            log.info("advice: %d keywords, %d batches via %s", len(pending), len(batches), model)
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(_fetch, url, headers, model, g): g for g in batches}
                for fut in as_completed(futs):
                    try:
                        res = fut.result()
                    except Exception as e:  # noqa: BLE001
                        log.warning("advice batch error: %s", e)
                        continue
                    for q, rec in res.items():
                        rec["ts"] = now
                        cache[q] = rec
            save_json(cache_path, _prune_cache(cache, cache_days))

    # Inject advice (stars always; text when available/fresh).
    n = 0
    for path, data, sel in groups:
        for kind, it in sel:
            q = it.get("name") or it.get("q")
            if not q:
                continue
            adv = {"stars": stars.get(q, 1)}
            adv["level"] = compute_level(kind, it, adv["stars"])
            adv["opportunities"] = content_opportunities(q, kind)
            seo = it.get("seo") or {}
            if seo.get("market"):
                adv["market"] = seo["market"]
            if isinstance(seo.get("nameRisk"), (int, float)):
                adv["nameRisk"] = seo["nameRisk"]
            rec = cache.get(q)
            if rec and _fresh(rec, ttl_days, now):
                for f in TEXT_FIELDS:
                    adv[f] = rec.get(f, "")
                for f in LIST_FIELDS:
                    adv[f] = rec.get(f, [])
            it["advice"] = adv
            n += 1
        save_json(path, data)
    log.info("advice: injected into %d keywords", n)
    return n
