import json

import collector.advice as A


def test_stars_high_opportunity():
    it = {"vol": 1000000, "growth": 1000, "new": True, "breakout": True, "seo": {"score": 95}}
    assert A.compute_stars("trend", it) == 5


def test_stars_low_opportunity():
    assert A.compute_stars("trend", {"vol": 500, "growth": 0, "seo": {"score": 5}}) == 1


def test_stars_competition_matters():
    base = {"vol": 500000, "growth": 500, "new": True}
    easy = A.compute_stars("trend", dict(base, seo={"score": 90}))
    hard = A.compute_stars("trend", dict(base, seo={"score": 10}))
    assert easy > hard


def test_stars_related_and_game():
    many = A.compute_stars("related", {"geos": ["US", "GB", "JP", "DE"], "seo": {"score": 80}})
    one = A.compute_stars("related", {"geos": ["US"]})
    assert many > one
    hot = A.compute_stars("game", {"score": 100, "breakout": True})
    cold = A.compute_stars("game", {"score": 10})
    assert hot > cold


def test_run_disabled_is_noop(tmp_path):
    assert A.run({"advice": {"enabled": False}}, tmp_path, tmp_path / "s.json") == 0


def test_advice_prune_cache_drops_old():
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    out = A._prune_cache({"o": {"ts": old}, "n": {"ts": now}}, 90)
    assert set(out) == {"n"}


def test_weights_normalized():
    w = A._weights({"demand": 2, "competition": 1, "window": 1})
    assert abs(sum(w.values()) - 1.0) < 1e-9
    assert w["demand"] == 0.5


def test_brand_penalty_lowers_stars():
    base = {"vol": 500000, "growth": 500, "new": True, "seo": {"score": 90}}
    plain = A.compute_stars("trend", dict(base))
    brand = A.compute_stars("trend", dict(base, q="samsung galaxy s27 fe"))
    assert brand < plain


def test_brand_penalty_direct():
    assert A._brand_penalty({"q": "google pixel buds pro 2"}) < 0
    assert A._brand_penalty({"q": "how to invest in index funds"}) == 0


def test_custom_weights_change_stars():
    it = {"vol": 500, "seo": {"score": 95}}  # low demand, great competition
    default = A.compute_stars("trend", it)
    comp_only = A.compute_stars("trend", it, {"competition": 1, "demand": 0, "window": 0})
    assert comp_only == 5
    assert comp_only >= default


def test_stars_competition_fallback_when_no_serp():
    base = {"vol": 500000, "growth": 500, "new": True}  # no seo.score
    default = A.compute_stars("trend", dict(base))                 # fallback 50
    easy = A.compute_stars("trend", dict(base), fallback=90)
    hard = A.compute_stars("trend", dict(base), fallback=10)
    assert easy > default > hard


def test_sitemap_ease_from_domain_count(tmp_path):
    (tmp_path / "sitemap.json").write_text(
        json.dumps({"items": [{"q": "Wordle Unlimited", "domain_count": 1},
                              {"q": "ffxiv", "domain_count": 5}]}),
        encoding="utf-8",
    )
    m = A._sitemap_ease(tmp_path)
    assert m["wordle unlimited"] == 75.0   # 90 - 1*15
    assert m["ffxiv"] == 15.0              # 90 - 5*15
    assert A._sitemap_ease(tmp_path / "none") == {}


def test_run_injects_stars_and_text(tmp_path, monkeypatch):
    (tmp_path / "related.json").write_text(
        json.dumps({"items": [{"q": "ai voice cloning free", "geos": ["US", "GB"]}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(A, "check_queries", lambda *a, **k: {})  # no network
    monkeypatch.setattr(A, "_resolve_llm", lambda: ("k", "http://x", "m"))
    monkeypatch.setattr(
        A, "_call_batch",
        lambda url, headers, model, items: {
            it["q"]: {
                "audience": "创作者", "directions": ["评测页"], "main_keyword": "best x",
                "monetize": ["affiliate"], "risk": "竞争", "evidence": "上升", "window": "2周",
            }
            for it in items
        },
    )

    state = tmp_path / "state.json"
    n = A.run({"advice": {"enabled": True}}, tmp_path, state)
    assert n == 1

    data = json.loads((tmp_path / "related.json").read_text(encoding="utf-8"))
    adv = data["items"][0]["advice"]
    assert adv["stars"] >= 1
    assert adv["audience"] == "创作者"

    cache = json.loads((tmp_path / "advice_cache.json").read_text(encoding="utf-8"))
    assert "ai voice cloning free" in cache


def test_content_opportunities_general_and_game():
    g = A.content_opportunities("best ai tool", "trend")
    assert g and g[0]["query"] == "best ai tool"
    prios = [e["priority"] for e in g]
    assert prios == sorted(prios, reverse=True)
    assert all("intent" in e for e in g)

    game = A.content_opportunities("control resonant", "game")
    assert any(e["query"] == "control resonant wiki" for e in game)
    assert any(e["query"] == "control resonant walkthrough" for e in game)
    assert A.content_opportunities("") == []


def test_compute_level():
    assert A.compute_level("trend", {}, 5) == "值得做"
    assert A.compute_level("trend", {}, 3) == "测试"
    assert A.compute_level("trend", {}, 2) == "观察"
    assert A.compute_level("trend", {}, 1) == "淘汰"
    # a strong fresh signal lifts a 3-star item (already 测试) and a 2-star one
    fresh = {"signal": {"classification": "breakout", "freshness": "new"}}
    assert A.compute_level("trend", fresh, 3) == "测试"
    # an occupied SERP can't be a top "值得做"
    assert A.compute_level("trend", {"seo": {"market": {"status": "occupied"}}}, 5) == "测试"
    assert A.compute_level("trend", {"seo": {"market": {"status": "greenfield"}}}, 5) == "值得做"


def test_run_injects_level_and_opportunities(tmp_path, monkeypatch):
    (tmp_path / "related.json").write_text(
        json.dumps({"items": [{"q": "ai voice cloning free", "geos": ["US", "GB"]}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(A, "check_queries", lambda *a, **k: {})
    monkeypatch.setattr(A, "_resolve_llm", lambda: (None, "", ""))
    A.run({"advice": {"enabled": True}}, tmp_path, tmp_path / "s.json")
    adv = json.loads((tmp_path / "related.json").read_text(encoding="utf-8"))["items"][0]["advice"]
    assert adv["level"] in ("值得做", "测试", "观察", "淘汰")
    assert adv["opportunities"][0]["query"] == "ai voice cloning free"
