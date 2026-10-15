"""Generate demo data so the frontend can be viewed/tested offline."""

from __future__ import annotations

import json
from pathlib import Path

from .config import CATEGORY_NAMES_ZH
from .store import now_iso, save_json


def _save_demo(path: Path, obj) -> None:
    """Write demo data, but never clobber an existing non-empty real file."""
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing:
                return
        except (ValueError, OSError):
            pass
    save_json(path, obj)

_DEMO = {
    "US": [
        ("ai website builder", "AI 建站工具", 500000, 1000, [18], ""),
        ("voice cloning app", "声音克隆应用", 200000, 500, [18], ""),
        ("pickleball paddle", "匹克球球拍", 100000, 200, [16], ""),
        ("cortisol detox", "皮质醇排毒", 50000, 300, [7], ""),
        ("nba scores", "NBA 比分", 500000, 1000, [17], "比赛"),
        ("susan dell", "苏珊·戴尔(戴尔CEO妻子)", 200000, 1000, [4], "人物"),
        ("hurricane watch", "飓风警戒", 100000, 1000, [20], "天气"),
        ("sleep gummies", "助眠软糖", 20000, 150, [7], ""),
    ],
    "GB": [
        ("heat pump grant", "热泵补贴", 50000, 400, [3], ""),
        ("matcha powder", "抹茶粉", 20000, 250, [5], ""),
        ("premier league fixtures", "英超赛程", 200000, 1000, [17], "比赛"),
        ("electric car tax", "电动车税", 50000, 300, [1], ""),
    ],
    "JP": [
        ("ai 画像 生成", "AI 头像生成", 100000, 800, [18], ""),
        ("ふるさと納税 おすすめ", "故乡税推荐", 50000, 200, [3], ""),
        ("プロ野球 結果", "职业棒球赛果", 200000, 1000, [17], "比赛"),
        ("新 NISA 銘柄", "新NISA标的", 100000, 500, [3], ""),
    ],
    "DE": [
        ("waermepumpe foerderung", "热泵补贴", 50000, 350, [3], ""),
        ("balkonkraftwerk", "阳台光伏", 100000, 600, [18], ""),
        ("bundesliga ergebnisse", "德甲赛果", 200000, 1000, [17], "比赛"),
    ],
    "IN": [
        ("best mutual funds", "最佳共同基金", 200000, 300, [3], ""),
        ("ai course free", "免费AI课程", 100000, 700, [9], ""),
        ("cricket live score", "板球实时比分", 500000, 1000, [17], "比赛"),
    ],
    "BR": [
        ("resultados das eleicoes", "选举结果", 10000000, 1000, [14], ""),
        ("receita de bolo", "蛋糕食谱", 200000, 200, [5], ""),
        ("flamengo x palmeiras", "弗拉门戈对帕尔梅拉斯", 500000, 1000, [17], "比赛"),
    ],
}

_DEMO_RELATED = {
    "ai website builder": ["best ai website builder for small business", "ai website builder free"],
    "voice cloning app": ["ai voice cloning free", "voice cloning app for android"],
    "pickleball paddle": ["best pickleball paddle for beginners", "pickleball paddle weight guide"],
    "cortisol detox": ["cortisol detox drink", "how to lower cortisol naturally"],
    "sleep gummies": ["best sleep gummies", "sleep gummies for adults"],
    "heat pump grant": ["heat pump grant application", "heat pump grant eligibility"],
    "matcha powder": ["best matcha powder", "matcha powder benefits"],
    "balkonkraftwerk": ["balkonkraftwerk anmelden", "balkonkraftwerk speicher"],
    "best mutual funds": ["best mutual funds for 2026", "best index funds"],
    "ai course free": ["free ai course with certificate", "ai course for beginners"],
    "receita de bolo": ["receita de bolo de chocolate", "bolo simples"],
}

