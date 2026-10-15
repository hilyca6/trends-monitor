"""Proxy pool with rotation on Google rate-limit blocks.

Google rate-limits by exit IP, and a blocked IP tends to stay blocked for a
while. Configure a *list* of proxies in ``config.local.json`` (``"proxy"`` may
be a string or a list) and/or a ``proxy_switch_url`` that rotates your local
proxy's upstream node (e.g. a Clash/v2ray control endpoint). When a block is
seen the collector rotates to the next proxy and retries once.

The pool is a process-wide singleton configured by ``settings.apply_environment``
and read by ``http.build_session`` and ``gtrends_api.GoogleTrendsClient``.
"""

from __future__ import annotations

import logging
import os
import random
from urllib.parse import quote

import requests

log = logging.getLogger("trends.proxy")

# Clash proxy-group types; entries of these kinds are *groups*, not leaf nodes,
# so a rotation should never land on one.
_GROUP_TYPES = {"Selector", "URLTest", "Fallback", "LoadBalance", "Relay"}

# The controller is a local address and must never go through the proxy itself
# (HTTP_PROXY is exported to point at Clash, which would mangle the API call).
_NO_PROXY = {"http": None, "https": None}


class ProxyPool:
    def __init__(self, proxies=None, *, switch_url: str = "", rotate_on_block: bool = True,
                 switch: dict | None = None) -> None:
        self._proxies = [p for p in (proxies or []) if p]
        self._i = 0
        self.switch_url = switch_url or ""
        self.rotate_on_block = bool(rotate_on_block)
        # Clash-style controller switch: {url, secret, group, nodes[]}
        self.switch = switch if isinstance(switch, dict) else {}
        self.rotations = 0

    @property
    def size(self) -> int:
        return len(self._proxies)

    def current(self) -> str | None:
        return self._proxies[self._i] if self._proxies else None

    def _switch_usable(self) -> bool:
        """A Clash switch needs a controller url AND a selector group to switch."""
        return bool(self.switch.get("url")) and bool(self.switch.get("group"))

    def can_rotate(self) -> bool:
        return len(self._proxies) > 1 or bool(self.switch_url) or self._switch_usable()

    def _hit_switch(self) -> bool:
        """Poke the configured switch. True only when it actually succeeded."""
        if self.switch.get("url"):
            return self._clash_switch(self.switch)
        if self.switch_url:
            try:
                r = requests.get(self.switch_url, timeout=5, proxies=_NO_PROXY)
                ok = getattr(r, "status_code", 200) < 400
                log.info("proxy switch endpoint hit (status %s)", getattr(r, "status_code", "?"))
                return ok
            except requests.RequestException as e:  # noqa: BLE001
                log.warning("proxy switch failed: %s", e)
        return False

    def _fetch_leaf_nodes(self, base: str, group: str, headers: dict) -> tuple[list[str], str | None]:
        """Return ``(leaf_nodes, current)`` for a selector group.

        Uses the top-level ``/proxies`` map so sub-groups (other Selectors /
        URLTest / Fallback / ...) and DIRECT/REJECT are skipped — a rotation
        must land on a real proxy node. This matters for ``GLOBAL``, whose list
        also contains every other group.
        """
        try:
            r = requests.get(f"{base}/proxies", headers=headers, timeout=5, proxies=_NO_PROXY)
            proxies = (r.json() or {}).get("proxies") or {}
        except (requests.RequestException, ValueError) as e:  # noqa: BLE001
            log.warning("clash proxies list failed: %s", e)
            return [], None
        info = proxies.get(group) or {}
        special = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE", "GLOBAL"}
        nodes = [
            str(name)
            for name in (info.get("all") or [])
            if name not in special and (proxies.get(name) or {}).get("type") not in _GROUP_TYPES
        ]
        return nodes, info.get("now")

    def _clash_switch(self, cfg: dict) -> bool:
        """Switch the upstream node of a Clash selector group (Clash Verge etc.).

        PUT {url}/proxies/{group} {"name": <node>}. If no explicit node list is
        configured, the group's leaf nodes are fetched and a random non-current
        one is chosen. Returns True only when the switch request was accepted.
        """
        base = str(cfg.get("url", "")).rstrip("/")
        group = str(cfg.get("group") or "")
        if not (base and group):
            log.warning("clash switch: missing url/group")
            return False
        headers = {}
        if cfg.get("secret"):
            headers["Authorization"] = f"Bearer {cfg['secret']}"
        nodes = [str(n) for n in (cfg.get("nodes") or []) if n]
        current = None
        if not nodes:
            nodes, current = self._fetch_leaf_nodes(base, group, headers)
        if not nodes:
            log.warning("clash switch: no candidate leaf nodes for group %r", group)
            return False
        choices = [n for n in nodes if n != current] or nodes
        node = random.choice(choices)
        try:
            r = requests.put(f"{base}/proxies/{quote(group)}", headers=headers,
                             json={"name": node}, timeout=5, proxies=_NO_PROXY)
        except requests.RequestException as e:  # noqa: BLE001
            log.warning("clash switch failed: %s", e)
            return False
        if getattr(r, "status_code", 200) >= 400:
            log.warning("clash switch rejected: HTTP %s", getattr(r, "status_code", "?"))
            return False
        log.info("clash switched %s -> %r", group, node)
        return True

    def rotate(self) -> str | None:
        """Advance to the next proxy (and/or poke the switch endpoint).

        ``rotations`` only counts rotations that actually happened, so a
        misconfigured or dead switch does not inflate it.
        """
        switched = self._hit_switch()
        advanced = False
        if len(self._proxies) > 1:
            self._i = (self._i + 1) % len(self._proxies)
            advanced = True
        if switched or advanced:
            self.rotations += 1
        return self.current()

    def notify_blocked(self) -> bool:
        """Rotate after a block. True only when a real rotation happened."""
        if not self.rotate_on_block or not self.can_rotate():
            return False
        before = self.rotations
        self.rotate()
        return self.rotations > before


