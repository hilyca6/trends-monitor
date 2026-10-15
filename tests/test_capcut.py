import json
from datetime import datetime, timedelta, timezone

import collector.capcut as C


def _rec(tid, uses, likes=0, title="t", url=None):
    return {
        "templateId": str(tid),
        "useCount": uses,
        "likeCount": likes,
        "commentCount": 3,
        "title": title,
        "structuredData": {
            "url": url or f"https://www.capcut.com/template-detail/x/{tid}",
            "interactionStatistic": {"useCount": uses, "likeCount": likes},
        },
    }


def _html(video=None, image=None):
    payload = {"loaderData": {"$": {"explore": {
        "videoTemplates": video or [],
        "videoTemplateList": {"videoTemplates": video or []},
        "imageTemplateList": {"imageTemplates": image or []},
    }}}}
    return '<script type="application/json" id="__MODERN_ROUTER_DATA__">' + json.dumps(payload) + "</script>"


def _img(tid, title="img"):
    return {"templateId": str(tid), "title": title, "structuredData": {"interactionStatistic": {}}}


def test_parse_uses_text():
    assert C.parse_uses_text("3M") == 3_000_000
    assert C.parse_uses_text("2.5M") == 2_500_000
    assert C.parse_uses_text("999.9K") == 999_900
    assert C.parse_uses_text("") == 0


def test_build_url():
    assert C.build_url("video", 1) == C.SOURCE_URL + "?templateType=video"
    assert C.build_url("image", 2) == C.SOURCE_URL + "?templateType=image&page=2"
    assert C.build_url("", 1) == C.SOURCE_URL


def test_parse_router_data_exact_counts_and_dedupe():
    html = _html(video=[_rec(1, 5_500_000, 379_263, "New trend")], image=[_img(2)])
    cards = C.parse_router_data(html)
    by_id = {c["id"]: c for c in cards}
    assert by_id["1"]["uses"] == 5_500_000  # exact, not abbreviated
    assert by_id["1"]["likes"] == 379_263
    assert by_id["1"]["type"] == "video"
    assert by_id["1"]["url"].endswith("/1")
    assert by_id["2"]["type"] == "image"
    assert by_id["2"]["uses"] == 0


def test_parse_router_data_none_when_no_script():
    assert C.parse_router_data("<html>nope</html>") == []
    assert C.parse_router_data(None) == []


def test_update_marks_baseline_then_new(tmp_path):
    state = {"items": {}}
    t0 = datetime.now(timezone.utc).isoformat()
    C.update(state, [{"id": "1", "title": "a", "url": "", "type": "video",
                      "uses": 100, "likes": 1, "comments": 0, "rank": 1}], t0)
    # baseline run: no fake "first seen"
    assert state["items"]["1"]["first"] is None
    payload = C.export_items(state, t0, ["video"], 1)
    assert payload["items"][0]["baseline"] is True
    assert payload["items"][0]["new"] is False

    t1 = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    C.update(state, [{"id": "1", "title": "a", "url": "", "type": "video",
                      "uses": 150, "likes": 5, "comments": 0, "rank": 1},
                     {"id": "2", "title": "b", "url": "", "type": "video",
                      "uses": 7, "likes": 0, "comments": 0, "rank": 2}], t1)
    payload = C.export_items(state, t1, ["video"], 1)
    items = {i["id"]: i for i in payload["items"]}
    assert items["1"]["usesDelta"] == 50
    assert items["1"]["likesDelta"] == 4
    assert items["1"]["new"] is False
    assert items["2"]["new"] is True
    assert items["2"]["level"] == "new"


def test_history_accumulates_and_prunes():
    state = {"items": {}}
    t0 = datetime.now(timezone.utc).isoformat()
    C.update(state, [{"id": "1", "title": "a", "url": "", "type": "video",
                      "uses": 100, "likes": 1, "comments": 0, "rank": 1}], t0)
    t1 = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    C.update(state, [{"id": "1", "title": "a", "url": "", "type": "video",
                      "uses": 150, "likes": 1, "comments": 0, "rank": 1}], t1)
    hist = state["items"]["1"]["history"]
    assert [p[1] for p in hist] == [100, 150]
    assert C.export_items(state, t1, ["video"], 1)["items"][0]["history"] == hist

    # prune points older than the window (keeps only the recent one)
    removed = C.prune_history(state, t1, days=0.1)
    assert removed == 1
    assert state["items"]["1"]["history"] == [[t1, 150]]


def test_level_thresholds():
    assert C._level(0, 0.0, False) == "steady"
    assert C._level(1, 0.5, False) == "surging"
    assert C._level(1, 0.03, False) == "rising"
    assert C._level(1, 0.005, False) == "up"
    assert C._level(0, 0.0, True) == "new"


def test_run_writes_payload_and_state(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "build_session", lambda proxy: object())
    cards = [{"id": "1", "title": "a", "url": "u", "type": "video",
              "uses": 100, "likes": 1, "comments": 0, "rank": 1}]
    monkeypatch.setattr(C, "fetch_cards", lambda session, types, pages, throttle: list(cards))
    sp = tmp_path / "capcut_state.json"
    n = C.run(proxy=None, out_dir=tmp_path, state_path=sp, template_types=["video"], max_pages=1, throttle=0)
    assert n == 1
    data = json.loads((tmp_path / "capcut.json").read_text(encoding="utf-8"))
    assert data["baselined"] is True
    assert data["items"][0]["id"] == "1"
    st = json.loads(sp.read_text(encoding="utf-8"))
    assert st["baselined"] is True
    assert "1" in st["items"]


def test_fetch_cards_dedupes_and_ranks(monkeypatch):
    monkeypatch.setattr(C, "build_session", lambda proxy: object())
    pages = {
        C.build_url("video", 1): _html(video=[_rec(1, 10), _rec(2, 5)]),
        C.build_url("video", 2): _html(video=[_rec(2, 5), _rec(3, 1)]),
    }
    monkeypatch.setattr(C, "get_text", lambda session, url, **k: pages.get(url, ""))
    cards = C.fetch_cards(object(), ["video"], 2, throttle=0)
    assert [c["id"] for c in cards] == ["1", "2", "3"]
    assert [c["rank"] for c in cards] == [1, 2, 3]  # ranked by use-count desc
