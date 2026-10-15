from collector.gameradar import sources as src
from collector.gameradar import store


def test_update_marks_new_and_accumulates_sightings():
    state = {"games": {}}
    t0 = "2026-01-01T00:00:00+00:00"
    new = store.update(state, [{"name": "Neon Drift.io", "source": "itch", "url": "u1"}], t0)
    assert "neon drift.io" in new
    rec = state["games"]["neon drift.io"]
    assert rec["sightings"] == 1
    assert rec["sources"] == ["itch"]
    assert rec["first"] == t0

    t1 = "2026-01-01T01:00:00+00:00"
    new2 = store.update(state, [{"name": "neon drift.io", "source": "poki", "url": "u2"}], t1)
    assert new2 == set()
    rec = state["games"]["neon drift.io"]
    assert rec["sightings"] == 2
    assert "poki" in rec["sources"]
    assert rec["url"] == "u1"  # first url kept


def test_update_ignores_blank_names():
    state = {"games": {}}
    assert store.update(state, [{"name": "  ", "source": "itch"}], "t") == set()
    assert state["games"] == {}


def test_export_filters_junk_and_marks_web_game():
    state = {"games": {
        "a": {"name": "Goblin Incremental", "first": "now", "last": "now", "sightings": 1, "sources": ["itch"], "url": None},
        "b": {"name": "wordle hints", "first": "now", "last": "now", "sightings": 1, "sources": ["x"], "url": None},
        "c": {"name": "skribbl.io", "first": "t0", "last": "t1", "sightings": 5, "sources": ["poki"], "url": None},
    }}
    out = store.export(state, "now")
    names = [i["name"] for i in out["items"]]
    assert "wordle hints" not in names
    assert set(names) == {"Goblin Incremental", "skribbl.io"}
    # new flag only for the one first seen this round
    by = {i["name"]: i for i in out["items"]}
    assert by["Goblin Incremental"]["new"] is True
    assert by["skribbl.io"]["new"] is False
    assert by["skribbl.io"]["web_game"] is True
    assert by["skribbl.io"]["q"] == "skribbl.io"  # q alias for q-keyed post-passes


def test_export_ranks_new_web_games_first():
    state = {"games": {
        "old": {"name": "Old Game", "first": "t0", "last": "t9", "sightings": 1, "sources": ["itch"], "url": None},
        "new": {"name": "Fresh.app", "first": "now", "last": "now", "sightings": 1, "sources": ["poki"], "url": None},
    }}
    out = store.export(state, "now")
    assert out["items"][0]["name"] == "Fresh.app"


def test_export_ranks_actionable_above_raw_new():
    state = {"games": {
        "raw": {"name": "Raw New", "first": "now", "last": "now", "sightings": 1, "sources": ["poki"], "url": None},
        "act": {"name": "Actionable", "first": "t0", "last": "t9", "sightings": 1, "sources": ["itch"], "url": None},
    }}
    previous = {"actionable": {"name": "Actionable", "opportunity": {"decision": "test-now", "score": 63},
                               "advice": {"stars": 4}}}
    out = store.export(state, "now", previous=previous)
    assert out["items"][0]["name"] == "Actionable"     # judged name beats raw-new
    assert out["items"][1]["name"] == "Raw New"


def test_export_ranks_blank_easy_market_first():
    state = {"games": {
        "a": {"name": "A", "first": "t0", "last": "t9", "sightings": 1, "sources": ["itch"], "url": None},
        "b": {"name": "B", "first": "now", "last": "now", "sightings": 1, "sources": ["poki"], "url": None},
    }}
    previous = {
        "a": {"name": "A", "seo": {"score": 88, "market": {"status": "greenfield"}}, "advice": {"stars": 3}},
        "b": {"name": "B", "seo": {"score": 30, "market": {"status": "occupied"}}, "advice": {"stars": 3}},
    }
    out = store.export(state, "now", previous=previous)
    assert out["items"][0]["name"] == "A"      # blank + easy beats new + occupied


def test_clean_name_strips_listing_noise():
    assert src._clean_name("New Foo Bar") == "Foo Bar"
    assert src._clean_name("Play Foo Bar Online") == "Foo Bar"
    assert src._clean_name("Foo Bar - itch.io") == "Foo Bar"
    assert src._clean_name("Top Baz 2") == "Baz"
    assert src._clean_name("1. Block & Awe") == "Block & Awe"
    assert src._clean_name("12) Some Quest") == "Some Quest"


