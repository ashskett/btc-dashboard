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
    """Return last N log entries as a list of dicts."""
    if not os.path.exists(LOG_PATH):
        return []
    entries = []
    with open(LOG_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
    return entries[-limit:]

def clear_log():
    if os.path.exists(LOG_PATH):
        os.remove(LOG_PATH)
