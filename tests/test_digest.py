import importlib.util
import json
from pathlib import Path

import collector.config as config
from collector.store import now_iso

ROOT = Path(__file__).resolve().parent.parent


def _run_all():
    spec = importlib.util.spec_from_file_location("run_all_mod", ROOT / "scripts" / "run_all.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _write_trends(tmp_path, rows):
    (tmp_path / "trends.json").write_text(json.dumps({"items": {"US": rows}}), encoding="utf-8")


def test_build_digest_orders_by_star(tmp_path, monkeypatch):
    m = _run_all()
    monkeypatch.setattr(config, "WEB_DATA_DIR", tmp_path)
    _write_trends(tmp_path, [
        {"q": "low", "new": True, "noise": "", "advice": {"stars": 2}},
        {"q": "high", "new": True, "noise": "", "advice": {"stars": 5}},
    ])
    text, keys = m.build_digest(tmp_path / "state.json", top_trends=1)
    assert "high" in text
    assert "low" not in text
    assert len(keys) == 1


def test_build_digest_dedup(tmp_path, monkeypatch):
    m = _run_all()
    monkeypatch.setattr(config, "WEB_DATA_DIR", tmp_path)
    _write_trends(tmp_path, [
        {"q": "ai website builder", "new": True, "noise": "", "advice": {"stars": 4, "main_keyword": "best ai"}},
    ])
    state = tmp_path / "state.json"

    text1, keys1 = m.build_digest(state)
    assert "ai website builder" in text1
    assert keys1 == ["trend|ai website builder"]

    m._mark_pushed(tmp_path / "pushed.json", keys1, now_iso())
    text2, keys2 = m.build_digest(state)
    assert text2 == ""
    assert keys2 == []


def test_in_quiet_bounds():
    m = _run_all()
    assert m._in_quiet({"push": {}}) is False
    assert m._in_quiet({"push": {"quiet_hours": "0-24"}}) is True
    assert m._in_quiet({"push": {"quiet_hours": "bogus"}}) is False


def test_hours_parse():
    m = _run_all()
    assert m._hours(None) is None
    assert m._hours(5) == [5]
    assert m._hours([17, 5]) == [5, 17]
    assert m._hours("5,17") == [5, 17]
    assert m._hours("bogus") is None


def test_due_slot_and_stamp(tmp_path):
    m = _run_all()
    state = tmp_path / "state.json"
    assert m._due_slot(state, [0]) == 0          # slot 0 is always <= now
    m._stamp_slot(state, 0, [0])
    assert m._due_slot(state, [0]) is None        # already pushed today
    assert m._due_slot(state, [24]) is None        # hour 24 is never reached


def test_games_task_interval_gate(tmp_path, monkeypatch):
    m = _run_all()
    monkeypatch.setattr(m.config, "STATE_DIR", tmp_path)
    calls = {"n": 0}
    monkeypatch.setattr(m.radar, "run", lambda **k: calls.__setitem__("n", calls["n"] + 1) or 0)
    cfg = {"games": {"enabled": True, "interval_hours": 3}}
    m.games_task(cfg, tmp_path)   # due -> runs once
    m.games_task(cfg, tmp_path)   # not due (within 3h) -> skipped
    assert calls["n"] == 1


def test_sitemap_task_no_watch_is_noop(tmp_path, monkeypatch):
    m = _run_all()
    monkeypatch.setattr(m.config, "STATE_DIR", tmp_path)
    assert m.sitemap_task({"sitemap": {"enabled": True, "watch_urls": []}}, tmp_path, tmp_path / "s.json") == 0


def test_sitemap_task_interval_gate(tmp_path, monkeypatch):
    m = _run_all()
    monkeypatch.setattr(m.config, "STATE_DIR", tmp_path)
    calls = {"n": 0}
    monkeypatch.setattr(m.sitemaps, "run", lambda **k: calls.__setitem__("n", calls["n"] + 1) or 2)
    cfg = {"sitemap": {"enabled": True, "watch_urls": ["https://a/x.xml"], "interval_hours": 24}}
    assert m.sitemap_task(cfg, tmp_path, tmp_path / "s.json") == 2   # due -> runs
    assert m.sitemap_task(cfg, tmp_path, tmp_path / "s.json") == 0   # within 24h -> skipped
    assert calls["n"] == 1


def test_maybe_block_alert_once_per_day(tmp_path, monkeypatch):
    import collector.gtrends_api as g

    m = _run_all()
    g._blocked_at = "2026-10-11T01:00:00+00:00"
    sent = {"n": 0}
    monkeypatch.setattr(m, "send_text", lambda cfg, msg: sent.__setitem__("n", sent["n"] + 1) or True)
    cfg = {"push": {"enabled": True}}
    try:
        assert m.maybe_block_alert(cfg, tmp_path / "state.json") is True
        assert m.maybe_block_alert(cfg, tmp_path / "state.json") is False  # once per day
        assert sent["n"] == 1
        assert m.maybe_block_alert({"push": {"enabled": False}}, tmp_path / "state.json") is False
    finally:
        g._blocked_at = None


def test_signal_postpass_annotates_and_persists(tmp_path, monkeypatch):
    m = _run_all()
    (tmp_path / "trends.json").write_text(
        json.dumps({"items": {"US": [{"q": "new word", "new": True, "noise": "", "vol": 100}]}}),
        encoding="utf-8",
    )
    (tmp_path / "related.json").write_text(
        json.dumps({"items": [{"q": "rising word", "geos": ["US"], "noise": ""}]}),
        encoding="utf-8",
    )
    (tmp_path / "discovered.json").write_text(
        json.dumps({"items": [{"q": "fresh word", "new": True, "noise": ""}]}),
        encoding="utf-8",
    )
    (tmp_path / "state.json").write_text(
        json.dumps({"seen": {"US|new word": {}}}), encoding="utf-8",
    )
    fake = {
        "new word": {"classification": "breakout", "freshness": "new", "ratio": 6.0},
        "rising word": {"classification": "rising", "freshness": "existing", "ratio": 1.5},
        "fresh word": {"classification": "strong", "freshness": "new", "ratio": 0.8},
    }
    monkeypatch.setattr(m.signal, "analyze", lambda *a, **k: fake)

    n = m.signal_postpass({"signal": {"enabled": True, "budget": 10}}, tmp_path, tmp_path / "state.json")
    assert n == 3

    t = json.loads((tmp_path / "trends.json").read_text(encoding="utf-8"))
    assert t["items"]["US"][0]["signal"]["classification"] == "breakout"
    r = json.loads((tmp_path / "related.json").read_text(encoding="utf-8"))
    assert r["items"][0]["signal"]["classification"] == "rising"
    d = json.loads((tmp_path / "discovered.json").read_text(encoding="utf-8"))
    assert d["items"][0]["signal"]["classification"] == "strong"
    st = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert st["seen"]["US|new word"]["signal"]["ratio"] == 6.0


def test_signal_postpass_disabled_is_noop(tmp_path):
    m = _run_all()
    assert m.signal_postpass({"signal": {"enabled": False}}, tmp_path, tmp_path / "s.json") == 0


def test_fast_postpass_baseline_then_new(tmp_path, monkeypatch):
    m = _run_all()
    trends = tmp_path / "trends.json"
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"seen": {}}), encoding="utf-8")

    def write(sug):
        trends.write_text(json.dumps({"items": {"US": [{"q": "foo", "longtails": sug}]}}), encoding="utf-8")

    cfg = {"fast": {"enabled": True}}
    write(["a"])
    m.fast_postpass(cfg, tmp_path, state)
    assert "fast" not in json.loads(trends.read_text(encoding="utf-8"))["items"]["US"][0]

    write(["a", "b", "c"])
    m.fast_postpass(cfg, tmp_path, state)
    fast = json.loads(trends.read_text(encoding="utf-8"))["items"]["US"][0]["fast"]
    assert fast["newSuggestionCount"] == 2      # b, c are new vs baseline
    assert fast["classification"] == "watch"    # 2 * 8 = 16 >= 12

    # A no-change run must not report new activity.
    assert m.fast_postpass(cfg, tmp_path, state) == 0
