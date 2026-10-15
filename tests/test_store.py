from datetime import datetime, timedelta, timezone

from collector.store import export_history, export_trends, now_iso, prune_records, prune_state, update_state


def _item(q, vol, growth, cats=None):
    return {"q": q, "vol": vol, "growth": growth, "cats": cats or [], "zh": "", "noise": ""}


def test_new_flag_only_on_first_sighting():
    state = {"seen": {}}
    first = update_state(state, {"US": [_item("a", 100, 50)]}, now_iso())
    assert first["seen"]["US|a"]

    items = [_item("a", 200, 90), _item("b", 10, 10)]
    update_state(state, {"US": items}, now_iso())
    assert items[0]["new"] is False
    assert items[1]["new"] is True
    assert state["seen"]["US|a"]["vol_peak"] == 200
    assert state["seen"]["US|a"]["growth_peak"] == 90


def test_export_trends_shape():
    update_state({"seen": {}}, {"US": [_item("q1", 500, 100, [3])]}, now_iso())
    trend = export_trends({"US": [{"q": "q1", "vol": 500, "growth": 100, "cats": [3], "noise": "", "new": True}]}, ["US"], {3: "商业财经"}, now_iso())
    assert trend["geos"] == ["US"]
    assert trend["cats"] == {"3": "商业财经"}
    assert trend["items"]["US"][0]["q"] == "q1"


def test_export_history_sorted_by_peak():
    state = {"seen": {}}
    update_state(state, {"US": [_item("low", 10, 1), _item("high", 999, 5)]}, now_iso())
    hist = export_history(state, now_iso())
    assert [r["q"] for r in hist["items"]] == ["high", "low"]


def test_update_state_carries_related_forward():
    state = {"seen": {}}
    update_state(state, {"US": [_item("a", 10, 1)]}, now_iso())
    state["seen"]["US|a"]["related"] = {"rising": ["x"], "top": []}
    items = [_item("a", 20, 2)]
    update_state(state, {"US": items}, now_iso())
    assert items[0]["related"] == {"rising": ["x"], "top": []}


def test_export_trends_includes_related():
    row = {"q": "q1", "vol": 1, "growth": 1, "cats": [], "noise": "", "new": True,
           "related": {"rising": ["r"], "top": []}}
    trend = export_trends({"US": [row]}, ["US"], {}, now_iso())
    assert trend["items"]["US"][0]["related"]["rising"] == ["r"]


def test_prune_records_drops_stale_keeps_recent_and_undated():
    now = datetime.now(timezone.utc).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    records = {
        "keep": {"last": now},
        "drop": {"last": old},
        "undated": {},
    }
    assert prune_records(records, days=90) == 1
    assert set(records) == {"keep", "undated"}


def test_prune_state_prunes_seen():
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    state = {"seen": {"US|x": {"last": old}, "US|y": {"last": now_iso()}}}
    assert prune_state(state, days=90) == 1
    assert set(state["seen"]) == {"US|y"}


def test_export_history_uses_now_param():
    state = {"seen": {"US|x": {"last": now_iso(), "vol_peak": 1}}}
    # A far-future `now` makes everything stale; if the function ignored `now`
    # and used datetime.now() the recent item would be included instead.
    assert export_history(state, "3000-01-01T00:00:00+00:00")["items"] == []
    assert export_history(state, now_iso())["items"] != []
