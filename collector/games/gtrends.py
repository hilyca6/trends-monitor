"""Google Trends interest-over-time reader (2-step API).

The shared explore/widget flow lives in ``collector.gtrends_api``; this module
only knows how to read the TIMESERIES widget into a list of 0-100 values.

Responses are prefixed with `)]}'`. Rate limits are aggressive: warm up once,
throttle between calls, and retry with backoff on 429.
"""

from __future__ import annotations

import logging

import requests

from ..gtrends_api import MULTILINE_API, GoogleTrendsClient

log = logging.getLogger("trends.games.gt")


class GoogleTrendsInterest:
    def __init__(self, session: requests.Session, throttle: float = 3.0) -> None:
        self._client = GoogleTrendsClient(session, throttle)

    def interest(
        self,
        kw: str,
        geo: str = "US",
        window: str = "now 7-d",
        retries: int = 4,
    ) -> list[int] | None:
        widgets = self._client.explore(kw, geo, window, retries=retries)
        widget = self._client.find_widget(widgets, "TIMESERIES")
        if not widget:
            return None

        data = self._client.widget_data(widget, MULTILINE_API)
        if not data:
            return None
        try:
            return [p["value"][0] for p in data["default"]["timelineData"]]
        except (KeyError, ValueError, TypeError) as e:  # noqa: BLE001
            log.warning("interest %r parse failed: %s", kw, e)
            return None
