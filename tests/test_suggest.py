import collector.suggest as G


def test_parse_suggest_chrome_json():
    text = '["base q",["base q how to","base q best","base q vs"],[],[],[]]'
    assert G.parse_suggest(text) == ["base q how to", "base q best", "base q vs"]


def test_parse_suggest_parity_style():
    text = '["base q",["s1","s2"]]'
    assert G.parse_suggest(text) == ["s1", "s2"]


def test_parse_suggest_garbage_returns_empty():
    assert G.parse_suggest(None) == []
    assert G.parse_suggest("<html>blocked</html>") == []
    assert G.parse_suggest("") == []


def test_expand_merges_and_dedupes(monkeypatch):
    def fake_fetch(session, q, hl="en", timeout=15):
        if "how to" in q:
            return [q, "shared suggestion"]
        return ["shared suggestion", "unique one"]

    monkeypatch.setattr(G, "fetch_suggest", fake_fetch)
    out = G.expand(None, ["kw"], per_q=1, throttle=0)
    # base results keep their order; modifier dupes and the bare query are dropped
    assert out["kw"] == ["shared suggestion", "unique one"]
    assert len(out["kw"]) <= G.MAX_PER_QUERY


def test_expand_respects_max_per_query(monkeypatch):
    monkeypatch.setattr(G, "fetch_suggest", lambda *a, **k: [f"sug{i}" for i in range(20)])
    out = G.expand(None, ["kw"], per_q=0, throttle=0, max_per_query=5)
    assert len(out["kw"]) == 5
