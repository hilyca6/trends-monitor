import collector.push as P


def test_dingtalk_sign_is_deterministic_and_time_sensitive():
    a = P.dingtalk_sign("SECRET", "1700000000000")
    b = P.dingtalk_sign("SECRET", "1700000000000")
    c = P.dingtalk_sign("SECRET", "1700000000001")
    assert a == b
    assert a != c
    assert isinstance(a, str) and len(a) > 0


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_post_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_post(url, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise P.requests.ConnectionError("boom")
        return _Resp(200, {"errcode": 0})

    monkeypatch.setattr(P.requests, "post", fake_post)
    monkeypatch.setattr(P.time, "sleep", lambda *_: None)
    assert P._post("http://x", {"a": 1}) is True
    assert calls["n"] == 3


def test_post_gives_up_after_attempts(monkeypatch):
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        raise P.requests.ConnectionError("down")

    monkeypatch.setattr(P.requests, "post", fake_post)
    monkeypatch.setattr(P.time, "sleep", lambda *_: None)
    assert P._post("http://x", {}, attempts=2, backoff=0) is False
    assert calls["n"] == 2


def test_post_detects_dingtalk_errcode(monkeypatch):
    monkeypatch.setattr(P.requests, "post", lambda *a, **k: _Resp(200, {"errcode": 310000, "errmsg": "sign"}))
    monkeypatch.setattr(P.time, "sleep", lambda *_: None)
    assert P._post("http://x", {}, attempts=1) is False


def test_post_detects_telegram_not_ok(monkeypatch):
    monkeypatch.setattr(P.requests, "post", lambda *a, **k: _Resp(200, {"ok": False, "description": "bad"}))
    monkeypatch.setattr(P.time, "sleep", lambda *_: None)
    assert P._post("http://x", {}, attempts=1) is False


def test_post_success_on_http_200(monkeypatch):
    monkeypatch.setattr(P.requests, "post", lambda *a, **k: _Resp(200, {"errcode": 0}))
    assert P._post("http://x", {}) is True


def test_send_text_disabled_is_noop(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(P, "_post", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or True)
    assert P.send_text({"push": {"enabled": False, "dingtalk_webhook": "u"}}, "hi") is False
    assert called["n"] == 0
