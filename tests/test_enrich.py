import json

import requests

import collector.enrich as E
from collector.enrich import LLMError, _fetch_resilient


def test_fetch_resilient_splits_on_400(monkeypatch):
    def fake(url, headers, model, queries):
        if len(queries) > 2:
            raise LLMError(400, "bad request")
        return {q: {"zh": "x", "noise": ""} for q in queries}

    monkeypatch.setattr(E, "_call_batch", fake)
    out = _fetch_resilient("u", {}, "m", ["a", "b", "c", "d", "e"])
    assert set(out) == {"a", "b", "c", "d", "e"}
    assert all(v["zh"] == "x" for v in out.values())


def test_fetch_resilient_single_400_cached_empty(monkeypatch):
    monkeypatch.setattr(E, "_call_batch", lambda *a, **k: (_ for _ in ()).throw(LLMError(400, "nope")))
    out = _fetch_resilient("u", {}, "m", ["badterm"])
    assert out == {"badterm": {"zh": "", "noise": ""}}


def test_fetch_resilient_transient_error_returns_empty(monkeypatch):
    def fake(*a, **k):
        raise requests.RequestException("timeout")

    monkeypatch.setattr(E, "_call_batch", fake)
    assert _fetch_resilient("u", {}, "m", ["a", "b"]) == {}


def test_llm_enrich_skips_rule_noise(monkeypatch, tmp_path):
    items = [
        {"q": "nba scores", "cats": [], "noise": "比赛", "zh": ""},
        {"q": "sleep gummies", "cats": [], "noise": "", "zh": ""},
    ]
    sent = []
    monkeypatch.setattr(E, "_resolve_llm", lambda: ("k", "http://x", "m"))
    monkeypatch.setattr(
        E, "_fetch_resilient",
        lambda url, headers, model, qs, depth=0: (sent.extend(qs) or {}),
    )
    E.llm_enrich(items, cache_path=tmp_path / "c.json")
    assert sent == ["sleep gummies"]


def test_llm_enrich_respects_limit(monkeypatch, tmp_path):
    items = [{"q": f"q{i} words", "cats": [], "noise": "", "zh": ""} for i in range(10)]
    sent = []
    monkeypatch.setattr(E, "_resolve_llm", lambda: ("k", "http://x", "m"))
    monkeypatch.setattr(
        E, "_fetch_resilient",
        lambda url, headers, model, qs, depth=0: (sent.extend(qs) or {}),
    )
    E.llm_enrich(items, cache_path=tmp_path / "c.json", limit=3)
    assert len(sent) == 3


def test_prune_cache_drops_old_and_grandfathers_legacy():
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    cache = {
        "legacy": {"zh": "x", "noise": ""},           # no ts -> grandfathered
        "old": {"zh": "x", "noise": "", "ts": old},    # dropped
        "fresh": {"zh": "x", "noise": "", "ts": now},  # kept
    }
    out = E._prune_cache(cache, days=90)
    assert set(out) == {"legacy", "fresh"}
    assert "ts" in out["legacy"]


def test_llm_enrich_persists_touches_when_all_cached(tmp_path):
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps({"a": {"zh": "A", "noise": ""}}), encoding="utf-8")
    items = [{"q": "a", "cats": [], "noise": "", "zh": ""}]
    E.llm_enrich(items, cache_path=cache)  # nothing pending -> no LLM
    assert items[0]["zh"] == "A"
    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert "ts" in saved["a"]  # cache re-saved even with no pending work


def test_clean_row_keeps_extra_fields_and_defaults():
    assert E._clean_row({"q": "x", "zh": "译", "noise": ""}) == {"zh": "译", "noise": ""}
    rec = E._clean_row({"zh": "译", "cat": "ai", "subcat": "checker", "entity": "tool"})
    assert rec["cat"] == "ai" and rec["subcat"] == "checker" and rec["entity"] == "tool"
    assert rec["noise"] == ""


def test_apply_cached_sets_classification():
    it = {"zh": "", "noise": ""}
    E._apply_cached(it, {"zh": "译", "cat": "gaming", "trigger": "新发布"})
    assert it["zh"] == "译" and it["cat"] == "gaming" and it["trigger"] == "新发布"
    E._apply_cached(it, None)  # no-op
    assert it["cat"] == "gaming"


def test_llm_enrich_applies_classification_fields(monkeypatch, tmp_path):
    items = [{"q": "sleep gummies", "cats": [], "noise": "", "zh": ""}]
    monkeypatch.setattr(E, "_resolve_llm", lambda: ("k", "http://x", "m"))
    monkeypatch.setattr(
        E, "_fetch_resilient",
        lambda url, headers, model, qs, depth=0: {
            q: {"zh": "助眠软糖", "noise": "", "cat": "health", "subcat": "supplement",
                "entity": "product", "trigger": "季节"}
            for q in qs
        },
    )
    E.llm_enrich(items, cache_path=tmp_path / "c.json")
    assert items[0]["cat"] == "health"
    assert items[0]["subcat"] == "supplement"
    assert items[0]["entity"] == "product"
    assert items[0]["trigger"] == "季节"
