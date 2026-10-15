"""Shared Google Trends API client (explore -> widget data).

Google exposes no official API for trends. The flow is always the same:

  1. GET /trends/explore page  -> obtains the NID cookie (required, else 429)
  2. GET /trends/api/explore   -> returns widgets (each with id/token/request)
  3. GET the widget's data endpoint (multiline / relatedsearches) with the
     widget's own request + token.

Rate limits are aggressive: warm up once, throttle between calls, retry with
backoff on non-200. This module centralises that so both the interest-over-time
client (games) and the related-queries client (SEO) share one implementation.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import requests

from . import proxy as proxy_pool

log = logging.getLogger("trends.gtapi")

EXPLORE_PAGE = "https://trends.google.com/trends/explore"
EXPLORE_API = "https://trends.google.com/trends/api/explore"
MULTILINE_API = "https://trends.google.com/trends/api/widgetdata/multiline"
RELATED_API = "https://trends.google.com/trends/api/widgetdata/relatedsearches"

# Google's anti-bot "unusual traffic" interstitial — a hard stop signal.
_SORRY_RE = re.compile(r"/sorry/|sorry/index", re.IGNORECASE)

# Set the first time Google blocks us during a run (used by the push reminder).
_blocked_at: str | None = None


def last_blocked() -> str | None:
    """ISO time of the first Google block this process, or None."""
    return _blocked_at


def mark_blocked() -> None:
    """Record the first Google block this process (idempotent).

    Shared by every Google-facing path (explore API *and* the SSR/suggest HTTP
    layer) so a single block short-circuits the rest of the run.
    """
    global _blocked_at
    if _blocked_at is None:
        _blocked_at = datetime.now(timezone.utc).isoformat()


def is_blocked_process() -> bool:
    """True once Google has blocked us at least once this process."""
    return _blocked_at is not None


class BlockedError(Exception):
    """Google served its anti-bot page (/sorry/) or a hard 429.

    Raised instead of retrying, so callers can stop immediately rather than
    piling on more requests (which only deepens the rate limit).
    """


def _is_blocked(r) -> bool:
    if r is None:
        return False
    if getattr(r, "status_code", None) == 429:
        return True
    return bool(_SORRY_RE.search(getattr(r, "url", "") or ""))


def is_blocked_response(r) -> bool:
    """Public wrapper: same 429 / ``/sorry/`` rules for the shared HTTP layer."""
    return _is_blocked(r)


def _ensure_ok(r) -> None:
    if _is_blocked(r):
        mark_blocked()
        raise BlockedError(f"blocked: {getattr(r, 'status_code', '?')} {getattr(r, 'url', '')}")

# Google prefixes JSON responses with ")]}'".
def parse_json_prefix(text: str) -> Any:
    i = text.find("{")
    if i < 0:
        return None
    try:
        return json.loads(text[i:])
    except ValueError:
        return None


class GoogleTrendsClient:
    """Low-level explore/widget client. One instance warms Google up once."""

    def __init__(self, session: requests.Session, throttle: float = 3.0, hl: str = "en-US", tz: str = "0") -> None:
        self.s = session
        self.throttle = throttle
        self.hl = hl
        self.tz = tz
        self._warmed = False
        self._rotations = 0
        self.max_rotations = max(0, int(os.getenv("TRENDS_MAX_ROTATIONS", "3")))

    @staticmethod
    def _kw_list(kw) -> list[str]:
        """Normalise a keyword or list of keywords into a list of strings."""
        return [kw] if isinstance(kw, str) else [str(k) for k in kw]

    def _reproxy(self) -> None:
        """Point the session at the pool's current proxy and force a re-warm."""
        if proxy_pool.current():
            proxy_pool.apply_to_session(self.s)
        else:
            pg = getattr(self.s, "proxies", None)
            if isinstance(pg, dict):
                pg.clear()
        self._warmed = False

    def _try_rotate(self) -> bool:
        """Rotate the proxy after a block. False if no rotation is available."""
        if self._rotations >= self.max_rotations or not proxy_pool.notify_blocked():
            return False
        self._rotations += 1
        self._reproxy()
        log.info("google blocked; rotated proxy (%d/%d)", self._rotations, self.max_rotations)
        return True

    def warm(self, kw, geo: str) -> None:
        q = ",".join(self._kw_list(kw))
        for _ in range(self.max_rotations + 1):
            try:
                r = self.s.get(EXPLORE_PAGE, params={"geo": geo, "q": q, "hl": self.hl}, timeout=30)
            except requests.RequestException as e:  # noqa: BLE001
                log.warning("warmup failed: %s", e)
                return
            if not _is_blocked(r):
                return
            if not self._try_rotate():
                _ensure_ok(r)  # raises BlockedError (records the block)
                return
            time.sleep(1.5)

    def _ensure_warm(self, kw, geo: str) -> None:
        if not self._warmed:
            self._warmed = True  # set first so a blocked warmup isn't retried forever
            self.warm(kw, geo)   # may raise BlockedError
            time.sleep(1.5)

    def explore(
        self,
        kw,
        geo: str = "US",
        window: str = "now 7-d",
        category: int = 0,
        prop: str = "",
        retries: int = 4,
    ) -> list[dict]:
        """Return the explore API widgets for one or more keywords, or [] on failure.

        ``kw`` may be a single string or a list. Passing several keywords puts
        them in the same ``comparisonItem`` so Google normalises them on one
        0-100 scale (required for any cross-term ratio).
        """
        kws = self._kw_list(kw)
        req = {
            "comparisonItem": [{"keyword": k, "geo": geo, "time": window} for k in kws],
            "category": category,
            "property": prop,
        }
        self._ensure_warm(kws, geo)
        for attempt in range(retries):
            try:
                r = self.s.get(
                    EXPLORE_API,
                    params={"hl": self.hl, "tz": self.tz, "req": json.dumps(req)},
                    timeout=30,
                )
            except requests.RequestException as e:  # noqa: BLE001
                log.warning("explore %r error: %s", kw, e)
                time.sleep(4 * (attempt + 1))
                continue
            if _is_blocked(r):
                if self._try_rotate():
                    time.sleep(2)
                    continue
                _ensure_ok(r)  # /sorry/ or 429 -> BlockedError, stop retrying
            if r.status_code == 200:
                data = parse_json_prefix(r.text)
                widgets = (data or {}).get("widgets")
                if widgets:
                    return widgets
            log.info("explore %r -> %s, retry %d", kw, getattr(r, "status_code", "?"), attempt + 1)
            time.sleep(4 * (attempt + 1))
        return []

    def interest_multi(
        self,
        kws,
        geo: str = "US",
        window: str = "now 7-d",
        retries: int = 4,
    ) -> dict[str, list[int]] | None:
        """Fetch one TIMESERIES for several keywords in a single request.

        Returns ``{keyword: [values]}`` with aligned points (same length/order),
        or None on failure. Because the keywords share one explore request,
        Google normalises them on the same 0-100 scale — the only correct way to
        compute a cross-term ratio (e.g. term vs the "gpts" baseline).
        """
        names = self._kw_list(kws)
        widgets = self.explore(names, geo, window, retries=retries)
        widget = self.find_widget(widgets, "TIMESERIES")
        if not widget:
            return None
        data = self.widget_data(widget, MULTILINE_API)
        if not data:
            return None
        try:
            timeline = data["default"]["timelineData"]
        except (KeyError, TypeError):
            log.warning("interest_multi parse failed")
            return None
        out: dict[str, list[int]] = {k: [] for k in names}
        for point in timeline:
            vals = point.get("value") or []
            for i, k in enumerate(names):
                try:
                    out[k].append(int(vals[i]))
                except (IndexError, ValueError, TypeError):
                    out[k].append(0)
        return out if any(out.values()) else None

    @staticmethod
    def find_widget(widgets: list[dict], widget_id: str) -> dict | None:
        return next((w for w in widgets if w.get("id") == widget_id), None)

    def widget_data(self, widget: dict, endpoint: str) -> Any:
        """Fetch and parse one widget's data, or None on failure."""
        time.sleep(max(0.5, self.throttle - 1.0))
        for _ in range(self.max_rotations + 1):
            try:
                rr = self.s.get(
                    endpoint,
                    params={
                        "hl": self.hl,
                        "tz": self.tz,
                        "req": json.dumps(widget["request"]),
                        "token": widget["token"],
                    },
                    timeout=30,
                )
            except (requests.RequestException, KeyError, ValueError, TypeError) as e:  # noqa: BLE001
                log.warning("widget %s failed: %s", widget.get("id"), e)
                return None
            if _is_blocked(rr):
                if self._try_rotate():
                    time.sleep(1.5)
                    continue
                _ensure_ok(rr)  # /sorry/ or 429 -> BlockedError
            if rr.status_code != 200:
                log.info("widget %s -> %s", widget.get("id"), rr.status_code)
                return None
            return parse_json_prefix(rr.text)
        return None
