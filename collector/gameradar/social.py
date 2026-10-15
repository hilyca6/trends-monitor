"""Social spillover verification for game-radar candidates.

Ported from the game-name-radar ``social-buzz`` model + its provider collectors.

Providers (all optional — a provider without credentials is skipped, never
failing the run):
    youtube   YOUTUBE key (shared with the game radar's own YouTube pass)
    reddit    REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET (+ USER_AGENT)
    x         X_BEARER_TOKEN
    tiktok    TIKTOK_RESEARCH_TOKEN

The external-spillover rule needs **at least two platforms**, **at least four
independent creators** and **at least one platform with 24h activity**. A single
platform, even a strong one, only gives `hasSinglePlatformMomentum`.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from datetime import datetime, timezone

import requests

from . import youtube

log = logging.getLogger("trends.gameradar.social")

SOCIAL_MODEL_VERSION = 3
DAY_MS = 86_400_000
_WORD_RE = re.compile(r"[^a-z0-9]+")

PROVIDERS = ("youtube", "reddit", "x", "tiktok")

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _normalize(value: str) -> str:
    return _WORD_RE.sub(" ", (value or "").lower()).strip()


def name_matches(text: str, name: str) -> bool:
    target = _normalize(name)
    return len(target) >= 3 and target in _normalize(text)


def _number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _configured(provider: dict) -> bool:
    return bool(provider and provider.get("configured"))


def _checked(provider: dict) -> bool:
    return bool(provider and provider.get("checkedAt") and not provider.get("error"))


# ---------------------------------------------------------------------------
# scoring (pure) — mirrors lib/social-buzz.mjs
# ---------------------------------------------------------------------------


def youtube_score(d: dict) -> int:
    views, likes, comments = _number(d.get("totalViews")), _number(d.get("totalLikes")), _number(d.get("totalComments"))
    videos, channels, recent = _number(d.get("videoCount")), _number(d.get("channelCount")), _number(d.get("recent24h"))
    s = 0
    if views >= 250000:
        s += 28
    elif views >= 100000:
        s += 24
    elif views >= 20000:
        s += 18
    elif views >= 5000:
        s += 10
    elif views >= 1000:
        s += 5
    if channels >= 8:
        s += 12
    elif channels >= 4:
        s += 8
    elif channels >= 2:
        s += 4
    if videos >= 10:
        s += 8
    elif videos >= 5:
        s += 5
    elif videos >= 2:
        s += 2
    if recent >= 5:
        s += 8
    elif recent >= 2:
        s += 4
    engagement = likes + comments
    if engagement >= 10000:
        s += 6
    elif engagement >= 2000:
        s += 4
    elif engagement >= 300:
        s += 2
    return s


def reddit_score(d: dict) -> int:
    posts, subs, recent = _number(d.get("postCount")), _number(d.get("subredditCount")), _number(d.get("recent24h"))
    engagement = _number(d.get("totalScore")) + _number(d.get("totalComments"))
    s = 0
    if posts >= 10:
        s += 14
    elif posts >= 5:
        s += 10
    elif posts >= 2:
        s += 5
    if subs >= 4:
        s += 6
    elif subs >= 2:
        s += 3
    if engagement >= 1000:
        s += 10
    elif engagement >= 300:
        s += 7
    elif engagement >= 50:
        s += 4
    if recent >= 3:
        s += 5
    elif recent >= 1:
        s += 2
    return s


def x_score(d: dict) -> int:
    posts7d, posts24h = _number(d.get("posts7d")), _number(d.get("posts24h"))
    s = 0
    if posts7d >= 1000:
        s += 18
    elif posts7d >= 300:
        s += 14
    elif posts7d >= 100:
        s += 10
    elif posts7d >= 30:
        s += 6
    elif posts7d >= 10:
        s += 3
    if posts24h >= 100:
        s += 6
    elif posts24h >= 30:
        s += 4
    elif posts24h >= 10:
        s += 2
    return s


def tiktok_score(d: dict) -> int:
    views = _number(d.get("totalViews"))
    videos = _number(d.get("videoCount"))
    engagement = _number(d.get("totalLikes")) + _number(d.get("totalComments")) + _number(d.get("totalShares"))
    s = 0
    if views >= 1000000:
        s += 22
    elif views >= 250000:
        s += 17
    elif views >= 50000:
        s += 12
    elif views >= 10000:
        s += 6
    elif views >= 2000:
        s += 3
    if videos >= 20:
        s += 8
    elif videos >= 8:
        s += 5
    elif videos >= 3:
        s += 2
    if engagement >= 50000:
        s += 8
    elif engagement >= 10000:
        s += 5
    elif engagement >= 1000:
        s += 2
    return s


def independent_creator_count(name: str, d: dict) -> int:
    if name == "youtube":
        return int(_number(d.get("channelCount")))
    if name in ("reddit", "x"):
        return int(_number(d.get("authorCount")))
    if name == "tiktok":
        return int(_number(d.get("creatorCount")))
    return 0


def recent24h_count(name: str, d: dict) -> int:
    if name == "x":
        return int(_number(d.get("posts24h")))
    return int(_number(d.get("recent24h")))


def strong_provider(name: str, d: dict) -> bool:
    if name == "youtube":
        return (_number(d.get("totalViews")) >= 20000 and _number(d.get("channelCount")) >= 3) or (
            _number(d.get("totalViews")) >= 5000 and _number(d.get("videoCount")) >= 5
        )
    if name == "reddit":
        return _number(d.get("postCount")) >= 3 and _number(d.get("totalScore")) + _number(d.get("totalComments")) >= 50
    if name == "x":
        return _number(d.get("posts7d")) >= 50
    if name == "tiktok":
        return _number(d.get("totalViews")) >= 50000 and _number(d.get("videoCount")) >= 3
    return False


def very_strong_provider(name: str, d: dict) -> bool:
    if name == "youtube":
        return _number(d.get("totalViews")) >= 100000 and _number(d.get("channelCount")) >= 4
    if name == "reddit":
        return _number(d.get("postCount")) >= 8 and _number(d.get("totalScore")) + _number(d.get("totalComments")) >= 500
    if name == "x":
        return _number(d.get("posts7d")) >= 300
    if name == "tiktok":
        return _number(d.get("totalViews")) >= 250000 and _number(d.get("videoCount")) >= 5
    return False


def _is_active(name: str, d: dict) -> bool:
    if name == "youtube":
        return _number(d.get("videoCount")) > 0 or _number(d.get("totalViews")) > 0
    if name == "reddit":
        return _number(d.get("postCount")) > 0
    if name == "x":
        return _number(d.get("posts7d")) > 0
    if name == "tiktok":
        return _number(d.get("videoCount")) > 0 or _number(d.get("totalViews")) > 0
    return False


# ---------------------------------------------------------------------------
# provider collectors
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fresh(checked_at: str | None, max_age_ms: float, now_ms: float) -> bool:
    if not checked_at:
        return False
    try:
        t = datetime.fromisoformat(checked_at).timestamp() * 1000
    except (ValueError, TypeError):
        return False
    return (now_ms - t) <= max_age_ms


_REDDIT_TOKEN: dict = {"value": None, "exp": 0.0}


def _reddit_token(client_id: str, client_secret: str, user_agent: str, timeout: float) -> str:
    now = time.time()
    if _REDDIT_TOKEN["value"] and _REDDIT_TOKEN["exp"] > now:
        return _REDDIT_TOKEN["value"]
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        "https://www.reddit.com/api/v1/access_token",
        headers={"Authorization": f"Basic {auth}", "User-Agent": user_agent},
        data={"grant_type": "client_credentials"},
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Reddit token {resp.status_code}")
    payload = resp.json()
    if not payload.get("access_token"):
        raise RuntimeError("Reddit token missing")
    _REDDIT_TOKEN["value"] = payload["access_token"]
    _REDDIT_TOKEN["exp"] = now + max(60, int(payload.get("expires_in") or 3600) - 60)
    return _REDDIT_TOKEN["value"]


def query_reddit(name: str, client_id: str, client_secret: str, user_agent: str, timeout: float) -> dict:
    token = _reddit_token(client_id, client_secret, user_agent, timeout)
    resp = requests.get(
        "https://oauth.reddit.com/search",
        params={"q": f'"{name}"', "sort": "new", "t": "week", "type": "link", "limit": "25", "raw_json": "1"},
        headers={"Authorization": f"Bearer {token}", "User-Agent": user_agent},
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Reddit search {resp.status_code}")
    children = (resp.json().get("data") or {}).get("children") or []
    posts = [c.get("data", {}) for c in children]
    posts = [p for p in posts if name_matches(f"{p.get('title', '')} {p.get('selftext', '')}", name)]
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    return {
        "configured": True,
        "checkedAt": _now_iso(),
        "postCount": len(posts),
        "subredditCount": len({p.get("subreddit") for p in posts if p.get("subreddit")}),
        "authorCount": len({p.get("author") for p in posts if p.get("author")}),
        "totalScore": sum(int(_number(p.get("score"))) for p in posts),
        "totalComments": sum(int(_number(p.get("num_comments"))) for p in posts),
        "recent24h": sum(1 for p in posts if now_ms - _number(p.get("created_utc")) * 1000 <= DAY_MS),
    }


def query_x(name: str, bearer: str, timeout: float) -> dict:
    resp = requests.get(
        "https://api.x.com/2/tweets/counts/recent",
        params={"query": f'"{name}" lang:en -is:retweet', "granularity": "hour"},
        headers={"Authorization": f"Bearer {bearer}"},
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"X recent counts {resp.status_code}")
    payload = resp.json()
    buckets = payload.get("data") or []
    cutoff = datetime.now(timezone.utc).timestamp() * 1000 - DAY_MS
    posts7d = (payload.get("meta") or {}).get("total_tweet_count")
    if posts7d is None:
        posts7d = sum(int(_number(b.get("tweet_count"))) for b in buckets)
    posts24h = 0
    for b in buckets:
        try:
            end = datetime.fromisoformat(str(b.get("end")).replace("Z", "+00:00")).timestamp() * 1000
        except (ValueError, TypeError):
            continue
        if end >= cutoff:
            posts24h += int(_number(b.get("tweet_count")))
    return {"configured": True, "checkedAt": _now_iso(), "posts7d": int(posts7d), "posts24h": posts24h}


def query_tiktok(name: str, token: str, timeout: float) -> dict:
    fields = "id,video_description,create_time,view_count,like_count,comment_count,share_count,username"
    now = datetime.now(timezone.utc)
    body = {
        "query": {"and": [
            {"operation": "IN", "field_name": "region_code", "field_values": ["US", "CA", "GB", "AU"]},
            {"operation": "EQ", "field_name": "keyword", "field_values": [name]},
        ]},
        "start_date": (now - _timedelta_days(7)).strftime("%Y%m%d"),
        "end_date": now.strftime("%Y%m%d"),
        "max_count": 100,
        "cursor": 0,
        "is_random": False,
    }
    resp = requests.post(
        f"https://open.tiktokapis.com/v2/research/video/query/?fields={fields}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body,
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"TikTok research {resp.status_code}")
    payload = resp.json()
    err = payload.get("error") or {}
    if err.get("code") and err["code"] != "ok":
        raise RuntimeError(f"TikTok {err.get('message') or err['code']}")
    videos = [v for v in ((payload.get("data") or {}).get("videos") or [])
              if name_matches(v.get("video_description") or name, name)]
    now_ms = now.timestamp() * 1000
    return {
        "configured": True,
        "checkedAt": _now_iso(),
        "videoCount": len(videos),
        "creatorCount": len({v.get("username") for v in videos if v.get("username")}),
        "totalViews": sum(int(_number(v.get("view_count"))) for v in videos),
        "totalLikes": sum(int(_number(v.get("like_count"))) for v in videos),
        "totalComments": sum(int(_number(v.get("comment_count"))) for v in videos),
        "totalShares": sum(int(_number(v.get("share_count"))) for v in videos),
        "recent24h": sum(1 for v in videos if now_ms - _number(v.get("create_time")) * 1000 <= DAY_MS),
    }


def _timedelta_days(days: int):
    from datetime import timedelta

    return timedelta(days=days)


def collect(
    name: str,
    *,
    youtube_api_key: str = "",
    reddit_client_id: str = "",
    reddit_client_secret: str = "",
    reddit_user_agent: str = "trendradar/1.0",
    x_bearer_token: str = "",
    tiktok_research_token: str = "",
    timeout: float = 20,
    throttle: float = 0.0,
    existing_youtube: dict | None = None,
    youtube_max_age_ms: float = 6 * 3600 * 1000,
) -> tuple[dict, dict]:
    """Query every configured provider for one game name.

    Returns ``(providers, provider_errors)``. A provider that errors is recorded
    as checked-with-error (not as "no heat"). YouTube reuses the game radar's
    fresh data to avoid double-spending quota.
    """
    providers: dict = {}
    errors: dict = {}
    now_ms = datetime.now(timezone.utc).timestamp() * 1000

    # YouTube (reuse fresh game-radar data if available)
    if youtube_api_key:
        if existing_youtube and _fresh(existing_youtube.get("checkedAt"), youtube_max_age_ms, now_ms):
            providers["youtube"] = {**existing_youtube, "configured": True}
        else:
            try:
                res = youtube.verify_youtube(name, youtube_api_key, timeout=int(timeout), throttle=throttle)
                providers["youtube"] = {**res, "configured": True} if res else {"configured": False}
            except Exception as e:  # noqa: BLE001
                errors["youtube"] = str(e)[:200]
                providers["youtube"] = {"configured": True, "checkedAt": _now_iso(), "error": errors["youtube"]}
    else:
        providers["youtube"] = {"configured": False}

    # Reddit
    if reddit_client_id and reddit_client_secret:
        try:
            providers["reddit"] = query_reddit(name, reddit_client_id, reddit_client_secret, reddit_user_agent, timeout)
        except Exception as e:  # noqa: BLE001
            errors["reddit"] = str(e)[:200]
            providers["reddit"] = {"configured": True, "checkedAt": _now_iso(), "error": errors["reddit"]}
    else:
        providers["reddit"] = {"configured": False}

    # X
    if x_bearer_token:
        try:
            providers["x"] = query_x(name, x_bearer_token, timeout)
        except Exception as e:  # noqa: BLE001
            errors["x"] = str(e)[:200]
            providers["x"] = {"configured": True, "checkedAt": _now_iso(), "error": errors["x"]}
    else:
        providers["x"] = {"configured": False}

    # TikTok
    if tiktok_research_token:
        try:
            providers["tiktok"] = query_tiktok(name, tiktok_research_token, timeout)
        except Exception as e:  # noqa: BLE001
            errors["tiktok"] = str(e)[:200]
            providers["tiktok"] = {"configured": True, "checkedAt": _now_iso(), "error": errors["tiktok"]}
    else:
        providers["tiktok"] = {"configured": False}

    return providers, errors


def analyze(
    providers: dict,
    provider_errors: dict | None = None,
    checked_at: str | None = None,
    min_platforms: int = 2,
) -> dict:
    """Turn a providers dict into a social-spillover verdict.

    ``min_platforms`` is how many independent platforms a spillover needs. The
    default 2 is the strict rule; lower it to 1 to accept a strong single
    platform (more false positives).
    """
    min_platforms = max(1, int(min_platforms or 2))
    providers = providers or {}
    provider_errors = provider_errors or {}
    configured = [n for n in PROVIDERS if _configured(providers.get(n))]
    checked = [n for n in PROVIDERS if _checked(providers.get(n))]
    active = [n for n in checked if _is_active(n, providers[n])]
    strong = [n for n in active if strong_provider(n, providers[n])]
    very_strong = [n for n in active if very_strong_provider(n, providers[n])]
    independent = [n for n in active if independent_creator_count(n, providers[n]) >= 2]
    independent_creators = sum(independent_creator_count(n, providers[n]) for n in independent)
    fresh = [n for n in active if recent24h_count(n, providers[n]) > 0]

    has_external_spillover = len(independent) >= min_platforms and independent_creators >= 4 and len(fresh) >= 1
    single = next(
        (n for n in active
         if strong_provider(n, providers[n])
         and independent_creator_count(n, providers[n]) >= 3
         and recent24h_count(n, providers[n]) > 0),
        None,
    )

    score = (
        youtube_score(providers.get("youtube") or {})
        + reddit_score(providers.get("reddit") or {})
        + x_score(providers.get("x") or {})
        + tiktok_score(providers.get("tiktok") or {})
    )
    if len(active) >= 3:
        score += 12
    elif len(active) >= 2:
        score += 7
    score = max(0, min(100, score))

    classification = "pending"
    if checked:
        classification = "weak"
        if score >= 75 and len(strong) >= min_platforms and has_external_spillover:
            classification = "viral"
        elif score >= 45 and len(strong) >= min_platforms and has_external_spillover:
            classification = "pass"
        elif single or score >= 20 or len(active) >= 2:
            classification = "watch"

    reasons: list[str] = []
    yt = providers.get("youtube") or {}
    rd = providers.get("reddit") or {}
    xx = providers.get("x") or {}
    tk = providers.get("tiktok") or {}
    if _number(yt.get("videoCount")) > 0:
        reasons.append(f"YouTube近7天{int(_number(yt.get('videoCount')))}个视频／{int(_number(yt.get('channelCount')))}个频道／{int(_number(yt.get('totalViews'))):,}播放")
    if _number(yt.get("recent24h")) > 0:
        reasons.append(f"YouTube近24小时新增{int(_number(yt.get('recent24h')))}个相关视频")
    if _number(rd.get("postCount")) > 0:
        reasons.append(f"Reddit近7天{int(_number(rd.get('postCount')))}个帖子／{int(_number(rd.get('subredditCount')))}个社区")
    if _number(xx.get("posts7d")) > 0:
        reasons.append(f"X近7天约{int(_number(xx.get('posts7d'))):,}条提及，近24小时{int(_number(xx.get('posts24h'))):,}条")
    if _number(tk.get("videoCount")) > 0:
        reasons.append(f"TikTok近7天{int(_number(tk.get('videoCount')))}个视频／{int(_number(tk.get('totalViews'))):,}播放")
    if len(active) >= 2:
        reasons.append(f"已在{len(active)}个社媒平台形成传播")
    if has_external_spillover:
        reasons.append(f"至少{len(independent)}个平台、{independent_creators}个独立创作者形成外溢传播")
    elif single:
        reasons.append(f"{single}单平台已出现多个独立创作者且24小时仍在新增，可用于低成本测试但不足以直接确认独立站")
    elif active:
        reasons.append("尚未验证多个平台与多个独立创作者的传播")
    elif checked:
        reasons.append("已检查社媒，但暂未发现明显宣传或玩家讨论")
    if not checked and provider_errors:
        reasons.append("社媒Provider本轮验证失败，按未知处理而不是按无热度处理")
    elif not checked:
        reasons.append("尚未完成社媒热度验证")

    return {
        "modelVersion": SOCIAL_MODEL_VERSION,
        "checkedAt": checked_at or datetime.now(timezone.utc).isoformat(),
        "score": score,
        "classification": classification,
        "confidence": "high" if len(checked) >= 3 else ("medium" if checked else "low"),
        "providers": providers,
        "configuredProviders": configured,
        "checkedProviders": checked,
        "activeProviders": active,
        "strongProviders": strong,
        "veryStrongProviders": very_strong,
        "independentProviders": independent,
        "independentCreatorCount": independent_creators,
        "freshProviders": fresh,
        "crossPlatformCount": len(active),
        "minPlatforms": min_platforms,
        "hasExternalSpillover": has_external_spillover,
        "hasSinglePlatformMomentum": bool(single),
        "singlePlatformMomentum": single,
        "hasMeasuredEvidence": bool(checked),
        "allowsTest": has_external_spillover or bool(single) or not checked,
        "allowsIndependent": classification in ("pass", "viral") and has_external_spillover,
        "reasons": reasons,
        "providerErrors": provider_errors,
    }
