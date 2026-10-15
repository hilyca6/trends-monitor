import pytest

import collector.gtrends_api as G


class _Resp:
    def __init__(self, status, url="https://trends.google.com/trends/api/explore"):
        self.status_code = status
        self.url = url
        self.text = ""


def test_is_blocked_on_429():
    assert G._is_blocked(_Resp(429)) is True
    assert G._is_blocked(_Resp(200)) is False
    assert G._is_blocked(None) is False


def test_is_blocked_on_sorry_page():
    r = _Resp(200, "https://www.google.com/sorry/index?continue=https://trends.google.com")
    assert G._is_blocked(r) is True


def test_ensure_ok_raises_on_block_and_passes_ok():
    with pytest.raises(G.BlockedError):
        G._ensure_ok(_Resp(429))
    with pytest.raises(G.BlockedError):
        G._ensure_ok(_Resp(200, "https://www.google.com/sorry/index"))
    G._ensure_ok(_Resp(200))  # no raise


def test_last_blocked_records_first_block():
    G._blocked_at = None
    assert G.last_blocked() is None
    with pytest.raises(G.BlockedError):
        G._ensure_ok(_Resp(429))
    assert G.last_blocked() is not None
    G._blocked_at = None


def test_mark_blocked_is_idempotent_and_flags_process():
    G._blocked_at = None
    assert G.is_blocked_process() is False
    G.mark_blocked()
    first = G.last_blocked()
    assert G.is_blocked_process() is True
    G.mark_blocked()  # second call must keep the *first* timestamp
    assert G.last_blocked() == first
    G._blocked_at = None


def test_is_blocked_response_public_wrapper():
    assert G.is_blocked_response(_Resp(429)) is True
    assert G.is_blocked_response(_Resp(200, "https://www.google.com/sorry/index")) is True
    assert G.is_blocked_response(None) is False


def test_kw_list_normalizes():
    assert G.GoogleTrendsClient._kw_list("a") == ["a"]
    assert G.GoogleTrendsClient._kw_list(["a", "b"]) == ["a", "b"]


class _TextResp:
    def __init__(self, text, url=G.EXPLORE_API, status=200):
        self.status_code = status
        self.url = url
        self.text = text


def test_explore_builds_multi_comparison_item():
    import json

    captured = {}

    class _S:
        def get(self, url, params=None, timeout=None):
            captured.setdefault("urls", []).append(url)
            if url == G.EXPLORE_API:
                captured["req"] = params["req"]
                payload = {"widgets": [{"id": "TIMESERIES", "token": "t", "request": {}}]}
                return _TextResp(")]}'\n" + json.dumps(payload))
            return _TextResp("", url=G.EXPLORE_PAGE)

    c = G.GoogleTrendsClient(_S(), throttle=0)
    c._warmed = True  # skip network warmup
    widgets = c.explore(["foo bar", "gpts"], geo="US")
    assert widgets[0]["id"] == "TIMESERIES"
    req = json.loads(captured["req"])
    assert [ci["keyword"] for ci in req["comparisonItem"]] == ["foo bar", "gpts"]


def test_interest_multi_aligns_values(monkeypatch):
    c = G.GoogleTrendsClient(object(), throttle=0)
    monkeypatch.setattr(
        c, "explore",
        lambda kws, geo="US", window="now 7-d", retries=4: [
            {"id": "TIMESERIES", "token": "t", "request": {}}
        ],
    )
    timeline = {"default": {"timelineData": [
        {"value": [10, 50]}, {"value": [20, 40]}, {"value": [30, 0]},
    ]}}
    monkeypatch.setattr(c, "widget_data", lambda widget, endpoint: timeline)
    out = c.interest_multi(["foo", "gpts"], geo="US")
    assert out == {"foo": [10, 20, 30], "gpts": [50, 40, 0]}


def test_interest_multi_returns_none_without_widget(monkeypatch):
    c = G.GoogleTrendsClient(object(), throttle=0)
    monkeypatch.setattr(c, "explore", lambda *a, **k: [])
    assert c.interest_multi(["a", "b"]) is None


def test_explore_rotates_and_retries_on_block(monkeypatch):
    import json

    import collector.proxy as P

    P.configure(["http://p1", "http://p2"])

    class _S:
        def __init__(self):
            self.proxies = {}
            self.calls = 0

        def get(self, url, params=None, timeout=None):
            if url == G.EXPLORE_API:
                self.calls += 1
                if self.calls == 1:
                    return _TextResp("", url="https://www.google.com/sorry/index", status=429)
                payload = {"widgets": [{"id": "TIMESERIES", "token": "t", "request": {}}]}
                return _TextResp(")]}'\n" + json.dumps(payload))
            return _TextResp("", url=G.EXPLORE_PAGE)

    try:
        s = _S()
        c = G.GoogleTrendsClient(s, throttle=0)
        c._warmed = True  # skip warmup
        monkeypatch.setattr(G.time, "sleep", lambda *a: None)
        widgets = c.explore(["foo bar", "gpts"], geo="US")
        assert widgets and widgets[0]["id"] == "TIMESERIES"
        assert s.calls == 2
        assert P.pool().rotations >= 1
    finally:
        P.configure([])
