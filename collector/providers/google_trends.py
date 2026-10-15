"""Google Trends providers.

GoogleTrendsSSR – primary. Scrapes the SSR page, gets vol + growth + cats.
GoogleTrendsRSS – fallback. Official RSS feed, gets vol only (no growth/cats).
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Any

from ..http import get_text
from ..parser import parse_trends
from .base import TrendProvider

log = logging.getLogger("trends.provider")

SSR_URL = "https://trends.google.com/trending?geo={geo}&hl=en-US"
RSS_URL = "https://trends.google.com/trending/rss?geo={geo}"
HT_NS = "{https://trends.google.com/trending/rss}"


def _parse_traffic(text: str | None) -> int | None:
    if not text:
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else None


class GoogleTrendsSSR(TrendProvider):
    name = "google_trends_ssr"

    def fetch(self, geo: str) -> list[dict[str, Any]]:
        html = get_text(self.session, SSR_URL.format(geo=geo), timeout=35)
        if not html:
            return []
        items = parse_trends(html, geo)
        log.info("ssr %s -> %d trends", geo, len(items))
        return items


class GoogleTrendsRSS(TrendProvider):
    name = "google_trends_rss"

    def fetch(self, geo: str) -> list[dict[str, Any]]:
        xml = get_text(self.session, RSS_URL.format(geo=geo), timeout=30)
        if not xml:
            return []
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as e:  # noqa: BLE001
            log.warning("rss %s parse error: %s", geo, e)
            return []

        items: list[dict[str, Any]] = []
        for item in root.iter("item"):
            title = item.findtext("title")
            if not title:
                continue
            traffic = item.findtext(f"{HT_NS}approx_traffic")
            pub = item.findtext("pubDate")
            items.append(
                {
                    "q": title,
                    "geo": geo,
                    "vol": _parse_traffic(traffic),
                    "growth": None,
                    "cats": [],
                    "variants": [],
                    "ts": pub,
                    "title": title,
                }
            )
        log.info("rss %s -> %d trends", geo, len(items))
        return items

