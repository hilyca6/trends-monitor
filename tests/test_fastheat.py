import json

import collector.fastheat as F


def test_compute_none_on_baseline():
    assert F.compute({"entries": {}}, "q", ["a"], ["u1"]) is None


def test_heat_baseline_then_detects_new_then_none():
    st = {"entries": {}}
    assert F.heat(st, "q", ["a", "b"], ["u1"], "t1") is None  # baseline
    h = F.heat(st, "q", ["a", "b", "c"], ["u1", "u2"], "t2")
    assert h["newSuggestionCount"] == 1
    assert h["newSerpPageCount"] == 1
    assert h["classification"] in ("watch", "pass")
    h2 = F.heat(st, "q", ["a", "b", "c"], ["u1", "u2"], "t3")
    assert h2["newSuggestionCount"] == 0 and h2["newSerpPageCount"] == 0
    assert h2["classification"] == "weak"


def test_classification_pass_threshold():
    st = {"entries": {"q": {"suggest": [], "serp": []}}}
    h = F.compute(st, "q", ["a", "b", "c", "d", "e", "f"], [])
    assert h["classification"] == "pass"  # 6 * 8 = 48 >= 40


def test_save_prunes_old_entries(tmp_path):
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    st = {"entries": {
        "old": {"suggest": [], "serp": [], "ts": old},
        "new": {"suggest": [], "serp": [], "ts": now},
    }}
    F.save(tmp_path / "f.json", st, days=90)
    data = json.loads((tmp_path / "f.json").read_text(encoding="utf-8"))
    assert set(data["entries"]) == {"new"}
