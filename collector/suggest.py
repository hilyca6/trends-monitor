"""Google suggest (autocomplete) long-tail expansion for fresh trending terms.

Uses the public suggestqueries endpoint (same one the search box uses) — free,
no key, tolerant of light automation. For each base query we also try a few
modifier patterns ("q how to", "q best", ...) to surface question/comparison
long-tails that are realistic entry pages for a small site.
"""

from __future__ import annotations

import ast
import json
import logging
import time
from urllib.parse import quote_plus

from .http import get_text

log = logging.getLogger("trends.suggest")

SUGGEST_URL = "https://suggestqueries.google.com/complete/search?client=chrome&hl={hl}&q={q}"

# Modifiers appended after the base query. Question/long-tail intent first.
MODIFIERS = ["how to", "best", "vs", "is", "why", "for"]
MAX_PER_QUERY = 12


def parse_suggest(text: str | None) -> list[str]:
    """Parse a chrome-client suggest response -> list of suggestion strings."""
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        try:
            data = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            log.warning("cannot parse suggest response")
            return []
    if isinstance(data, (list, tuple)) and len(data) > 1 and isinstance(data[1], (list, tuple)):
        return [s for s in data[1] if isinstance(s, str)]
    return []


def fetch_suggest(session, q: str, hl: str = "en", timeout: int = 15) -> list[str]:
    text = get_text(session, SUGGEST_URL.format(hl=hl, q=quote_plus(q)), timeout=timeout)
    return parse_suggest(text)


def expand(
    session,
    queries: list[str],
    *,
    modifiers: list[str] = MODIFIERS,
    per_q: int = 3,
    max_per_query: int = MAX_PER_QUERY,
    throttle: float = 0.5,
    hl: str = "en",
) -> dict[str, list[str]]:
    """Expand each query into long-tail suggestions (base + per_q modifier fetches)."""
    out: dict[str, list[str]] = {}
    for i, q in enumerate(queries):
        if throttle and i:
            time.sleep(throttle)

        seen: set[str] = set()
        picks: list[str] = []

        def _add(cands: list[str], sent: str) -> None:
            for s in cands:
                s = s.strip().lower()
                # drop the bare query and echoes of the exact string we sent
                if not s or s == q.lower() or s == sent:
                    continue
                if s not in seen:
                    seen.add(s)
                    picks.append(s)

        _add(fetch_suggest(session, q, hl=hl), q)
        for mod in modifiers[: max(0, per_q)]:
            if throttle:
                time.sleep(throttle)
            sent = f"{q} {mod}"
            _add(fetch_suggest(session, sent, hl=hl), sent)
            if len(picks) >= max_per_query:
                break

        out[q] = picks[:max_per_query]
        log.debug("suggest %s -> %d longtails", q, len(out[q]))
    log.info("suggest done: %d queries expanded", len(out))
    return out
