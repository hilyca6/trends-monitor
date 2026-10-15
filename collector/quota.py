"""Reusable monthly/daily API quota guard (reserve-before-request).

The collector's own sources are free, so nothing here is wired in by default.
This exists as a template: when you later plug in a paid/hosted API (SerpAPI,
Apify, Brave Search, ...), wrap each request with a :class:`Quota` so a bug or
a busy day can never blow past the plan. Usage is persisted to a JSON file and
reserved *before* the request is made (same idea as the game-radar tools).

Example::

    from collector.quota import Quota, QuotaExceeded

    q = Quota(state_dir / "serpapi-usage.json", monthly_limit=200, daily_limit=8)
    try:
        q.reserve()                 # increments + persists, raises if over budget
        response = call_paid_api()
    except QuotaExceeded as e:
        log.info("quota exhausted: %s", e)
    usage = q.usage()               # {month, monthUsed, day, dayUsed, ...}
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("trends.quota")


class QuotaExceeded(Exception):
    def __init__(self, message: str, code: str = "QUOTA_GUARD", *, api_requests: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.api_requests = api_requests


class Quota:
    """A month/day-limited counter persisted to ``path``."""

    def __init__(self, path, *, monthly_limit: int, daily_limit: int, name: str = "api") -> None:
        self.path = Path(path)
        self.monthly_limit = max(1, int(monthly_limit))
        self.daily_limit = max(1, int(daily_limit))
        self.name = name

    # -- persistence ------------------------------------------------------
    def _read(self) -> dict:
        now = datetime.now(timezone.utc)
        month, day = now.isoformat()[:7], now.isoformat()[:10]
        usage: dict = {}
        if self.path.exists():
            try:
                usage = json.loads(self.path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                usage = {}
        if usage.get("month") != month:
            usage = {"month": month, "monthUsed": 0, "day": day, "dayUsed": 0}
        elif usage.get("day") != day:
            usage = {**usage, "day": day, "dayUsed": 0}
        usage.setdefault("monthUsed", 0)
        usage.setdefault("dayUsed", 0)
        usage["monthlyLimit"] = self.monthly_limit
        usage["dailyLimit"] = self.daily_limit
        return usage

    def _write(self, usage: dict) -> None:
        usage["updatedAt"] = datetime.now(timezone.utc).isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(usage, ensure_ascii=False, indent=2), encoding="utf-8")

    # -- api --------------------------------------------------------------
    def usage(self) -> dict:
        u = self._read()
        self._write(u)  # persist rollover even on read
        return u

    def remaining(self) -> tuple[int, int]:
        u = self._read()
        return (
            max(0, self.monthly_limit - int(u["monthUsed"])),
            max(0, self.daily_limit - int(u["dayUsed"])),
        )

    def available(self, n: int = 1) -> bool:
        rem_m, rem_d = self.remaining()
        return rem_m >= n and rem_d >= n

    def reserve(self, n: int = 1) -> dict:
        """Reserve ``n`` requests; persist and return usage, or raise QuotaExceeded."""
        u = self._read()
        if int(u["monthUsed"]) + n > self.monthly_limit or int(u["dayUsed"]) + n > self.daily_limit:
            reason = (
                f"{self.name} monthly safety limit reached ({self.monthly_limit})"
                if int(u["monthUsed"]) + n > self.monthly_limit
                else f"{self.name} daily safety limit reached ({self.daily_limit})"
            )
            raise QuotaExceeded(reason, api_requests=0)
        u["monthUsed"] = int(u["monthUsed"]) + n
        u["dayUsed"] = int(u["dayUsed"]) + n
        self._write(u)
        return u
