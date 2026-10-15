"""Push a short digest to WeCom / DingTalk / Telegram (all free, webhook-based).

Disabled unless `push.enabled` is true in config.local.json. Each channel is
optional — fill in whichever webhook(s) you use.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
import urllib.parse

import requests

log = logging.getLogger("trends.push")


def dingtalk_sign(secret: str, timestamp_ms: str) -> str:
    """Return the URL-encoded HMAC-SHA256 signature DingTalk requires."""
    string_to_sign = f"{timestamp_ms}\n{secret}"
    digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    return urllib.parse.quote_plus(base64.b64encode(digest))


def _post_once(url: str, payload: dict):
    """Return (ok, error). Treats HTTP>=400 *and* API-level error codes as failure."""
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code >= 400:
            return False, f"HTTP {r.status_code}"
        try:
            data = r.json()
        except ValueError:
            return True, ""
        if isinstance(data, dict):
            if "errcode" in data and data.get("errcode") not in (0, "0"):
                return False, f"errcode {data.get('errcode')}: {data.get('errmsg', '')}"
            if data.get("ok") is False:
                return False, f"ok=false: {data.get('description') or data.get('error') or ''}"
        return True, ""
    except requests.RequestException as e:  # noqa: BLE001
        return False, str(e)


def _post(url: str, payload: dict, attempts: int = 3, backoff: float = 2.0) -> bool:
    for i in range(1, attempts + 1):
        ok, err = _post_once(url, payload)
        if ok:
            return True
        log.warning("push attempt %d/%d failed %s: %s", i, attempts, url.split("?")[0], err)
        if i < attempts:
            time.sleep(backoff * i)
    return False


def send_text(cfg: dict, content: str) -> bool:
    push = cfg.get("push") or {}
    if not push.get("enabled"):
        return False

    ok = False

    if push.get("wecom_webhook"):
        ok |= _post(push["wecom_webhook"], {"msgtype": "text", "text": {"content": content}})

    if push.get("dingtalk_webhook"):
        url = push["dingtalk_webhook"]
        if push.get("dingtalk_secret"):
            ts = str(round(time.time() * 1000))
            sign = dingtalk_sign(push["dingtalk_secret"], ts)
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}timestamp={ts}&sign={sign}"
        ok |= _post(url, {"msgtype": "text", "text": {"content": content}})

    if push.get("telegram_bot_token") and push.get("telegram_chat_id"):
        tg = f"https://api.telegram.org/bot{push['telegram_bot_token']}/sendMessage"
        ok |= _post(tg, {"chat_id": push["telegram_chat_id"], "text": content, "disable_web_page_preview": True})

    return ok
