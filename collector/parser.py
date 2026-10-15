"""Parse the Google Trends "Trending Now" SSR payload.

`https://trends.google.com/trending?geo=US` returns an Angular SSR page that
embeds the full trend list as:

    AF_initDataCallback({key: 'ds:0', hash: '2', data:[null,[ <trend>, ... ]], ...});

Each <trend> row is a 13-element array:
    0  query            str
    2  geo              str
    3  [start_ts]       unix seconds
    4  [end_ts] | null
    6  approx_traffic   int   (volume bucket floor, e.g. 500, 200000)
    8  growth           int   (percent increase, capped at 1000)
    9  variants         [str]
    10 category_ids     [int]
    11 news_ids         [[...]]
    12 display title    str
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

_DS0_RE = re.compile(
    r"AF_initDataCallback\(\{key:\s*'ds:0'.*?data:(\[.*?\]),\s*sideChannel",
    re.DOTALL,
)


def extract_ds0_payload(html: str) -> Optional[list]:
    """Return the raw parsed `data` array for key ds:0, or None."""
    m = _DS0_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except (ValueError, TypeError):
        return None


def parse_trends(html: str, geo: str) -> list[dict[str, Any]]:
    """Parse trends out of a /trending page. Returns [] when unavailable."""
    data = extract_ds0_payload(html)
    if not data or len(data) < 2 or not isinstance(data[1], list):
        return []

    out: list[dict[str, Any]] = []
    for row in data[1]:
        if not isinstance(row, list) or not row or not row[0]:
            continue
        q = str(row[0])

        def _at(i, default=None):
            return row[i] if len(row) > i else default

        cats_raw = _at(10) or []
        cats = [c for c in cats_raw if isinstance(c, int)]
        variants_raw = _at(9) or []
        variants = [v for v in variants_raw if isinstance(v, str)]
        ts_arr = _at(3) or []
        ts = ts_arr[0] if isinstance(ts_arr, list) and ts_arr else None
        title = _at(12) or q

        out.append(
            {
                "q": q,
                "geo": geo,
                "vol": _at(6),
                "growth": _at(8),
                "cats": cats,
                "variants": variants,
                "ts": ts,
                "title": title,
            }
        )
    return out
