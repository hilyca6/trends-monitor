from pathlib import Path

from collector.parser import extract_ds0_payload, parse_trends

FIXTURE = Path(__file__).parent / "fixtures" / "sample_trending.html"


def test_extract_ds0_payload_returns_data_array():
    payload = extract_ds0_payload(FIXTURE.read_text(encoding="utf-8"))
    assert payload is not None
    assert payload[0] is None
    assert isinstance(payload[1], list)
    assert len(payload[1]) == 3


def test_parse_trends_maps_fields_and_skips_empty_query():
    items = parse_trends(FIXTURE.read_text(encoding="utf-8"), "US")
    assert len(items) == 2
    assert [it["q"] for it in items] == ["susan dell", "trump accounts"]

    first = items[0]
    assert first["geo"] == "US"
    assert first["vol"] == 200000
    assert first["growth"] == 1000
    assert first["cats"] == [4]
    assert first["variants"] == ["susan dell", "dell wife"]
    assert first["ts"] == 1791393000

    second = items[1]
    assert second["cats"] == [3, 10]


def test_parse_trends_returns_empty_when_no_payload():
    assert parse_trends("<html></html>", "US") == []