_SAMPLE_ADVICE = {
    "audience": "想用 AI 提效的创作者 / 小商家",
    "directions": ["工具对比评测页", "免费工具导航", "上手教程站"],
    "main_keyword": "best ai tool",
    "monetize": ["工具 affiliate 佣金", "广告", "付费教程"],
    "risk": "同类站多、更新快，依赖单一工具政策",
    "evidence": "近 7 天热度上升、rising 相关词增长、首页有 reddit",
    "window": "2-4 周（上升期）",
    "level": "测试",
    "market": {"status": "greenfield", "confidence": "medium", "reason": "未发现专站或攻略竞争站占位"},
    "nameRisk": 6,
    "opportunities": [
        {"query": "best ai tool", "intent": "landing", "priority": 61},
        {"query": "best ai tool how to", "intent": "guide", "priority": 50},
        {"query": "best ai tool vs", "intent": "compare", "priority": 45},
    ],
}

_SIGNAL_CLASSES = ("weak", "moderate", "strong", "rising", "breakout")
_SIGNAL_LABELS = ("需求较弱", "需求中等", "需求较强", "明显上涨", "爆发")
_SIG_CATS = ("ai", "tools", "gaming", "finance", "health")


def write_demo(out_dir: Path) -> None:
    ts = now_iso()
    items = {}
    history = []
    for geo, rows in _DEMO.items():
        items[geo] = []
        for i, (q, zh, vol, growth, cats, noise) in enumerate(rows):
            row = {
                "q": q,
                "zh": zh,
                "vol": vol,
                "growth": growth,
                "cats": cats,
                "noise": noise,
                "new": i % 4 == 0,
            }
            rising = _DEMO_RELATED.get(q)
            if rising and not noise:
                row["related"] = {"rising": list(rising), "top": []}
            if not noise:
                row["advice"] = dict(_SAMPLE_ADVICE, stars=3 + (i % 3))
                row["cat"] = _SIG_CATS[i % len(_SIG_CATS)]
                row["trigger"] = "新发布" if i % 3 == 0 else ""
                row["signal"] = {
                    "ratio": round(0.5 + (i % 5) * 0.8, 2),
                    "qualifiedRatio": round(0.3 + (i % 4) * 0.7, 2),
                    "classification": _SIGNAL_CLASSES[i % 5],
                    "label": _SIGNAL_LABELS[i % 5],
                    "freshness": "new" if i % 2 == 0 else "existing",
                    "entityConflict": i % 5 == 4,
                    "score": 30 + (i % 5) * 12,
                }
                row["fast"] = {
                    "score": (i % 4) * 20,
                    "classification": ["weak", "watch", "pass", "pass"][i % 4],
                    "newSuggestionCount": i % 3,
                    "newSerpPageCount": (i + 1) % 3,
                }
            items[geo].append(row)
            history.append(
                {
                    "q": q,
                    "zh": zh,
                    "geo": geo,
                    "cats": cats,
                    "vol_peak": vol,
                    "growth_peak": growth,
                    "first": ts,
                    "last": ts,
                    "noise": noise,
                }
            )
    history.sort(key=lambda r: r["vol_peak"], reverse=True)

    _save_demo(
        out_dir / "trends.json",
        {
            "updated": ts,
            "geos": list(_DEMO.keys()),
            "cats": {str(k): v for k, v in CATEGORY_NAMES_ZH.items()},
            "items": items,
        },
    )
    _save_demo(out_dir / "history.json", {"updated": ts, "chunks": 1, "items": history})
    _save_demo(out_dir / "games.json", {"updated": ts, "items": _demo_games(ts)})
    _save_demo(
        out_dir / "gameradar.json",
        {"updated": ts, "items": _demo_gameradar(ts)},
    )
    _save_demo(out_dir / "sitemap.json", {"updated": ts, "watch_count": 0, "count": 0, "items": []})
    print(f"demo data written to {out_dir}")


