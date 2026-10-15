import pytest

import collector.gtrends_api as G
import collector.http as H
import collector.proxy as P


class _Resp:
    def __init__(self, status, url, text="ok", headers=None):
        self.status_code = status
        self.url = url
        self.text = text
        self.headers = headers or {"content-type": "text/html; charset=utf-8"}
        self.apparent_encoding = None


class _Session:
    def __init__(self, resp):
        self._resp = resp
        self.proxies = {}

    def get(self, url, timeout=None):
        return self._resp


def _reset(monkeypatch):
    G._blocked_at = None
    monkeypatch.setattr(P.ProxyPool, "_hit_switch", lambda self: None)
    P.configure([])


def test_is_google_url():
    assert H._is_google_url("https://trends.google.com/trending?geo=US") is True
    assert H._is_google_url("https://suggestqueries.google.com/complete/search") is True
    assert H._is_google_url("https://www.google.co.uk/x") is True
    assert H._is_google_url("https://itch.io/games") is False
    assert H._is_google_url("https://notgoogle.com/x") is False


def test_google_429_raises_and_flags_process(monkeypatch):
    _reset(monkeypatch)
    try:
        url = "https://trends.google.com/trending?geo=US"
        with pytest.raises(G.BlockedError):
            H.get_text(_Session(_Resp(429, url)), url)
        assert G.is_blocked_process() is True
    finally:
        G._blocked_at = None


def test_google_sorry_page_raises(monkeypatch):
    _reset(monkeypatch)
    try:
        url = "https://trends.google.com/trending?geo=US"
        sorry = "https://www.google.com/sorry/index?continue=x"
        with pytest.raises(G.BlockedError):
            H.get_text(_Session(_Resp(200, sorry)), url)
        assert G.is_blocked_process() is True
    finally:
        G._blocked_at = None


def test_non_google_429_is_not_a_block(monkeypatch):
    _reset(monkeypatch)
    url = "https://itch.io/games"
    assert H.get_text(_Session(_Resp(429, url)), url) is None
    assert G.is_blocked_process() is False


def test_build_session_pool_managed_flag(monkeypatch):
    _reset(monkeypatch)
    P.configure(["http://p1"])
    try:
        assert H.build_session()._trends_pool_managed is True
        assert H.build_session("http://explicit")._trends_pool_managed is False
    finally:
        P.configure([])


class _Probe:
    """Session stub that records which proxy was in effect during get()."""

    def __init__(self, managed):
        self.proxies = {"https": "http://old"}
        self._trends_pool_managed = managed
        self.seen = None

    def get(self, url, timeout=None):
        self.seen = self.proxies.get("https")
        return _Resp(200, url, text="[]")


def test_pool_managed_session_follows_rotation(monkeypatch):
    _reset(monkeypatch)
    P.configure(["http://p1", "http://p2"])
    try:
        P.rotate()  # pool current -> p2
        s = _Probe(managed=True)
        H.get_text(s, "https://trends.google.com/trending?geo=US")
        assert s.seen == "http://p2"
    finally:
        P.configure([])


def test_google_block_rotates_and_retries_then_succeeds(monkeypatch):
    _reset(monkeypatch)
    P.configure(["http://p1", "http://p2"])
    monkeypatch.setattr(H.time, "sleep", lambda *a: None)

    class _S:
        def __init__(self):
            self.proxies = {"https": "http://p1"}
            self._trends_pool_managed = True
            self.calls = 0

        def get(self, url, timeout=None):
            self.calls += 1
            if self.calls == 1:
                return _Resp(200, "https://www.google.com/sorry/index")
            return _Resp(200, url, text="[]")

    try:
        s = _S()
        out = H.get_text(s, "https://trends.google.com/trending?geo=US")
        assert out == "[]"
        assert s.calls == 2              # blocked once, retried once
        assert P.pool().rotations == 1
        assert G.is_blocked_process() is False  # recovered -> no block recorded
    finally:
        P.configure([])
        G._blocked_at = None


def test_google_block_without_rotation_marks_and_raises(monkeypatch):
    _reset(monkeypatch)  # empty pool -> no rotation available
    monkeypatch.setattr(H.time, "sleep", lambda *a: None)

    class _S:
        def __init__(self):
            self.proxies = {}
            self._trends_pool_managed = True

        def get(self, url, timeout=None):
            return _Resp(429, url)

    try:
        with pytest.raises(G.BlockedError):
            H.get_text(_S(), "https://trends.google.com/trending?geo=US")
        assert G.is_blocked_process() is True
    finally:
        G._blocked_at = None


def test_explicit_proxy_session_is_not_repointed(monkeypatch):
    _reset(monkeypatch)
    P.configure(["http://p1", "http://p2"])
    try:
        P.rotate()  # pool current -> p2
        s = _Probe(managed=False)
        H.get_text(s, "https://trends.google.com/trending?geo=US")
        assert s.seen == "http://old"  # explicit session left untouched
    finally:
        P.configure([])
