"""Opportunity score + hard gates for game-radar candidates.

Ported from the game-name-radar ``opportunity-finalizer`` for the **online**
channel (browser games). Produces an explainable 0-100 score, a `test-now`
gate set and an `independent` gate set.

Social spillover (provider #4) is not wired yet, so ``socialSpread`` is 0 and
``allowsIndependent`` stays False until social data exists. ``allowsTest`` does
not depend on social and works today.

Data used (all optional; missing pieces simply score low):
  * ``seo.market.status`` / ``seo.nameRisk`` / ``seo.entityConflict``
  * ``signal.classification`` / ``signal.freshness`` / ``signal.qualifiedRatio``
  * ``fast.sourceAdded24h`` / ``fast.onlinePlatformCount`` / ``fast.classification``
"""

from __future__ import annotations

OPPORTUNITY_WEIGHTS = {
    "socialSpread": 0.30,
    "growthVelocity": 0.25,
    "searchFormation": 0.15,
    "contentExpandability": 0.15,
    "serpGap": 0.10,
    "nameSafety": 0.05,
}

_SEARCH_FORMATION = {
    "breakout": 100, "rising": 85, "strong": 70, "moderate": 55,
    "weak": 25, "none": 0, "pending": 15, "error": 0,
}
_SERP_GAP = {
    "greenfield": 100, "unconfirmed-new": 60, "unknown": 25,
    "contested": 15, "occupied": 0, "established": 0,
}


def _clamp(value) -> int:
    return max(0, min(100, round(float(value) or 0)))


def _search_formation(item: dict) -> int:
    signal = item.get("signal") or {}
    score = _SEARCH_FORMATION.get(signal.get("classification") or "pending", 15)
    if signal.get("freshness") == "new":
        score += 10
    if signal.get("freshness") == "existing":
        score = min(score, 25)
    return _clamp(score)


def _growth_velocity(item: dict) -> int:
    fast = item.get("fast") or {}
    return _clamp(
        int(fast.get("sourceAdded24h") or 0) * 25
        + min(30, int(fast.get("maxRankGain") or 0) * 3)
        + min(30, int(fast.get("onlinePlatformCount") or 0) * 15)
    )


def _content_expandability(item: dict) -> int:
    """Browser-game proxy for the "Play online / Controls" autocomplete demand.

    The original counts autocomplete long-tails; we use the Trends *qualified*
    demand (``{q} online`` / ``play {q}`` relative to the anchor) instead so no
    extra request is needed.
    """
    qr = (item.get("signal") or {}).get("qualifiedRatio") or 0
    if qr >= 0.05:
        return 100
    if qr >= 0.02:
        return 80
    if qr >= 0.01:
        return 55
    if qr > 0:
        return 30
    return 0


def analyze(item: dict) -> dict:
    seo = item.get("seo") or {}
    signal = item.get("signal") or {}
    fast = item.get("fast") or {}
    social = item.get("social") or {}
    market = seo.get("market") or {}
    name_risk = seo.get("nameRisk")
    name_risk = 30 if not isinstance(name_risk, (int, float)) else float(name_risk)
    entity_conflict = bool(seo.get("entityConflict") or signal.get("entityConflict"))
    status = market.get("status")

    components = {
        "socialSpread": _clamp(social.get("score") or 0),
        "growthVelocity": _growth_velocity(item),
        "searchFormation": _search_formation(item),
        "contentExpandability": _content_expandability(item),
        "serpGap": _SERP_GAP.get(status, 0),
        "nameSafety": 0 if entity_conflict else _clamp(100 - name_risk * 4),
    }
    score = round(sum(components[k] * w for k, w in OPPORTUNITY_WEIGHTS.items()))

    has_spillover = bool(social.get("hasExternalSpillover"))
    # Low competition alone is supply, not demand: cap until real evidence exists.
    if not has_spillover:
        score = min(score, 59)
    if status != "greenfield":
        score = min(score, 69)
    if signal.get("freshness") == "existing" or entity_conflict:
        score = min(score, 49)

    hard_gates = {
        "externalSpillover": has_spillover,
        "growthVelocity": components["growthVelocity"] >= 45,
        "searchFormation": components["searchFormation"] >= 70,
        "contentDepth": components["contentExpandability"] >= 55,
        "competitionOpen": status == "greenfield",
        "nameSafe": not entity_conflict and name_risk <= 18,
    }
    allows_independent = all(hard_gates.values()) and score >= 70

    test_gates = {
        "growthVelocity": components["growthVelocity"] >= 35 or fast.get("classification") == "pass",
        "searchFormation": components["searchFormation"] >= 55,
        "contentDepth": components["contentExpandability"] >= 30,
        "competitionTestable": status in ("greenfield", "unconfirmed-new"),
        "nameSafe": not entity_conflict and name_risk <= 20,
    }
    # Test score deliberately excludes social: a missing provider must not look
    # like "no heat".
    test_score = round(
        components["growthVelocity"] * 0.35
        + components["searchFormation"] * 0.25
        + components["contentExpandability"] * 0.15
        + components["serpGap"] * 0.15
        + components["nameSafety"] * 0.10
    )
    allows_test = all(test_gates.values()) and test_score >= 60

    reasons: list[str] = []
    if not hard_gates["externalSpillover"]:
        reasons.append("缺少多平台、多独立创作者的外溢传播")
    if not hard_gates["growthVelocity"]:
        reasons.append("24–48小时平台扩散速度不足")
    if not hard_gates["searchFormation"]:
        reasons.append("搜索需求尚未形成")
    if not hard_gates["contentDepth"]:
        reasons.append("可扩展内容主题不足")
    if not hard_gates["competitionOpen"]:
        reasons.append("SERP尚未确认是严格空白市场")
    if not hard_gates["nameSafe"]:
        reasons.append("名称歧义或实体冲突风险过高")

    decision = "watch"
    seo_cls = seo.get("classification")
    if seo_cls in ("reject", "error") or entity_conflict:
        decision = "reject"
    elif allows_independent:
        decision = "independent"
    elif allows_test:
        decision = "test-now"

    return {
        "modelVersion": 1,
        "checkedAt": None,  # filled by caller
        "channel": "online",
        "weights": OPPORTUNITY_WEIGHTS,
        "components": components,
        "score": score,
        "hardGates": hard_gates,
        "testGates": test_gates,
        "testScore": test_score,
        "allowsTest": allows_test,
        "allowsIndependent": allows_independent,
        "socialMeasured": bool(social.get("hasMeasuredEvidence")),
        "decision": decision,
        "reasons": reasons,
    }
