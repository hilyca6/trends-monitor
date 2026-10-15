import json

import pytest

import collector.collect as C
from collector.collect import _load_partial, dedupe_sort, fetch_geo
from collector.gtrends_api import BlockedError
from collector.providers.base import TrendProvider


class Ok(TrendProvider):
    name = "ok"

    def fetch(self, geo):
        return [{"q": f"{geo}-term", "geo": geo, "vol": 1, "growth": 1, "cats": [], "variants": []}]


class Empty(TrendProvider):
    name = "empty"

    def fetch(self, geo):
        return []


class Boom(TrendProvider):
    name = "boom"

    def fetch(self, geo):
        raise RuntimeError("network down")


def _p(cls):
    return cls(None)


def test_fetch_geo_primary_ok_no_fallback_call():
    out = fetch_geo(_p(Ok), _p(Empty), "US")
    assert len(out) == 1


def test_fetch_geo_falls_back_on_empty():
    out = fetch_geo(_p(Empty), _p(Ok), "GB")
    assert len(out) == 1
    assert out[0]["src"] == "ok"  # tagged with the serving provider


def test_fetch_geo_falls_back_on_exception():
    out = fetch_geo(_p(Boom), _p(Ok), "JP")
    assert len(out) == 1
    assert out[0]["src"] == "ok"


def test_fetch_geo_both_fail_returns_empty():
    assert fetch_geo(_p(Boom), _p(Empty), "DE") == []
    assert fetch_geo(_p(Empty), None, "DE") == []


class Blocked(TrendProvider):
    name = "blocked"

    def fetch(self, geo):
        raise BlockedError("429")


def test_fetch_geo_propagates_block_without_fallback():
    # A Google block must NOT be caught as a generic error and fall back to RSS
    # (same exit IP, also blocked) — it has to bubble up so the run stops.
    with pytest.raises(BlockedError):
        fetch_geo(_p(Blocked), _p(Ok), "US")


def test_run_stops_after_first_block(tmp_path, monkeypatch):
    attempted: list[str] = []

    class BlockingProvider(TrendProvider):
        name = "blocking"

        def __init__(self, session):
            pass

        def fetch(self, geo):
            attempted.append(geo)
            raise BlockedError("429")

    monkeypatch.setattr(C, "build_session", lambda proxy: None)
    monkeypatch.setattr(C, "GoogleTrendsSSR", BlockingProvider)
    monkeypatch.setattr(C, "GoogleTrendsRSS", BlockingProvider)
    monkeypatch.setattr(C, "enrich", lambda items, **k: items)

    n = C.run(["US", "GB", "JP"], proxy=None, provider_name="ssr", use_llm=False,
              out_dir=tmp_path, state_path=tmp_path / "state.json")
    assert attempted == ["US"]  # stopped right after the first geo blocked
    assert n == 0


def test_dedupe_sort_by_volume_then_growth():
    items = [
        {"q": "low", "vol": 100, "growth": 5},
        {"q": "high", "vol": 500, "growth": 1},
        {"q": "mid", "vol": 300, "growth": 1},
    ]
    assert [r["q"] for r in dedupe_sort(items)] == ["high", "mid", "low"]


def test_dedupe_sort_dedupes_by_query():
    items = [{"q": "a", "vol": 1}, {"q": "a", "vol": 2}]
    assert len(dedupe_sort(items)) == 1


def test_load_partial_resumes_fresh_checkpoint(tmp_path):
    from datetime import datetime, timezone

    p = tmp_path / "collect_partial.json"
    p.write_text(
        json.dumps(
            {
                "updated": datetime.now(timezone.utc).isoformat(),
                "items": {"US": [{"q": "x"}], "GB": [{"q": "y"}]},
            }
        ),
        encoding="utf-8",
    )
    out = _load_partial(p, ["US", "GB", "JP"], max_age_hours=6)
    assert set(out) == {"US", "GB"}


def test_load_partial_ignores_stale_checkpoint(tmp_path):
    from datetime import datetime, timedelta, timezone

    p = tmp_path / "collect_partial.json"
    old = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
    p.write_text(json.dumps({"updated": old, "items": {"US": [{"q": "x"}]}}), encoding="utf-8")
    assert _load_partial(p, ["US"], max_age_hours=6) == {}


def test_load_partial_missing_returns_empty(tmp_path):
    assert _load_partial(tmp_path / "nope.json", ["US"]) == {}


def test_run_resumes_and_clears_checkpoint(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    fetched: list[str] = []

    class FakeProvider(TrendProvider):
        name = "fake"

        def __init__(self, session):
            pass

        def fetch(self, geo):
            fetched.append(geo)
            return [{"q": f"{geo} kw", "geo": geo, "vol": 1, "growth": 1, "cats": [], "variants": []}]

    monkeypatch.setattr(C, "build_session", lambda proxy: None)
    monkeypatch.setattr(C, "GoogleTrendsSSR", FakeProvider)
    monkeypatch.setattr(C, "GoogleTrendsRSS", FakeProvider)
    monkeypatch.setattr(C, "enrich", lambda items, **k: items)

    (tmp_path / "collect_partial.json").write_text(
        json.dumps(
            {
                "updated": datetime.now(timezone.utc).isoformat(),
                "items": {"US": [{"q": "US kw", "geo": "US", "vol": 1, "growth": 1, "cats": [], "variants": []}]},
            }
        ),
        encoding="utf-8",
    )

    n = C.run(["US", "GB"], proxy=None, provider_name="ssr", use_llm=False,
              out_dir=tmp_path, state_path=tmp_path / "state.json")
    assert fetched == ["GB"]          # US served from the checkpoint, not refetched
    assert n == 2
    assert not (tmp_path / "collect_partial.json").exists()
    trends = json.loads((tmp_path / "trends.json").read_text(encoding="utf-8"))
    assert set(trends["items"]) == {"US", "GB"}
