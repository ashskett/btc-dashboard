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

# Global mute (Ash 2026-09-04, "turn off all griddy notifications while we
# rethink the plan"): when this file exists, ALL pushes are suppressed (still
# logged to stdout so nothing is lost). Replies to Ash's OWN Telegram commands
# bypass via send_direct() - muting answers to questions he just asked would
# only break the mobile bridge. Unmute: delete the file.
_MUTE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "notifications_muted")


def _send(text: str, force: bool = False) -> bool:
    """POST to Telegram. Returns True on success, False on failure."""
    if not _TOKEN or not _CHAT_ID:
        return False
    if os.path.exists(_MUTE_FILE) and not force:
        print(f"[notify MUTED] {text[:160]}")
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


def send_direct(msg: str):
    """Bypasses the global mute - ONLY for replies to Ash's own commands
    (mobile bridge). Never use for engine/audit chatter."""
    _send(msg, force=True)
