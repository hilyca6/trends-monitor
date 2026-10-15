import os

import collector.proxy as P


def test_pool_rotates_cycles():
    pool = P.ProxyPool(["a", "b", "c"])
    assert pool.current() == "a"
    assert pool.rotate() == "b"
    assert pool.rotate() == "c"
    assert pool.rotate() == "a"
    assert pool.rotations == 3


def test_notify_blocked_requires_rotation_ability(monkeypatch):
    monkeypatch.setattr(P.ProxyPool, "_hit_switch", lambda self: True)  # no network
    assert P.ProxyPool(["a"]).notify_blocked() is False            # single, no switch
    assert P.ProxyPool(["a", "b"]).notify_blocked() is True
    assert P.ProxyPool([], switch_url="http://x").notify_blocked() is True
    assert P.ProxyPool(["a"], rotate_on_block=False).notify_blocked() is False


def test_dead_switch_url_without_group_cannot_rotate():
    # url present but no selector group -> the switch can't do anything, so it
    # must NOT claim a rotation happened (this was the real-world bug).
    pool = P.ProxyPool(["http://only"], switch={"url": "http://127.0.0.1:9097", "group": "", "nodes": []})
    assert pool.can_rotate() is False
    assert pool.notify_blocked() is False
    assert pool.rotations == 0


def test_failed_clash_put_does_not_count_rotation(monkeypatch):
    monkeypatch.setattr(P.random, "choice", lambda seq: seq[0])
    monkeypatch.setattr(P.requests, "put",
                        lambda *a, **k: type("R", (), {"status_code": 401})())
    pool = P.ProxyPool([], switch={"url": "http://x", "group": "G", "nodes": ["N1", "N2"]})
    assert pool.notify_blocked() is False
    assert pool.rotations == 0


def test_configure_sets_env_and_singleton(monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    P.configure(["http://p1", "http://p2"])
    assert P.current() == "http://p1"
    assert os.environ["HTTPS_PROXY"] == "http://p1"
    P.rotate()
    assert os.environ["HTTPS_PROXY"] == "http://p2"


def test_configure_accepts_string_and_comma_safe():
    P.configure("http://solo")
    assert P.pool().size == 1 and P.current() == "http://solo"
    P.configure([])  # reset to empty for other tests


def test_clash_switch_fetches_nodes_and_puts_non_current(monkeypatch):
    calls = []

    def fake_get(url, headers=None, timeout=None, proxies=None):
        calls.append(("get", url))
        # /proxies map: group lists DIRECT, leaf A, leaf B and a sub-group "Sub".
        return type("R", (), {"json": lambda self: {"proxies": {
            "PROXY": {"type": "Selector", "now": "A", "all": ["DIRECT", "A", "B", "Sub"]},
            "A": {"type": "Vless"},
            "B": {"type": "Vless"},
            "Sub": {"type": "URLTest", "all": ["X"]},
            "DIRECT": {"type": "Direct"},
        }}})()

    def fake_put(url, headers=None, json=None, timeout=None, proxies=None):
        calls.append(("put", url, json))
        return type("R", (), {})()

    monkeypatch.setattr(P.requests, "get", fake_get)
    monkeypatch.setattr(P.requests, "put", fake_put)
    monkeypatch.setattr(P.random, "choice", lambda seq: seq[0])

    pool = P.ProxyPool([], switch={"url": "http://127.0.0.1:9097", "group": "PROXY"})
    assert pool.can_rotate() is True
    assert pool.notify_blocked() is True
    puts = [c for c in calls if c[0] == "put"]
    assert puts and puts[0][2]["name"] == "B"  # A is current -> picks B; "Sub"/DIRECT skipped


def test_clash_switch_uses_configured_nodes(monkeypatch):
    puts = []
    monkeypatch.setattr(P.requests, "put",
                        lambda url, headers=None, json=None, timeout=None, proxies=None:
                        puts.append(json) or type("R", (), {})())
    monkeypatch.setattr(P.random, "choice", lambda seq: seq[-1])
    pool = P.ProxyPool([], switch={"url": "http://x", "group": "G", "nodes": ["N1", "N2"]})
    pool.rotate()
    assert puts[0]["name"] == "N2"


def test_single_proxy_without_switch_cannot_rotate():
    assert P.ProxyPool(["http://only"]).can_rotate() is False


def test_apply_to_session_sets_current_proxy():
    P.configure(["http://p1", "http://p2"])

    class _S:
        proxies = {}

    s = _S()
    assert P.apply_to_session(s) == "http://p1"
    assert s.proxies["https"] == "http://p1"
    P.rotate()  # pool advances to p2
    P.apply_to_session(s)
    assert s.proxies["https"] == "http://p2"
    P.configure([])
