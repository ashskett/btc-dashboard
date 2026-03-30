"""
Thin Telegram notification wrapper for the grid engine.
Token and chat ID loaded from env.
Rate-limited: critical events fire immediately, non-critical throttled (max 1 per 30s).
Fails silently — never crashes the engine.
"""

import os
import sys
import time
import requests

_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

_last_notify_ts = 0.0
_COOLDOWN = 30  # seconds between non-critical messages


def _send(text: str) -> bool:
    """POST to Telegram. Returns True on success, False on failure."""
    if not _TOKEN or not _CHAT_ID:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{_TOKEN}/sendMessage",
            json={"chat_id": _CHAT_ID, "text": text},
            timeout=5,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"[notify] Telegram send failed: {e}", file=sys.stderr)
        return False


def notify(msg: str):
    """Rate-limited notification — max 1 per 30s. Non-critical events."""
    global _last_notify_ts
    now = time.time()
    if now - _last_notify_ts < _COOLDOWN:
        return
    _last_notify_ts = now
    _send(msg)


def notify_critical(msg: str):
    """Immediate notification — bypasses rate limit. Critical events only."""
    global _last_notify_ts
    _last_notify_ts = time.time()
    _send(msg)
