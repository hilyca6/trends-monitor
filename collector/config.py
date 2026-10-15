"""Static configuration: geos, category map, noise rules, paths."""

import re as _re
from pathlib import Path

from .noise_data import (
    BRAND_PHRASES,
    BRAND_WORDS,
    CLUB_PHRASES,
    CLUBS,
    FIRST_NAMES,
    LEAGUE_PHRASES,
    LEAGUES,
    SPORTS_EVENT_WORDS,
    SURNAMES,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = PROJECT_ROOT / "web"
WEB_DATA_DIR = WEB_DIR / "data"
STATE_DIR = PROJECT_ROOT / "state"

# Google Trends "Trending Now" geos (matches findnews.me). Order = display order.
ALL_GEOS = [
    "US", "GB", "CA", "AU", "NZ", "IE",
    "DE", "FR", "IT", "ES", "NL", "SE", "PL", "PT",
    "JP", "KR", "TW", "HK", "SG", "MY", "TH", "VN", "PH", "ID",
    "IN", "PK", "BD",
    "BR", "MX", "AR", "CO", "CL", "PE",
    "TR", "SA", "ZA", "NG", "KE",
]

# A smaller default set for quick/frequent runs.
DEFAULT_GEOS = ["US", "GB", "CA", "AU", "DE", "FR", "JP", "KR", "IN", "BR"]

# Google Trends trending category ids -> names.
# zh names mirror findnews.me; en names are the official Google labels.
CATEGORY_NAMES_ZH = {
    1: "汽车交通",
    2: "美容时尚",
    3: "商业财经",
    4: "娱乐",
    5: "饮食",
    6: "游戏",
    7: "健康",
    8: "爱好休闲",
    9: "工作教育",
    10: "法律政府",
    11: "其他",
    13: "宠物动物",
    14: "政治",
    15: "科学",
    16: "购物",
    17: "体育",
    18: "科技",
    19: "旅行",
    20: "天气",
}

CATEGORY_NAMES_EN = {
    1: "Autos & Vehicles",
    2: "Beauty & Fashion",
    3: "Business & Finance",
    4: "Entertainment",
    5: "Food & Drink",
    6: "Games",
    7: "Health",
    8: "Hobbies & Leisure",
    9: "Jobs & Education",
    10: "Law & Government",
    11: "Other",
    13: "Pets & Animals",
    14: "Politics",
    15: "Science",
    16: "Shopping",
    17: "Sports",
    18: "Technology",
    19: "Travel",
    20: "Weather",
}

# Category label language used when writing trends.json ("zh" | "en").
CATEGORY_LANG = "zh"

# Volume / growth filter defaults (frontend also offers these as buttons).
VOL_FILTERS = [0, 20000, 100000, 500000]
GROWTH_FILTERS = [0, 200, 500, 1000]

# ---------------------------------------------------------------------------
# New-game radar
# ---------------------------------------------------------------------------
GAMES_STATE_PATH = STATE_DIR / "games_state.json"

# Game-name radar (web-game portals -> web/data/gameradar.json).
GAMERADAR_STATE_PATH = STATE_DIR / "gameradar_state.json"

# CapCut most-used template monitor (-> web/data/capcut.json).
CAPCUT_STATE_PATH = STATE_DIR / "capcut_state.json"

# App Store "New Games" candidate sources (iTunes RSS, genre 6014 = Games).
APPSTORE_GENRE_GAMES = 6014
APPSTORE_COUNTRIES = ["us", "gb", "jp", "de", "fr", "kr", "ca", "au", "br", "in"]
APPSTORE_LIMIT = 50

# Steam Store featured categories.
STEAM_CC = "us"
STEAM_NEW_LIMIT = 30
STEAM_COMING_LIMIT = 10

# Google Trends interest-over-time settings for each game.
GT_INTEREST_GEO = "US"
GT_INTEREST_WINDOW = "now 7-d"
GT_INTEREST_THROTTLE = 10.0  # seconds between interest calls (be polite / avoid 429)
GAMES_MAX_CHART_PER_RUN = 6   # how many new games to chart per run (Google rate-limits hard)
GAMES_MAX_REFRESH_PER_RUN = 6  # how many already-charted games to re-sample per run
GAMES_CHART_MIN_GROWTH = 200  # skip a curve when a *known* Google growth (%) is below this
GAMES_CHART_MIN_POS = 0.30    # skip a curve when the ranked position is in the bottom 30%
GAMES_HOT_GROWTH = 500        # a Google growth (%) at/above this -> refresh curve every run
GAMES_HOT_RANK_CHANGE = 0.20  # a normalised chart climb at/above this -> refresh curve every run
GAMES_MAX_HOT_PER_RUN = 3     # how many hot (big mover) games to refresh FIRST each run
GT_REFRESH_HOURS = 12         # min hours before re-sampling the same game
GAMES_RISE_BREAKOUT = 1.0     # recent-vs-prior ratio that counts as a "sudden rise"
GAMES_SERIES_POINTS = 48      # downsample the 7d curve to this many points
GAMES_MIN_PEAK = 5           # ignore games whose trend peak is below this (noise)
GAMES_MIN_SIGNAL = 0.2       # min fraction of non-zero points to count as a real signal

# ---------------------------------------------------------------------------
# Noise heuristics. A "noise" term is trending but useless for building an
# SEO site (celebrity gossip, live sports fixtures, weather, accidents...).
# These are cheap rules; an optional LLM pass refines them (see enrich.py).
# ---------------------------------------------------------------------------
# substrings that strongly indicate the term is about a person
PERSON_HINTS = {"wife", "husband", "net worth", "age", "height", "gf", "girlfriend", "boyfriend"}
# sports "A vs B" fixtures
_VS_RE = _re.compile(r"\bvs\.?\b|\bv\.\b", _re.IGNORECASE)
_WORD_RE = _re.compile(r"[a-z0-9]+")
_NAME_WORDS = FIRST_NAMES | SURNAMES


def _match(q: str, words=(), phrases=()) -> bool:
    """True if q contains any listed word (token-exact) or phrase (substring)."""
    ql = q.lower()
    if phrases and any(p in ql for p in phrases):
        return True
    return bool(set(_WORD_RE.findall(ql)) & set(words)) if words else False


def _looks_like_person(q: str) -> bool:
    parts = _WORD_RE.findall(q.lower())
    return 2 <= len(parts) <= 3 and all(p in _NAME_WORDS for p in parts)


NOISE_RULES = [
    ("比赛", lambda q, cats: bool(_VS_RE.search(q)) or _match(q, words=SPORTS_EVENT_WORDS) or (17 in cats and any(t in q.lower() for t in (" vs ", " - ", " v ", "--", " x ")))),
    ("体育", lambda q, cats: _match(q, words=LEAGUES, phrases=LEAGUE_PHRASES) or _match(q, words=CLUBS, phrases=CLUB_PHRASES)),
    ("天气", lambda q, cats: bool(_re.search(r"\b(weather|hurricane|storm|forecast|rain|snow|typhoon|temperature)\b", q)) or 20 in cats),
    ("彩票", lambda q, cats: bool(_re.search(r"\b(lottery|jackpot|powerball|mega millions|euromillions|result)\b", q))),
    ("事故", lambda q, cats: bool(_re.search(r"\b(accident|crash|shooting|dead|dies|killed|explosion|fire)\b", q))),
    ("导航", lambda q, cats: bool(_re.search(r"\b(directions|map|near me|address|zip code|postal code)\b", q))),
    ("品牌", lambda q, cats: _match(q, words=BRAND_WORDS, phrases=BRAND_PHRASES)),
    ("人物", lambda q, cats: any(h in q.lower() for h in PERSON_HINTS) or _looks_like_person(q) or (4 == cats_primary(cats) and _looks_like_name(q))),
]


def cats_primary(cats):
    return cats[0] if cats else None


def _looks_like_name(q: str) -> bool:
    parts = q.split()
    return 1 < len(parts) <= 3 and all(p[:1].isupper() for p in parts if p)
