"""Provider abstraction so the data source can be swapped later
(e.g. replace the free SSR scraper with a paid API)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import requests


class TrendProvider(ABC):
    name = "base"

    def __init__(self, session: requests.Session, **kwargs: Any) -> None:
        self.session = session
        self.options = kwargs

    @abstractmethod
    def fetch(self, geo: str) -> list[dict[str, Any]]:
        """Return a list of trend dicts for the given geo (may be empty)."""
        raise NotImplementedError
