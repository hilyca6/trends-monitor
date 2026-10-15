import json
from datetime import datetime, timedelta, timezone

import collector.discover as D


def test_enumerate_collects_seeds_and_dedupes(monkeypatch):
    def fake_fetch(session, q, hl="en", timeout=15):
        return [q, "best ai app", "best ai app"]

    monkeypatch.setattr(D, "fetch_suggest", fake_fetch)
    out = D.enumerate_seeds(None, ["best ai"], charset="ab", throttle=0)
    assert set(out) == {"best ai app"}
    assert out["best ai app"]["seeds"] == ["best ai"]
    assert set(out["best ai app"]["chars"]) == {"a", "b"}


def test_enumerate_respects_budget(monkeypatch):
    calls = {"n": 0}

    def fake_fetch(*a, **k):
        calls["n"] += 1
        return [f"sug{calls['n']}"]

    monkeypatch.setattr(D, "fetch_suggest", fake_fetch)
    out = D.enumerate_seeds(None, ["s1", "s2"], charset="abcdef", throttle=0, budget=3)
    assert calls["n"] == 3
    assert len(out) == 3


def test_export_marks_new_and_sorts():
    now = datetime.now(timezone.utc).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    seen = {
        "w_new": {"first": now, "last": now, "sightings": 1, "seeds": ["best ai"], "zh": "", "noise": ""},
        "w_old": {"first": old, "last": old, "sightings": 3, "seeds": ["best cd"], "zh": "", "noise": ""},
    }
    out = D.export_discovered(seen, ["best ai"], now, days=30)
    assert out["items"][0]["q"] == "w_new"
    assert out["items"][0]["new"] is True
    assert out["items"][1]["new"] is False


def test_is_due():
    now = datetime.now(timezone.utc)
    assert D.is_due({"last_run": (now - timedelta(hours=25)).isoformat()}, now.isoformat(), 24) is True
    assert D.is_due({"last_run": (now - timedelta(hours=1)).isoformat()}, now.isoformat(), 24) is False
    assert D.is_due({}, now.isoformat(), 24) is True


def test_run_writes_discovered(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "build_session", lambda proxy: None)
    monkeypatch.setattr(
        D, "enumerate_seeds",
        lambda *a, **k: {"best ai app": {"seeds": ["best ai"], "chars": ["a"]}},
    )
    monkeypatch.setattr(D, "enrich", lambda items, **k: items)

    state = tmp_path / "discover_state.json"
    n = D.run(proxy=None, seeds=["best ai"], out_dir=tmp_path, state_path=state, use_llm=False)
    assert n == 1

    data = json.loads((tmp_path / "discovered.json").read_text(encoding="utf-8"))
    assert data["items"][0]["q"] == "best ai app"
    assert data["items"][0]["new"] is True
    st = json.loads(state.read_text(encoding="utf-8"))
    assert st["last_run"]


def test_run_no_seeds_is_noop(tmp_path):
    assert D.run(proxy=None, seeds=[], out_dir=tmp_path, state_path=tmp_path / "s.json") == 0
    assert not (tmp_path / "discovered.json").exists()


def test_default_state_path_name():
    assert D.default_state_path().name == "discover_state.json"


def test_run_drops_single_token_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "build_session", lambda proxy: None)
    monkeypatch.setattr(
        D, "enumerate_seeds",
        lambda *a, **k: {
            "chatgpt": {"seeds": ["claude"], "chars": ["a"]},
            "best ai app": {"seeds": ["best ai"], "chars": ["a"]},
        },
    )
    monkeypatch.setattr(D, "enrich", lambda items, **k: items)
    D.run(proxy=None, seeds=["best ai"], out_dir=tmp_path, state_path=tmp_path / "s.json", use_llm=False)
    data = json.loads((tmp_path / "discovered.json").read_text(encoding="utf-8"))
    assert [it["q"] for it in data["items"]] == ["best ai app"]
