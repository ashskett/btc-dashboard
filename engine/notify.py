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


# ── Tiers + digest (2026-11-22) ─────────────────────────────────────────────
# send(msg, level, key): "critical" -> immediate Telegram; "info" -> appended to
# notify_queue.jsonl and delivered as ONE digest at notify_settings.json's
# digest_utc_hours (flushed by dashboard_server's _notify_digest_loop or
# POST /notify/flush). Keys listed in immediate_keys skip the queue.
# send_direct() above is untouched and always immediate.
import json as _json

_DIR = os.path.dirname(os.path.abspath(__file__))
_QUEUE_FILE = os.path.join(_DIR, "notify_queue.jsonl")
_SETTINGS_FILE = os.path.join(_DIR, "notify_settings.json")
_DEFAULT_SETTINGS = {"digest_utc_hours": [8, 20], "immediate_keys": ["fill"]}
_DIGEST_BODY_CAP = 3500


def load_settings() -> dict:
    s = dict(_DEFAULT_SETTINGS)
    try:
        with open(_SETTINGS_FILE) as f:
            d = _json.load(f)
        if isinstance(d, dict):
            s.update(d)
    except Exception:
        pass
    return s


def save_settings(updates: dict) -> dict:
    s = load_settings()
    s.update(updates or {})
    tmp = _SETTINGS_FILE + ".tmp"
    with open(tmp, "w") as f:
        _json.dump(s, f, indent=2)
    os.replace(tmp, _SETTINGS_FILE)
    return s


def send(msg: str, level: str = "info", key=None) -> bool:
    """Tiered send. Never raises."""
    try:
        if level == "critical":
            return _send(msg)
        if key is not None and key in (load_settings().get("immediate_keys") or []):
            return _send(msg)
        with open(_QUEUE_FILE, "a") as f:
            f.write(_json.dumps({"ts": int(time.time()), "msg": msg, "key": key}) + "\n")
        return True
    except Exception as e:
        print(f"[notify] send failed: {e}", file=sys.stderr)
        return False


def flush_digest() -> int:
    """Send everything queued as ONE Telegram message, then truncate the queue.
    Returns the number of items flushed (0 if queue missing/empty)."""
    # Move the queue aside first so lines appended mid-flush aren't lost.
    work = _QUEUE_FILE + ".flushing"
    try:
        if not os.path.exists(work):
            os.replace(_QUEUE_FILE, work)
        with open(work) as f:
            lines = f.readlines()
    except Exception:
        return 0
    items = []
    for ln in lines:
        try:
            d = _json.loads(ln)
            if isinstance(d, dict) and d.get("msg"):
                items.append(d)
        except Exception:
            continue
    if not items:
        try:
            os.remove(work)
        except Exception:
            pass
        return 0
    # Group exact-duplicate texts, keeping first-seen order.
    counts, order = {}, []
    for d in items:
        m = str(d["msg"])
        if m not in counts:
            counts[m] = 0
            order.append(m)
        counts[m] += 1
    entries = [(m if len(m) <= 1200 else m[:1200] + "...")
               + ("" if counts[m] == 1 else " (x{})".format(counts[m]))
               for m in order]
    body, used = [], 0
    for i, e in enumerate(entries):
        if used + len(e) + 1 > _DIGEST_BODY_CAP:
            body.append("... +{} more".format(len(entries) - i))
            break
        body.append(e)
        used += len(e) + 1
    header = "Griddy digest {} UTC".format(time.strftime("%H:%M", time.gmtime()))
    _send(header + "\n\n" + "\n".join(body))
    try:
        os.remove(work)
    except Exception:
        pass
    return len(items)
