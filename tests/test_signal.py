import collector.signal as S


def test_series_stats_basic():
    st = S.series_stats([0, 0, 0, 10, 20, 30])
    assert st["points"] == 6
    assert st["peak"] == 30
    assert st["nonzero"] == 3
    assert st["recentAverage"] > st["earlierAverage"]
    assert st["coverage"] == 0.5
    assert st["slope"] > 0


def test_series_stats_empty():
    st = S.series_stats([])
    assert st["points"] == 0 and st["peak"] == 0 and st["momentum"] == 0.0


def test_guess_channel():
    assert S.guess_channel("skribbl.io") == "online"
    assert S.guess_channel("roblox") == "wiki"
    assert S.guess_channel("best ai tool") == "generic"


def test_build_profile_queries_and_anchor():
    p = S.build_profile("wordle", "wiki")
    assert p["anchor"] == "steam"
    assert p["queries"][0] == "wordle"
    assert "wordle wiki" in p["queries"]
    assert p["queries"][-1] == "steam"
    p2 = S.build_profile("foo.bar", "online", anchors={"online": "poki"})
    assert p2["anchor"] == "poki"


def test_isolated_spike():
    assert S.isolated_spike(S.series_stats([0, 0, 0, 0, 0, 100])) is True   # single point
    assert S.isolated_spike(S.series_stats([40, 45, 50, 55, 60, 50])) is False


def test_classify_demotes_spike_to_weak():
    stats = S.series_stats([0, 0, 0, 0, 0, 100])
    assert S.classify(9.0, stats, spike=True) == "weak"


def test_entity_conflict():
    assert S.entity_conflict("existing", 0.5, 0.01) is True
    assert S.entity_conflict("existing", 0.5, 0.4) is False
    assert S.entity_conflict("new", 0.5, 0.01) is False


def test_score_penalizes_existing_and_conflict():
    hot = {"classification": "breakout", "ratio": 3.0, "qualifiedRatio": 3.0, "coverage": 1.0, "spike": False}
    s_new = S._score(hot, hot, hot, "new", False)
    assert s_new > S._score(hot, hot, hot, "existing", False)
    assert s_new > S._score(hot, hot, hot, "new", True)


def test_analyze_builds_channel_profile(tmp_path):
    data = {
        "now 7-d": {"wordle": [10, 20, 30, 40, 50, 60], "wordle wiki": [5, 10, 15, 20, 25, 30], "steam": [10] * 6},
        "today 1-m": {"wordle": [5] * 30, "wordle wiki": [3] * 30, "steam": [10] * 30},
        "today 3-m": {"wordle": [4] * 93, "wordle wiki": [2] * 93, "steam": [10] * 93},
    }
    seen = {}

    class _C:
        def interest_multi(self, kws, geo="US", window="now 7-d"):
            seen["kws"] = kws
            return data.get(window)

    out = S.analyze(_C(), [{"q": "wordle", "channel": "wiki"}],
                    geo="US", throttle=0, cache_path=tmp_path / "c.json", budget=5)
    assert out["wordle"]["channel"] == "wiki"
    assert out["wordle"]["anchor"] == "steam"
    assert "wordle wiki" in seen["kws"] and "steam" in seen["kws"]
    assert out["wordle"]["qualifiedRatio"] > 0
    assert "score" in out["wordle"]


def test_ratio_and_classify_breakout():
    term = [0, 0, 0, 40, 80, 100]
    base = [10, 10, 10, 10, 10, 10]
    r = S.ratio(term, base)
    stats = S.series_stats(term)
    assert r > 5
    assert S.classify(r, stats, breakout_ratio=5.0) == "breakout"


def test_classify_none_when_flat_zero():
    assert S.classify(0, S.series_stats([0, 0, 0])) == "none"


def test_ratio_falls_back_to_peak_when_baseline_flat_zero():
    # baseline recentAverage 0 but has a peak -> use peak ratio
    assert S.ratio([10, 20, 30], [0, 0, 20]) > 0


def test_freshness_new_vs_existing():
    new = S.series_stats([0] * 6 + [10, 20, 30])
    existing = S.series_stats([20] * 9)
    assert S.freshness(new) == "new"
    assert S.freshness(existing) == "existing"
    assert S.freshness({}) == "unknown"


class _FakeClient:
    def __init__(self, data):
        self.data = data
        self.calls = 0

    def interest_multi(self, kws, geo="US", window="now 7-d"):
        self.calls += 1
        return self.data.get(window)


DATA = {
    "now 7-d": {"term": [0, 0, 10, 20, 40, 80], "gpts": [10, 10, 10, 10, 10, 10]},
    "today 1-m": {"term": [0] * 20 + [20] * 10, "gpts": [10] * 30},
    "today 3-m": {"term": [0] * 60 + [20] * 33, "gpts": [10] * 93},
}


def test_analyze_with_fake_client_and_cache(tmp_path):
    cache = tmp_path / "signal_cache.json"
    client = _FakeClient(DATA)
    out = S.analyze(client, ["term"], geo="US", throttle=0, cache_path=cache, budget=5)
    assert "term" in out
    sig = out["term"]
    assert sig["classification"] == "breakout"
    assert sig["label"] == "爆发"
    assert sig["freshness"] == "new"
    assert sig["ratio"] > 5
    assert set(sig["windows"]) == {"7d", "30d", "90d"}
    assert cache.exists()
    assert client.calls == 3  # one fetch per window

    # Second run is served from cache (no new network calls).
    out2 = S.analyze(_FakeClient(DATA), ["term"], geo="US", throttle=0, cache_path=cache, budget=5)
    assert client.calls == 3
    assert out2["term"]["classification"] == "breakout"


def test_analyze_respects_budget(tmp_path):
    cache = tmp_path / "signal_cache.json"
    client = _FakeClient(DATA)
    out = S.analyze(
        client, ["a", "b", "c"], geo="US", throttle=0, cache_path=cache, budget=1,
    )
    # budget caps uncached fetches at 1 term (3 windows) -> only "a" gets a signal
    assert set(out) == {"a"}
    assert client.calls == 3