def _demo_games(ts: str) -> list[dict]:
    rows = [
        ("Witch Storm", "steam_new", 72, 2.4, 500),
        ("Neon Drift", "trending:US", 88, 3.1, 900),
        ("Puzzle Grove", "steam_soon", 41, 0.0, 0),
        ("Sky Brawl", "appstore:us", 55, 1.2, 300),
    ]
    items = []
    for i, (name, source, score, rise, growth) in enumerate(rows):
        base = max(0, score - 45)
        series = [min(100, base + j * 4) for j in range(28)]
        items.append({
            "name": name,
            "series": series,
            "chart_at": ts,
            "first": ts,
            "last": ts,
            "score": score,
            "rise": rise,
            "growth": growth,
            "sightings": 1 + i,
            "source": source,
            "url": "",
            "zh": "",
            "new": i % 2 == 0,
            "web_game": name.endswith((".io", ".app")),
            "breakout": growth >= 500 or rise >= 1.0,
            "advice": dict(_SAMPLE_ADVICE, stars=min(5, 2 + i % 4)),
        })
    return items


def _demo_gameradar(ts: str) -> list[dict]:
    rows = [
        ("neon drift.io", ["itch", "newgrounds"], "greenfield", 8, 2),
        ("cat café tycoon", ["itch-popular"], "contested", 55, 6),
        ("goblin incremental", ["crazygames", "poki"], "greenfield", 12, 1),
        ("pixel dungeon crawl", ["poki", "y8"], "occupied", 78, 9),
        ("sunset racing 3d", ["gamepix", "lagged"], "unknown", 44, 4),
    ]
    items = []
    for i, (name, srcs, market, seo_score, risk) in enumerate(rows):
        items.append({
            "name": name,
            "q": name,
            "first": ts,
            "last": ts,
            "sightings": 1 + i % 3,
            "sources": srcs,
            "url": "",
            "web_game": name.endswith((".io", ".app", ".game")),
            "new": i % 2 == 0,
            "fast": {
                "model": "gameradar",
                "score": 20 + i * 15,
                "classification": ["weak", "watch", "pass", "pass", "watch"][i % 5],
                "onlinePlatformCount": len(srcs),
                "sourceAdded24h": 1 if i % 2 == 0 else 0,
                "sourceAdded48h": 1,
            },
            "youtube": ({
                "checkedAt": ts, "videoCount": 3 + i, "channelCount": 1 + i % 3,
                "totalViews": 5000 * (i + 1), "recent24h": i % 2,
            } if i % 2 == 0 else None),
            "seo": {
                "score": seo_score,
                "label": "高" if seo_score >= 70 else ("中" if seo_score >= 45 else "低"),
                "market": {"status": market, "confidence": "medium", "reason": ""},
                "nameRisk": risk,
                "gameIntent": {
                    "game": 6, "news": 0, "nonGame": 2, "other": 2, "total": 10,
                    "gameRatio": 0.6, "newsRatio": 0.0, "nonGameRatio": 0.2,
                },
                "entityConflict": i == 3,
            },
            "advice": dict(_SAMPLE_ADVICE, stars=min(5, 2 + i % 4),
                           level=["值得做", "测试", "观察", "测试", "淘汰"][i % 5]),
            "social": ({
                "modelVersion": 3,
                "checkedAt": ts,
                "score": 30 + i * 12,
                "classification": ["watch", "pass", "viral", "weak", "watch"][i % 5],
                "crossPlatformCount": 1 + i % 3,
                "hasExternalSpillover": i == 2,
                "checkedProviders": ["youtube"],
            } if i % 2 == 0 else None),
            "opportunity": {
                "modelVersion": 1,
                "channel": "online",
                "score": 40 + i * 8,
                "testScore": 50 + i * 6,
                "allowsTest": i % 2 == 0,
                "allowsIndependent": False,
                "socialMeasured": False,
                "decision": ["test-now", "watch", "test-now", "watch", "reject"][i % 5],
                "reasons": [],
            },
        })
    return items