_pool = ProxyPool()


def configure(proxies, *, switch_url: str = "", rotate_on_block: bool = True,
              switch: dict | None = None) -> ProxyPool:
    """(Re)build the process-wide pool and export the current proxy to the env."""
    global _pool
    if isinstance(proxies, str):
        proxies = [proxies]
    _pool = ProxyPool(
        [p.strip() for p in (proxies or []) if (p or "").strip()],
        switch_url=switch_url,
        rotate_on_block=rotate_on_block,
        switch=switch,
    )
    apply_environment(_pool)
    return _pool


def pool() -> ProxyPool:
    return _pool


def current() -> str | None:
    return _pool.current()


def rotate() -> str | None:
    p = _pool.rotate()
    apply_environment(_pool)
    return p


def notify_blocked() -> bool:
    ok = _pool.notify_blocked()
    if ok:
        apply_environment(_pool)
    return ok


def apply_to_session(session, proxy: str | None = None) -> str | None:
    """Point a requests session's ``proxies`` at the pool's current proxy.

    Lets a long-lived session (collect / suggest / gtrends_api) follow pool
    rotations instead of staying stuck on the proxy chosen when it was built.
    Returns the proxy now in effect (or None when the pool has none).
    """
    cur = proxy if proxy is not None else _pool.current()
    pg = getattr(session, "proxies", None)
    if isinstance(pg, dict) and cur:
        pg.update({"http": cur, "https": cur})
    return cur


def apply_environment(p: ProxyPool | None = None) -> str | None:
    """Point HTTP(S)_PROXY at the pool's current proxy (no-op if none)."""
    p = p or _pool
    cur = p.current()
    if cur:
        os.environ["HTTPS_PROXY"] = cur
        os.environ["HTTP_PROXY"] = cur
    return cur