def test_name_from_slug_skips_stopwords():
    assert src._name_from_slug("https://x.com/game/my-cool-game") == "My Cool"
    assert src._name_from_slug("https://x.com/game/play-online") == ""


def test_parse_feed_parses_items():
    xml = (
        "<rss><channel>"
        "<item><title>Alpha Quest</title><link>https://x/alpha</link></item>"
        "<item><title>Beta Blast</title><link>https://x/beta</link></item>"
        "</channel></rss>"
    )
    out = src.parse_feed(xml)
    assert [e["name"] for e in out] == ["Alpha Quest", "Beta Blast"]
    assert out[0]["url"] == "https://x/alpha"


def test_parse_itch_uses_title_anchors():
    html = (
        '<div class="game_cell">'
        '<a class="title game_link" href="/foo/bar">Gamma Garden</a>'
        "<a class=\"other\" href=\"/should/skip\">Skip Me</a>"
        "</div>"
    )
    out = src.parse_itch(html, "https://itch.io")
    assert [e["name"] for e in out] == ["Gamma Garden"]
    assert out[0]["url"] == "https://itch.io/foo/bar"


def test_parse_portal_filters_host_and_path_profile():
    html = (
        '<a href="/en/game/neon-drift">Neon Drift</a>'
        '<a href="/en/game">All Games</a>'
        '<a href="https://other.com/game/cool-thing">External</a>'
    )
    out = src.parse_portal(html, "https://www.crazygames.com/new", "crazygames")
    assert [e["name"] for e in out] == ["Neon Drift"]


def test_parse_portal_falls_back_to_slug_name():
    html = '<a href="/play/goblin-incremental"></a>'
    out = src.parse_portal(html, "https://www.gamepix.com/new", "gamepix")
    assert [e["name"] for e in out] == ["Goblin Incremental"]


def test_is_probably_movie():
    assert src.is_probably_movie("LONELY GHOST | ANIMATION MEME | x")
    assert src.is_probably_movie("THE SCARIEST CARTOON EVER MADE!!!!")
    assert src.is_probably_movie("SLUDGE BROTHERS - Ep 2: The Greasy Goiter")
    assert not src.is_probably_movie("Block & Awe")
    assert not src.is_probably_movie("Nuclear Daze: The Genesis Trials")
    assert not src.is_probably_movie("Animal Crossing")  # "anim" must be a whole word


def test_discover_source_newgrounds_filters_movies(monkeypatch):
    xml = (
        "<rss><channel>"
        "<item><title>Cool Quest</title><link>https://ng/g1</link></item>"
        "<item><title>Some Animation Meme</title><link>https://ng/m1</link></item>"
        "</channel></rss>"
    )
    monkeypatch.setattr(src, "get_text", lambda session, url, throttle=0: xml)
    out = src.discover_source(None, "newgrounds", throttle=0)
    assert [e["name"] for e in out] == ["Cool Quest"]


def test_export_filters_movies():
    state = {"games": {
        "a": {"name": "Block & Awe", "first": "now", "last": "now", "sightings": 1,
              "sources": ["newgrounds"], "sourceFirst": {"newgrounds": "now"}},
        "b": {"name": "LONELY GHOST ANIMATION MEME", "first": "now", "last": "now", "sightings": 1,
              "sources": ["newgrounds"], "sourceFirst": {"newgrounds": "now"}},
    }}
    names = [i["name"] for i in store.export(state, "now")["items"]]
    assert "Block & Awe" in names
    assert not any("ANIMATION" in n for n in names)


def test_update_tracks_source_first_and_platform_collapse():
    from collector.gameradar import signals

    state = {"games": {}}
    t0 = "2026-01-01T00:00:00+00:00"
    store.update(
        state,
        [{"name": "Foo", "source": "itch", "url": None},
         {"name": "Foo", "source": "itch-popular", "url": None}],
        t0,
    )
    rec = state["games"]["foo"]
    assert set(rec["sources"]) == {"itch", "itch-popular"}
    fast = signals.compute_fast(rec, signals.to_ms(t0))
    assert fast["onlinePlatformCount"] == 1  # two itch feeds collapse to one platform


def test_compute_fast_multi_platform_is_pass():
    from collector.gameradar import signals

    rec = {
        "name": "Bar",
        "sources": ["itch", "poki"],
        "sourceFirst": {"itch": "2026-01-01T00:00:00+00:00", "poki": "2026-01-01T00:00:00+00:00"},
        "sightings": 1,
    }
    fast = signals.compute_fast(rec, signals.to_ms("2026-01-01T01:00:00+00:00"))
    assert fast["onlinePlatformCount"] == 2
    assert fast["sourceAdded24h"] == 2
    assert fast["classification"] == "pass"


