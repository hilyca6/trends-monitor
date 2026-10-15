import json

import collector.related as R

PAYLOAD = {
    "default": {
        "rankedList": [
            {
                "rankedKeyword": [
                    {"query": "al nassr fixtures", "value": 100},
                    {"query": "al nassr vs", "value": 80},
                ]
            },
            {
                "rankedKeyword": [
                    {"query": "al nassr new signing", "value": 5000},
                    {"query": "al nassr breakout", "value": 100, "isBreakout": True},
                    {"query": "", "value": 1},
                ]
            },
        ]
    }
}


def test_parse_related_top_and_rising():
    out = R.parse_related(PAYLOAD)
    assert [e["query"] for e in out["top"]] == ["al nassr fixtures", "al nassr vs"]
    assert [e["query"] for e in out["rising"]] == ["al nassr new signing", "al nassr breakout"]
    assert out["rising"][1]["is_breakout"] is True


def test_parse_related_garbage_returns_empty():
    assert R.parse_related(None) == {"top": [], "rising": []}
    assert R.parse_related({"default": {}}) == {"top": [], "rising": []}


def test_expand_caches_and_returns_strings(tmp_path, monkeypatch):
    cache = tmp_path / "related_cache.json"
    calls = {"n": 0}

    def fake_fetch(client, kw, geo, window="now 7-d"):
        calls["n"] += 1
        return R.parse_related(PAYLOAD)

    monkeypatch.setattr(R, "fetch_related", fake_fetch)
    monkeypatch.setattr(R, "GoogleTrendsClient", lambda *a, **k: object())

    out = R.expand(None, ["al nassr"], geo="US", throttle=0, cache_path=cache)
    assert out["al nassr"]["rising"] == ["al nassr new signing", "al nassr breakout"]
    assert out["al nassr"]["top"] == ["al nassr fixtures", "al nassr vs"]
    assert calls["n"] == 1

    out2 = R.expand(None, ["al nassr"], geo="US", throttle=0, cache_path=cache)
    assert calls["n"] == 1  # served from cache
    assert out2 == out
    data = json.loads(cache.read_text(encoding="utf-8"))
    assert set(data) == {"US|al nassr"}


def test_expand_does_not_cache_failures(tmp_path, monkeypatch):
    cache = tmp_path / "related_cache.json"
    monkeypatch.setattr(R, "fetch_related", lambda *a, **k: None)
    monkeypatch.setattr(R, "GoogleTrendsClient", lambda *a, **k: object())
    assert R.expand(None, ["x"], throttle=0, cache_path=cache) == {}
    assert not cache.exists()


def test_rising_strings():
    assert R.rising_strings({"rising": ["a", "b"]}) == ["a", "b"]
    assert R.rising_strings(None) == []


def test_clean_query_strips_leading_verb():
    assert R.clean_query("play wordle online") == "wordle online"
    assert R.clean_query("  download   skibidi  ") == "skibidi"


def test_is_useful_rising_filters_junk():
    assert R.is_useful_rising("how to lower cortisol naturally") is True
    assert R.is_useful_rising("best mutual funds for 2026") is True
    assert R.is_useful_rising("free online games") is False       # all generic
    assert R.is_useful_rising("download apk") is False            # piracy
    assert R.is_useful_rising("cracked pc") is False
    assert R.is_useful_rising("x") is False                       # too short
    assert R.is_useful_rising("a b c d e f g h i j") is False     # > 8 words


def test_expand_uses_provided_client(monkeypatch, tmp_path):
    sent = object()
    seen = {}

    def fake_fetch(client, kw, geo, window="now 7-d"):
        seen["client"] = client
        return {"top": [], "rising": [{"query": "x", "value": 1, "is_breakout": False}]}

    monkeypatch.setattr(R, "fetch_related", fake_fetch)
    out = R.expand(None, ["kw"], throttle=0, cache_path=tmp_path / "c.json", client=sent)
    assert seen["client"] is sent
    assert out["kw"]["rising"] == ["x"]
