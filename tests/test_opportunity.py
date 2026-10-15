from collector.gameradar import opportunity as O


def _item(**kw):
    base = {"name": "Foo", "seo": {}, "signal": {}, "fast": {}}
    base.update(kw)
    return base


def test_strong_greenfield_passes_test_but_not_independent_without_social():
    item = _item(
        seo={"classification": "independent", "nameRisk": 5,
             "market": {"status": "greenfield"}, "entityConflict": False},
        signal={"classification": "breakout", "freshness": "new", "qualifiedRatio": 0.03},
        fast={"classification": "pass", "sourceAdded24h": 2, "onlinePlatformCount": 2},
    )
    opp = O.analyze(item)
    assert opp["allowsTest"] is True
    assert opp["decision"] == "test-now"
    assert opp["allowsIndependent"] is False  # social (#4) not wired yet


def test_entity_conflict_zeroes_name_safety_and_rejects():
    item = _item(
        seo={"classification": "independent", "nameRisk": 5,
             "market": {"status": "greenfield"}, "entityConflict": True},
        signal={"classification": "breakout", "freshness": "new", "qualifiedRatio": 0.03},
        fast={"classification": "pass", "sourceAdded24h": 2, "onlinePlatformCount": 2},
    )
    opp = O.analyze(item)
    assert opp["components"]["nameSafety"] == 0
    assert opp["decision"] == "reject"
    assert opp["allowsTest"] is False


def test_weak_candidate_is_watch():
    item = _item(
        seo={"classification": "watch", "nameRisk": 20, "market": {"status": "unknown"}},
        signal={"classification": "weak", "freshness": "existing"},
        fast={},
    )
    opp = O.analyze(item)
    assert opp["decision"] == "watch"
    assert opp["allowsTest"] is False


def test_social_spillover_unlocks_independent():
    item = _item(
        seo={"classification": "independent", "nameRisk": 5,
             "market": {"status": "greenfield"}, "entityConflict": False},
        signal={"classification": "breakout", "freshness": "new", "qualifiedRatio": 0.05},
        fast={"classification": "pass", "sourceAdded24h": 3, "onlinePlatformCount": 3},
        social={"score": 80, "hasExternalSpillover": True, "hasMeasuredEvidence": True},
    )
    opp = O.analyze(item)
    assert opp["allowsIndependent"] is True
    assert opp["decision"] == "independent"
    assert opp["socialMeasured"] is True


def test_occupied_market_caps_score():
    item = _item(
        seo={"classification": "page", "nameRisk": 8, "market": {"status": "occupied"}},
        signal={"classification": "rising", "freshness": "new", "qualifiedRatio": 0.02},
        fast={"classification": "pass", "sourceAdded24h": 2, "onlinePlatformCount": 2},
    )
    opp = O.analyze(item)
    assert opp["score"] <= 69
    assert opp["decision"] != "independent"