def test_export_attaches_fast():
    state = {"games": {
        "a": {"name": "Foo", "first": "now", "last": "now", "sightings": 1,
              "sources": ["itch", "poki"],
              "sourceFirst": {"itch": "now", "poki": "now"}, "url": None},
    }}
    out = store.export(state, "now")
    assert out["items"][0]["fast"]["onlinePlatformCount"] == 2


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


def test_youtube_name_matches():
    from collector.gameradar import youtube

    assert youtube.name_matches("Let's Play Neon Drift.io!", "neon drift.io")
    assert not youtube.name_matches("Totally unrelated video", "neon drift.io")


def test_verify_youtube_summarizes(monkeypatch):
    from collector.gameradar import youtube as Y

    search = {"items": [
        {"id": {"videoId": "v1"}, "snippet": {"title": "Neon Drift.io gameplay", "description": ""}},
        {"id": {"videoId": "v2"}, "snippet": {"title": "unrelated clip", "description": ""}},
    ]}
    stats = {"items": [
        {"snippet": {"channelId": "c1", "publishedAt": "2026-10-11T00:30:00Z"},
         "statistics": {"viewCount": "1000"}},
    ]}
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params))
        return _Resp(search if url.endswith("/search") else stats)

    monkeypatch.setattr(Y.requests, "get", fake_get)
    now_ms = Y.datetime.fromisoformat("2026-10-11T01:00:00+00:00").timestamp() * 1000
    out = Y.verify_youtube("Neon Drift.io", "KEY", throttle=0, now_ms=now_ms)
    assert out["videoCount"] == 1
    assert out["channelCount"] == 1
    assert out["totalViews"] == 1000
    assert out["recent24h"] == 1
    assert len(calls) == 2
    assert calls[0][1]["key"] == "KEY"


def test_verify_youtube_without_key_is_none():
    from collector.gameradar import youtube

    assert youtube.verify_youtube("Foo", "", throttle=0) is None


def test_compute_fast_youtube_bonus():
    from collector.gameradar import signals

    now = signals.to_ms("2026-10-11T01:00:00+00:00")
    plain = signals.compute_fast(
        {"name": "Foo", "sources": ["itch"], "sourceFirst": {"itch": "2026-10-11T00:00:00+00:00"}, "sightings": 1},
        now,
    )
    boosted = signals.compute_fast({
        "name": "Foo", "sources": ["itch"],
        "sourceFirst": {"itch": "2026-10-11T00:00:00+00:00"}, "sightings": 1,
        "youtube": {"channelCount": 4, "videoCount": 8, "totalViews": 30000},
    }, now)
    assert boosted["score"] >= plain["score"]
    assert boosted["score"] > 0


def test_persist_enrichment_round_trips():
    state = {"games": {"foo": {"name": "Foo", "first": "t0", "last": "t1", "sightings": 1,
                                "sources": ["itch"], "sourceFirst": {"itch": "t0"}}}}
    items = [{"name": "Foo", "seo": {"score": 80}, "advice": {"stars": 4},
              "opportunity": {"decision": "test-now"}}]
    n = store.persist_enrichment(state, items)
    assert n == 1
    assert state["games"]["foo"]["seo"]["score"] == 80
    out = store.export(state, "t1")
    assert out["items"][0]["seo"]["score"] == 80
    assert out["items"][0]["opportunity"]["decision"] == "test-now"


def test_export_carries_forward_enrichment():
    state = {"games": {
        "a": {"name": "Foo", "first": "t0", "last": "now", "sightings": 1,
              "sources": ["itch"], "sourceFirst": {"itch": "now"}},
    }}
    previous = {"foo": {"seo": {"score": 80}, "advice": {"stars": 4},
                        "opportunity": {"decision": "test-now"}}}
    out = store.export(state, "now", previous=previous)
    it = out["items"][0]
    assert it["seo"]["score"] == 80
    assert it["advice"]["stars"] == 4
    assert it["opportunity"]["decision"] == "test-now"


def test_export_includes_youtube():
    state = {"games": {
        "a": {"name": "Foo", "first": "now", "last": "now", "sightings": 1,
              "sources": ["itch"], "sourceFirst": {"itch": "now"},
              "youtube": {"videoCount": 2, "channelCount": 1, "totalViews": 500}},
    }}
    out = store.export(state, "now")
    assert out["items"][0]["youtube"]["videoCount"] == 2


