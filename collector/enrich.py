"""Enrichment: rule-based noise labels + optional LLM (zh translation / noise).

The rule pass always runs (free, offline). The LLM pass is optional and only
activates when a key is configured:

    DEEPSEEK_API_KEY=sk-...        # preferred (base defaults to api.deepseek.com)
    DEEPSEEK_MODEL=deepseek-chat

  or any OpenAI-compatible endpoint:
    OPENAI_API_KEY=sk-...
    OPENAI_BASE_URL=https://api.openai.com/v1
    OPENAI_MODEL=gpt-4o-mini

LLM results are cached per query (state/llm_cache.json) so hourly re-runs and
duplicate queries across geos only cost one call each.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import NOISE_RULES

log = logging.getLogger("trends.enrich")


def rule_noise(q: str, cats: list[int]) -> str:
    for label, fn in NOISE_RULES:
        try:
            if fn(q, cats):
                return label
        except Exception:  # noqa: BLE001
            continue
    return ""


def apply_rules(items: list[dict]) -> list[dict]:
    for it in items:
        it.setdefault("zh", "")
        it["noise"] = rule_noise(it["q"], it.get("cats") or [])
    return items


SYSTEM_PROMPT = (
    "You label Google search-trending terms for an SEO analyst. "
    "For each input query return: "
    "zh = a concise Simplified-Chinese translation with brief context in "
    "parentheses when the term is a person/event/brand (e.g. "
    "'susan dell' -> '苏珊·戴尔(戴尔CEO妻子)'); "
    "noise = one of ['人物','比赛','天气','事故','彩票','导航','大词',''] "
    "(empty string when the term is a potentially useful evergreen topic). "
    "cat = one of ['gaming','ai','tools','shopping','finance','health',"
    "'lifestyle','education','entertainment','sports','travel','food','other']; "
    "subcat = a short snake_case subcategory (e.g. 'browser_game','checker',"
    "'calculator','puzzle'); "
    "entity = one of ['game','tool','product','person','event','brand','place',"
    "'topic','other']; "
    "trigger = the likely reason it is rising, in short Simplified Chinese "
    "(e.g. '新发布','病毒传播','季节','新闻事件'), or '' if unclear. "
    "Respond ONLY with a JSON object of the form "
    "{\"results\":[{\"q\":\"...\",\"zh\":\"...\",\"noise\":\"...\",\"cat\":\"...\","
    "\"subcat\":\"...\",\"entity\":\"...\",\"trigger\":\"...\"}]}, "
    "in the same order as the input queries."
)

# Extra classification fields (P5), filled by the LLM when configured.
EXTRA_FIELDS = ("cat", "subcat", "entity", "trigger")


def _clean_row(row: dict) -> dict:
    """Keep the fields we care about, bounded, from one LLM result row."""
    rec = {"zh": str(row.get("zh", "") or ""), "noise": str(row.get("noise", "") or "")}
    for f in EXTRA_FIELDS:
        v = row.get(f)
        if v:
            rec[f] = str(v)[:40]
    return rec


def _apply_cached(it: dict, c: dict) -> None:
    """Copy cached enrichment (zh/noise + classification) onto an item."""
    if not c:
        return
    if c.get("zh"):
        it["zh"] = c["zh"]
    if c.get("noise"):
        it["noise"] = c["noise"]
    for f in EXTRA_FIELDS:
        if c.get(f):
            it[f] = c[f]


def _resolve_llm():
    """Resolve (api_key, base_url, model). DeepSeek takes precedence."""
    deepseek_key = os.getenv("DEEPSEEK_API_KEY")
    if deepseek_key:
        base = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        model = os.getenv("DEEPSEEK_MODEL", os.getenv("OPENAI_MODEL", "deepseek-chat"))
        return deepseek_key, base.rstrip("/"), model
    api_key = os.getenv("OPENAI_API_KEY")
    base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    return api_key, base.rstrip("/"), model


def _load_cache(path: Path | None) -> dict:
    if path and path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
    return {}


def _save_cache(path: Path | None, cache: dict) -> None:
    if not path:
        return
    days = int(os.getenv("LLM_CACHE_DAYS", "90"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_prune_cache(cache, days), ensure_ascii=False), encoding="utf-8")


def _prune_cache(cache: dict, days: int) -> dict:
    """Drop cache entries not used within `days` (bounds state/llm_cache.json).

    Legacy entries without a timestamp are grandfathered (stamped with now) so
    nothing is mass-dropped; they then age out normally.
    """
    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - days * 86400
    out: dict = {}
    for q, rec in cache.items():
        if not isinstance(rec, dict):
            continue
        ts = rec.get("ts")
        if not ts:
            rec["ts"] = now.isoformat()
            out[q] = rec
            continue
        try:
            fresh = datetime.fromisoformat(ts).timestamp() >= cutoff
        except (ValueError, TypeError):
            fresh = True
        if fresh:
            out[q] = rec
    return out


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


class LLMError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message[:200]}")
        self.status = status


def _call_batch(url, headers, model, queries) -> dict:
    payload = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({"queries": queries}, ensure_ascii=False)},
        ],
    }
    r = requests.post(url, headers=headers, json=payload, timeout=120)
    if r.status_code != 200:
        raise LLMError(r.status_code, r.text)
    content = r.json()["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    rows = parsed.get("results", parsed) if isinstance(parsed, dict) else parsed
    out = {}
    for row in rows:
        if isinstance(row, dict) and row.get("q"):
            out[row["q"]] = _clean_row(row)
    return out


_TRANSIENT_STATUS = {429, 500, 502, 503, 504}


def _fetch_resilient(url, headers, model, queries, depth: int = 0, attempts: int = 3) -> dict:
    """Fetch a batch, retrying transient errors (429/5xx) with backoff.

    HTTP 400 (content filter) splits the batch and retries halves; a single
    query that still 400s is cached as empty so it isn't retried forever.
    Persistent/other errors return {} (retried on the next run).
    """
    for attempt in range(1, attempts + 1):
        try:
            return _call_batch(url, headers, model, queries)
        except LLMError as e:
            if e.status == 400 and len(queries) > 1 and depth < 8:
                mid = len(queries) // 2
                out = _fetch_resilient(url, headers, model, queries[:mid], depth + 1)
                out.update(_fetch_resilient(url, headers, model, queries[mid:], depth + 1))
                return out
            if e.status == 400:
                log.warning("LLM rejected %d query(s), skipping: %s", len(queries), queries[:3])
                return {q: {"zh": "", "noise": ""} for q in queries}
            if e.status in _TRANSIENT_STATUS and attempt < attempts:
                time.sleep(1.5 * attempt + random.random())
                continue
            log.warning("LLM batch failed: %s", e)
            return {}
        except requests.RequestException as e:  # noqa: BLE001
            if attempt < attempts:
                time.sleep(1.5 * attempt + random.random())
                continue
            log.warning("LLM request failed: %s", e)
            return {}
    return {}


def llm_enrich(
    items: list[dict],
    *,
    batch: int = int(os.getenv("LLM_BATCH", "50")),
    workers: int = int(os.getenv("LLM_WORKERS", "8")),
    cache_path: Path | None = None,
    limit: int | None = None,
) -> None:
    """Mutate items in place with zh/noise from an LLM, using a query cache.

    Cached results are always applied (no key needed). Only uncached queries
    hit the network, and only when a key is configured. Items the rule pass
    already flagged as noise are skipped (their zh stays empty) — they are the
    bulk and the LLM adds little for them. `limit` caps how many queries are
    sent per run (a random sample, so coverage still grows across runs).
    """
    cache = _load_cache(cache_path)
    now = datetime.now(timezone.utc).isoformat()
    pending: dict[str, str] = {}
    for it in items:
        c = cache.get(it["q"])
        if c:
            _apply_cached(it, c)
            c["ts"] = now  # touch so still-active queries keep their cache alive
        elif not it.get("noise") and it["q"] not in pending:
            pending[it["q"]] = it["q"]

    if not pending:
        _save_cache(cache_path, cache)  # persist any ts touches from cache hits
        return

    api_key, base, model = _resolve_llm()
    if not api_key:
        log.info("no LLM key set; applied cache, %d queries pending", len(pending))
        return

    log.info(
        "LLM enrich: %d items, %d unique, %d cached, %d pending",
        len(items), len({it["q"] for it in items}), len(cache), len(pending),
    )

    queries = list(pending)
    if limit and len(queries) > limit:
        log.info("LLM limit: sampling %d of %d pending queries", limit, len(queries))
        queries = random.sample(queries, limit)
    if queries:
        url = f"{base}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        groups = list(_chunks(queries, batch))
        log.info("LLM via %s model=%s: %d batches, %d workers", base, model, len(groups), workers)
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futures = {ex.submit(_fetch_resilient, url, headers, model, g): g for g in groups}
            for fut in as_completed(futures):
                try:
                    res = fut.result()
                    for rec in res.values():
                        if isinstance(rec, dict):
                            rec["ts"] = now
                    cache.update(res)
                except Exception as e:  # noqa: BLE001
                    log.warning("LLM batch failed: %s", e)
                done += 1
                if done % 5 == 0:  # persist partial progress so a kill isn't fatal
                    _save_cache(cache_path, cache)
                if done % 10 == 0 or done == len(groups):
                    log.info("LLM progress %d/%d batches (cache %d)", done, len(groups), len(cache))
        _save_cache(cache_path, cache)

    for it in items:
        c = cache.get(it["q"])
        if c:
            _apply_cached(it, c)


def enrich(
    items: list[dict],
    use_llm: bool = False,
    cache_path: Path | None = None,
    llm_limit: int | None = None,
) -> list[dict]:
    apply_rules(items)
    if use_llm:
        llm_enrich(items, cache_path=cache_path, limit=llm_limit)
    return items
