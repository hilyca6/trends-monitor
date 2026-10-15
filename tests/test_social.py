from collector.gameradar import social as S


def _p(**kw):
    base = {"configured": True, "checkedAt": "2026-10-11T00:00:00+00:00"}
    base.update(kw)
    return base


def test_provider_score_functions():
    assert S.youtube_score({"totalViews": 250000, "channelCount": 8, "videoCount": 10, "recent24h": 5}) >= 28 + 12 + 8 + 8
    assert S.reddit_score({"postCount": 10, "subredditCount": 4, "totalScore": 1000, "recent24h": 3}) >= 14 + 6 + 10 + 5
    assert S.x_score({"posts7d": 1000, "posts24h": 100}) >= 18 + 6
    assert S.tiktok_score({"totalViews": 1000000, "videoCount": 20, "totalLikes": 50000}) >= 22 + 8 + 8


def test_two_platform_spillover_unlocks_independent():
    providers = {
        "youtube": _p(totalViews=150000, channelCount=5, videoCount=8, totalLikes=2000, totalComments=500, recent24h=3),
        "reddit": _p(postCount=10, subredditCount=5, authorCount=8, totalScore=800, totalComments=200, recent24h=4),
        "x": {"configured": False},
        "tiktok": {"configured": False},
    }
    out = S.analyze(providers)
    assert out["hasExternalSpillover"] is True
    assert out["classification"] in ("pass", "viral")
    assert out["allowsIndependent"] is True
    assert out["independentCreatorCount"] >= 4


def test_single_platform_counts_when_min_platforms_is_one():
    providers = {
        "youtube": _p(totalViews=150000, channelCount=5, videoCount=8, totalLikes=300, recent24h=5),
        "reddit": {"configured": False},
        "x": {"configured": False},
        "tiktok": {"configured": False},
    }
    strict = S.analyze(providers)
    assert strict["hasExternalSpillover"] is False  # default needs 2 platforms

    relaxed = S.analyze(providers, min_platforms=1)
    assert relaxed["hasExternalSpillover"] is True
    assert relaxed["classification"] in ("pass", "viral")
    assert relaxed["allowsIndependent"] is True
    assert relaxed["minPlatforms"] == 1


def test_single_platform_is_momentum_not_spillover():
    providers = {
        "youtube": _p(totalViews=20000, channelCount=3, videoCount=6, recent24h=2),
        "reddit": {"configured": False},
        "x": {"configured": False},
        "tiktok": {"configured": False},
    }
    out = S.analyze(providers)
    assert out["hasExternalSpillover"] is False
    assert out["hasSinglePlatformMomentum"] is True
    assert out["classification"] == "watch"


def test_no_providers_checked_is_pending():
    out = S.analyze({"youtube": {"configured": False}, "reddit": {"configured": False},
                     "x": {"configured": False}, "tiktok": {"configured": False}})
    assert out["classification"] == "pending"
    assert out["allowsTest"] is True
    assert out["allowsIndependent"] is False


def test_provider_error_is_not_treated_as_no_heat():
    providers = {
        "youtube": {"configured": True, "checkedAt": "2026-10-11T00:00:00+00:00", "error": "429"},
        "reddit": {"configured": False},
        "x": {"configured": False},
        "tiktok": {"configured": False},
    }
    out = S.analyze(providers, {"youtube": "429"})
    assert out["checkedProviders"] == []
    assert out["classification"] == "pending"
    assert any("失败" in r for r in out["reasons"])


def test_name_matches():
    assert S.name_matches("Let's play Neon Drift.io!", "neon drift.io")
    assert not S.name_matches("totally unrelated clip", "neon drift.io")


def test_collect_skips_unconfigured_providers():
    providers, errors = S.collect("Neon Drift.io", youtube_api_key="", reddit_client_id="", x_bearer_token="", tiktok_research_token="")
    assert providers["youtube"] == {"configured": False}
    assert providers["reddit"] == {"configured": False}
    assert providers["x"] == {"configured": False}
    assert providers["tiktok"] == {"configured": False}
    assert errors == {}


def test_collect_reuses_fresh_youtube():
    fresh = {"checkedAt": S._now_iso(), "videoCount": 3, "channelCount": 2, "totalViews": 1000}
    providers, _ = S.collect("Foo", youtube_api_key="KEY", existing_youtube=fresh)
    assert providers["youtube"]["configured"] is True
    assert providers["youtube"]["videoCount"] == 3
