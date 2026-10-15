import json
from datetime import datetime, timedelta, timezone

import collector.sitemaps as S


class _Resp:
    def __init__(self, text, status=200):
        self.status_code = status
        self.text = text


class _Session:
    def __init__(self, pages):
        self.pages = pages

    def get(self, url, timeout=30):
        return _Resp(self.pages.get(url, ""), 200 if url in self.pages else 404)


def test_extract_keyword_from_slug():
    assert S.extract_keyword("https://x.com/games/wordle-unlimited") == "wordle unlimited"
    assert S.extract_keyword("https://x.com/en/game/among-us") == "among us"
    assert S.extract_keyword("https://x.com/privacy") is None
    assert S.extract_keyword("https://x.com/games/12345") is None
    assert S.extract_keyword("https://x.com/") is None


def test_parse_sitemap_dedupes_and_orders():
    text = ("<urlset><url><loc>https://a/1</loc></url>"
            "<url><loc>https://a/2</loc></url>"
            "<url><loc>https://a/1</loc></url></urlset>")
    assert S.parse_sitemap(text) == ["https://a/1", "https://a/2"]
    assert S.parse_sitemap("") == []


def test_fetch_sitemap_follows_index():
    pages = {
        "https://x/sitemap.xml": "<sitemapindex><sitemap><loc>https://x/s1.xml</loc></sitemap></sitemapindex>",
        "https://x/s1.xml": "<urlset><url><loc>https://x/games/a-b</loc></url></urlset>",
    }
    out = S.fetch_sitemap(_Session(pages), "https://x/sitemap.xml", throttle=0)
    assert out == ["https://x/games/a-b"]


def test_export_items_aggregates_domains():
    now = datetime.now(timezone.utc).isoformat()
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    watches = {
        "https://a.com/sitemap.xml": {"host": "a.com",
                                      "urls": {"https://a.com/games/wordle-unlimited": recent}},
        "https://b.com/sitemap.xml": {"host": "b.com",
                                      "urls": {"https://b.com/games/wordle-unlimited": old}},
    }
    out = S.export_items(watches, now, min_words=1)
    item = next(i for i in out["items"] if i["q"] == "wordle unlimited")
    assert item["domain_count"] == 2
    assert item["domain_count_7d"] == 1
    assert item["domain_count_30d"] == 1
    assert item["new"] is False  # earliest sighting is 40d old
    assert out["watch_count"] == 2


def test_export_items_marks_fresh_keyword_new():
    def hours(h):
        return (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat()

    now = datetime.now(timezone.utc).isoformat()
    watches = {
        "https://a.com/sitemap.xml": {"host": "a.com",
                                      "urls": {"https://a.com/games/among-us": hours(3)}},
    }
    item = S.export_items(watches, now, min_words=1)["items"][0]
    assert item["new"] is True
    assert item["domain_count_7d"] == 1


def test_run_aggregates_and_exports(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "build_session", lambda proxy: object())
    monkeypatch.setattr(S, "fetch_sitemap", lambda session, url, **k: ["https://a.com/games/among-us"])
    monkeypatch.setattr(S, "enrich", lambda items, **k: items)
    n = S.run(proxy=None, watch_urls=["https://a.com/sitemap.xml"],
              out_dir=tmp_path, state_path=tmp_path / "st.json", throttle=0)
    assert n == 1
    data = json.loads((tmp_path / "sitemap.json").read_text(encoding="utf-8"))
    assert data["items"][0]["q"] == "among us"
    st = json.loads((tmp_path / "st.json").read_text(encoding="utf-8"))
    assert "https://a.com/sitemap.xml" in st["watches"]


def test_run_no_watch_is_noop(tmp_path):
    assert S.run(proxy=None, watch_urls=[], out_dir=tmp_path, state_path=tmp_path / "s.json") == 0
    assert not (tmp_path / "sitemap.json").exists()


def test_host_reduces_to_registrable_domain():
    assert S._host("https://leafo.itch.io/x-moon") == "itch.io"
    assert S._host("https://www.silvergames.com/en/bowman") == "silvergames.com"
    assert S._host("https://www.bbc.co.uk/x") == "bbc.co.uk"


def test_run_baseline_then_detects_new(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "build_session", lambda proxy: object())
    monkeypatch.setattr(S, "enrich", lambda items, **k: items)
    pages = {"v": ["https://a.com/games/among-us"]}
    monkeypatch.setattr(S, "fetch_sitemap", lambda session, url, **k: list(pages["v"]))
    sp = tmp_path / "st.json"

    S.run(proxy=None, watch_urls=["https://a.com/sitemap.xml"], out_dir=tmp_path, state_path=sp, throttle=0)
    d1 = json.loads((tmp_path / "sitemap.json").read_text(encoding="utf-8"))
    assert d1["items"][0]["new"] is False       # baseline snapshot is not "new"
    assert d1["items"][0]["baseline"] is True
    assert d1["items"][0]["first"] is None      # no fake "first seen" date

    pages["v"].append("https://a.com/games/wordle-unlimited")
    S.run(proxy=None, watch_urls=["https://a.com/sitemap.xml"], out_dir=tmp_path, state_path=sp, throttle=0)
    d2 = json.loads((tmp_path / "sitemap.json").read_text(encoding="utf-8"))
    fresh = next(i for i in d2["items"] if i["q"] == "wordle unlimited")
    assert fresh["new"] is True
    assert fresh["domain_count"] == 1
