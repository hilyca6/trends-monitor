import base64

import collector.seocheck as S


def _bing_link(url: str) -> str:
    b64 = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return f'<a href="https://www.bing.com/ck/a?!&amp;&amp;p=abc&amp;u=a1{b64}&amp;ntb=1" h="ID=SERP">r</a>'


def _page(urls: list[str]) -> str:
    return "".join(_bing_link(u) for u in urls)


HTML_STRONG = _page(
    [
        "https://en.wikipedia.org/wiki/Topic",
        "https://www.nytimes.com/2026/10/10/topic.html",
        "https://www.espn.com/story/topic",
        "https://www.imdb.com/title/tt123",
        "https://www.amazon.com/s?k=topic",
    ]
)

HTML_WEAK = _page(
    [
        "https://joesblog.example.com/topic",
        "https://www.reddit.com/r/xyz/comments/abc/topic/",
        "https://smallforum.net/thread/123",
        "https://another-small-blog.org/topic",
    ]
)


def test_extract_domains_decodes_bing_links():
    domains = S.extract_domains(HTML_STRONG)
    assert domains[:2] == ["en.wikipedia.org", "nytimes.com"]
    assert "espn.com" in domains


def test_extract_domains_skips_bing_internal():
    html = _bing_link("https://www.bing.com/search?q=x") + _bing_link(
        "https://go.microsoft.com/fwlink/x"
    )
    assert S.extract_domains(html) == []


def test_extract_domains_plain_anchors_still_work():
    html = '<a href="https://example.org/page">1</a>'
    assert S.extract_domains(html) == ["example.org"]


def test_extract_results_yahoo_decodes_ru_and_titles():
    html = (
        '<h3 class="title"><a href="https://r.search.yahoo.com/_ylt=x/RU=https%3a%2f%2fen.wikipedia.org%2fwiki%2fTopic/RK=2/RS=y">Topic - Wikipedia</a></h3>'
        '<a href="https://r.search.yahoo.com/x/RU=https%3a%2f%2fjoesblog.example.com%2ftopic/RK=2">Joe blog</a>'
        '<a href="https://r.search.yahoo.com/x/RU=https%3a%2f%2fwww.yahoo.com%2fnews/RK=2">news</a>'
    )
    recs = S.extract_results_yahoo(html)
    assert [r["host"] for r in recs] == ["en.wikipedia.org", "joesblog.example.com"]
    assert recs[0]["title"] == "Topic - Wikipedia"
    assert S.score_serp([r["host"] for r in recs])["score"] is not None



def test_extract_domains_empty_on_no_results():
    assert S.extract_domains("<html><body>anomaly page</body></html>") == []


def test_score_serp_strong_page_is_hard():
    r = S.score_serp(S.extract_domains(HTML_STRONG))
    assert r["strong"] >= 4
    assert r["score"] <= 20
    assert r["label"] == "低"


def test_score_serp_weak_ugc_page_is_easy():
    r = S.score_serp(S.extract_domains(HTML_WEAK))
    assert r["ugc"] is True
    assert r["score"] >= 70
    assert r["label"] == "高"


def test_score_serp_no_domains_is_unknown():
    r = S.score_serp([])
    assert r["score"] is None
    assert r["label"] == "?"


def test_check_queries_caches(tmp_path, monkeypatch):
    import json

    cache = tmp_path / "seo_cache.json"
    calls = {"n": 0}

    def fake_get_html(session, url):
        calls["n"] += 1
        return HTML_WEAK

    monkeypatch.setattr(S, "get_html", fake_get_html)
    monkeypatch.setattr(S, "_direct_session", lambda: None)
    r1 = S.check_queries(None, ["alpha", "beta"], throttle=0, cache_path=cache)
    assert r1["alpha"]["label"] == "高"
    assert "market" in r1["alpha"] and "nameRisk" in r1["alpha"]
    assert calls["n"] == 2

    r2 = S.check_queries(None, ["alpha", "beta"], throttle=0, cache_path=cache)
    assert calls["n"] == 2  # served from cache
    assert r2 == r1
    data = json.loads(cache.read_text(encoding="utf-8"))
    assert set(data) == {"alpha", "beta"}


