import json, os, time
from datetime import datetime, timezone

LOG_PATH = os.path.join(os.path.dirname(__file__), "engine_log.jsonl")
LOG_ENABLED_PATH = os.path.join(os.path.dirname(__file__), "logging_enabled.flag")

def is_logging_enabled():
    return os.path.exists(LOG_ENABLED_PATH)

def set_logging_enabled(enabled: bool):
    if enabled:
        open(LOG_ENABLED_PATH, 'w').close()
    elif os.path.exists(LOG_ENABLED_PATH):
        os.remove(LOG_ENABLED_PATH)

def write_log_entry(state_dict: dict):
    """Append one structured log entry. Called each engine cycle if logging is on."""
    if not is_logging_enabled():
        return
    entry = {
        "ts":        int(time.time()),
        "dt":        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        **state_dict
    }
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")

def read_log(limit=500):
    """Return the last N log entries as a list of dicts.

    Reads ONLY the tail of the file. engine_log.jsonl grows unbounded (100MB+),
    and the previous implementation json-parsed every line into ~40k dicts on
    every call — on the 1GB droplet that pinned the dashboard process at 650MB+
    RSS and thrashed swap, making the dashboard unresponsive (budgets wouldn't
    load/save). Seeking to a bounded tail keeps memory at O(limit), not O(file).
    """
    if not os.path.exists(LOG_PATH):
        return []
    # Tail chunk sized to comfortably hold `limit` lines (each ~1-2KB), min 1MB.
    approx = max(limit * 4096, 1_000_000)
    try:
        size = os.path.getsize(LOG_PATH)
        with open(LOG_PATH, "rb") as f:
            if size > approx:
                f.seek(-approx, os.SEEK_END)
                f.readline()  # discard the partial first line after the seek
            data = f.read()
    except OSError:
        return []
    entries = []
    for raw in data.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            entries.append(json.loads(raw))
        except Exception:
            pass
    return entries[-limit:]

def clear_log():
    if os.path.exists(LOG_PATH):
        os.remove(LOG_PATH)
