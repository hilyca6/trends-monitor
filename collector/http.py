"""Shared HTTP session: proxy, UA, retries, polite rate limiting."""

from __future__ import annotations

import logging
import os
import re
import time
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import gtrends_api
from . import proxy as proxy_pool

log = logging.getLogger("trends.http")

# Google hosts that rate-limit by exit IP (trends/suggest + the www /sorry/ page).
_GOOGLE_HOST_RE = re.compile(r"(?:^|\.)google\.[a-z.]+$", re.IGNORECASE)


def _is_google_url(url: str) -> bool:
    """True for any *.google.<tld> host (trends.google.com, suggestqueries...)."""
    return bool(_GOOGLE_HOST_RE.search(urlsplit(url or "").netloc))

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Google serves a consent interstitial unless this cookie is present.
CONSENT_COOKIE = {"SOCS": "CAISNQgQEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjMwODI5LjA3X3AxGgJlbiACGgYIgLC_pwY"}


def build_session(proxy: str | None = None) -> requests.Session:
    """Build a Session. Proxy order: arg > rotating pool > HTTPS_PROXY > ALL_PROXY.

    A session built without an explicit ``proxy`` is marked *pool-managed*: it
    keeps following the pool's current proxy across rotations (see ``get_text``)
    instead of staying stuck on the proxy chosen here.
    """
    pool_managed = proxy is None
    proxy = proxy or proxy_pool.current() or os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or os.getenv("ALL_PROXY")
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": DEFAULT_UA,
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    s.cookies.update(CONSENT_COOKIE)

    retry = Retry(
        total=3,
        backoff_factor=1.5,
        # NOTE: 429 is deliberately NOT in the forcelist. Auto-retrying on 429
        # multiplies requests exactly when Google wants fewer, which deepens the
        # rate limit (/sorry/). Let callers handle 429 (see the games circuit
        # breaker). Only retry server-side glitches here.
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    s.mount("https://", adapter)
    s.mount("http://", adapter)

    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
        log.info("using proxy %s", proxy)
    s._trends_pool_managed = pool_managed
    return s


def get_text(
    session: requests.Session,
    url: str,
    *,
    timeout: int = 30,
    throttle: float = 0.0,
) -> str | None:
    """GET a URL and return text, or None on failure.

    For Google hosts a rate-limit (HTTP 429 or the ``/sorry/`` interstitial) is
    handled like the explore API: if the session is pool-managed we rotate the
    proxy and retry (up to ``TRENDS_MAX_ROTATIONS``); once rotation can no longer
    help we record the block and raise
    :class:`~collector.gtrends_api.BlockedError` so callers stop instead of
    piling on more requests.
    """
    if throttle:
        time.sleep(throttle)
    managed = bool(getattr(session, "_trends_pool_managed", False))
    is_google = _is_google_url(url)
    max_rotations = max(0, int(os.getenv("TRENDS_MAX_ROTATIONS", "3")))
    rotations = 0
    while True:
        # Pool-managed sessions follow rotations: re-point to the current proxy
        # before every request so a rotated exit IP is actually used.
        if managed:
            proxy_pool.apply_to_session(session)
        try:
            r = session.get(url, timeout=timeout)
        except requests.RequestException as e:  # noqa: BLE001
            log.warning("GET %s failed: %s", url, e)
            return None

        if is_google and gtrends_api.is_blocked_response(r):
            if managed and rotations < max_rotations and proxy_pool.notify_blocked():
                rotations += 1
                log.info("Google blocked %s; rotated proxy (%d/%d), retrying", url, rotations, max_rotations)
                time.sleep(1.5)
                continue
            gtrends_api.mark_blocked()
            log.warning("Google blocked %s -> HTTP %s %s", url, r.status_code, getattr(r, "url", ""))
            raise gtrends_api.BlockedError(f"blocked: {r.status_code} {getattr(r, 'url', '')}")

        if r.status_code != 200:
            log.warning("GET %s -> HTTP %s", url, r.status_code)
            return None
        # Servers often omit charset; requests then falls back to ISO-8859-1 and
        # mangles UTF-8 (e.g. "Magalí" -> "MagalÃ­"). Trust an explicit charset,
        # otherwise detect it.
        if "charset" not in (r.headers.get("content-type") or "").lower():
            detected = r.apparent_encoding
            if detected:
                r.encoding = detected
        return r.text