def test_extract_results_records_host_and_path():
    recs = S.extract_results(
        _page(["https://foo.example.com/games/among-us", "https://www.reddit.com/r/x/amongus"])
    )
    assert recs[0]["host"] == "foo.example.com"
    assert recs[0]["path"] == "/games/among-us"


def test_market_status_dedicated_domain_is_occupied():
    results = [{"url": "https://amonguswiki.com/x", "host": "amonguswiki.com", "path": "/x"}]
    m = S.market_status("among us", results)
    assert m["status"] == "occupied"
    assert "amonguswiki.com" in m["dedicatedDomains"]


def test_market_status_content_competitor_contested_and_occupied():
    one = [{"url": "https://a.com/among-us-guide", "host": "a.com", "path": "/among-us-guide"}]
    assert S.market_status("among us", one)["status"] == "contested"
    two = one + [{"url": "https://b.com/among-us-wiki", "host": "b.com", "path": "/among-us-wiki"}]
    assert S.market_status("among us", two)["status"] == "occupied"


def test_market_status_greenfield_ignores_platforms():
    results = [
        {"url": "https://itch.io/games/among-us", "host": "itch.io", "path": "/games/among-us"},
        {"url": "https://random.news/story", "host": "random.news", "path": "/story"},
    ]
    assert S.market_status("among us", results)["status"] == "greenfield"


def test_market_status_unknown_without_results():
    assert S.market_status("x", [])["status"] == "unknown"


def test_name_risk():
    assert S.name_risk("") == 25
    assert S.name_risk("stellar drift odyssey") == 0
    assert S.name_risk("free online game") > S.name_risk("stellar drift odyssey")
    assert S.name_risk("crypto price") >= 8  # high-risk term


def test_classify_intent_game_results():
    results = [
        {"url": "https://itch.io/games/among-us", "host": "itch.io", "path": "/games/among-us", "title": "Among Us"},
        {"url": "https://crazygames.com/game/among-us", "host": "crazygames.com", "path": "/game/among-us", "title": "Play Among Us online"},
    ]
    intent = S.classify_intent(results, "among us")
    assert intent["game"] == 2
    assert intent["gameRatio"] == 1.0


def test_classify_intent_flags_song_and_news():
    songs = [
        {"url": "https://genius.com/x", "host": "genius.com", "path": "/x", "title": "Among Us lyrics"},
        {"url": "https://open.spotify.com/track", "host": "open.spotify.com", "path": "/track", "title": "Among Us song"},
    ]
    assert S.classify_intent(songs, "among us")["nonGameRatio"] >= 0.4
    news = [
        {"url": "https://www.reuters.com/x", "host": "reuters.com", "path": "/x", "title": "Among Us clash"},
    ]
    assert S.classify_intent(news, "among us")["news"] == 1


def test_extract_results_carries_titles():
    html = _page(["https://itch.io/games/among-us"])
    # _page anchors have the text "r"; the title map still resolves via URL.
    recs = S.extract_results(html)
    assert "title" in recs[0]


def test_check_queries_rechecks_stale(tmp_path, monkeypatch):
    import json
    from datetime import datetime, timedelta, timezone

    cache = tmp_path / "seo_cache.json"
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    cache.write_text(json.dumps({"alpha": {"score": 80, "label": "高", "checked": old, "serpUrls": []}}), encoding="utf-8")
    calls = {"n": 0}
    monkeypatch.setattr(S, "get_html", lambda session, url: calls.__setitem__("n", calls["n"] + 1) or HTML_WEAK)
    monkeypatch.setattr(S, "_direct_session", lambda: None)

    r = S.check_queries(None, ["alpha"], throttle=0, cache_path=cache, cache_days=7)
    assert calls["n"] == 1          # stale -> refetched
    assert "market" in r["alpha"]

    calls["n"] = 0
    S.check_queries(None, ["alpha"], throttle=0, cache_path=cache, cache_days=7)
    assert calls["n"] == 0          # now fresh -> cached
