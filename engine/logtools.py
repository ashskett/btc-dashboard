"""Continuous log reader — streams a rotated log's FULL history in time order.

Logs are rotated monthly into gzip archives (log_rotate.sh) to keep the active
files small and fast, but NOTHING is ever deleted. This reader stitches every
monthly archive (`<name>.YYYY-MM.jsonl.gz`) plus the live `<name>.jsonl` back into
one unbroken stream, gunzipping on the fly — so any analysis (P&L, backtests, a
performance review months from now) sees the entire multi-year history exactly as
if the log had never been split.

Usage:
    from logtools import iter_entries
    for e in iter_entries("engine_log"):   # basename, no extension
        ...
"""
import os
import gzip
import json
import glob

HERE = os.path.dirname(os.path.abspath(__file__))


def archive_files(basename):
    """All files for a rotated log, oldest → newest: monthly .gz archives (sorted
    lexically, which is chronological for YYYY-MM) then the live .jsonl."""
    base = os.path.join(HERE, basename)
    files = sorted(glob.glob(base + ".*.jsonl.gz"))
    live = base + ".jsonl"
    if os.path.exists(live):
        files.append(live)
    return files


def iter_lines(basename):
    """Yield every non-empty raw line across the full history, in order."""
    for path in archive_files(basename):
        opener = gzip.open if path.endswith(".gz") else open
        try:
            with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        yield line
        except Exception:
            continue   # a corrupt/partial archive must never break the stream


def iter_entries(basename):
    """Yield every parsed JSON entry across the full history, in order."""
    for line in iter_lines(basename):
        try:
            yield json.loads(line)
        except Exception:
            continue


if __name__ == "__main__":
    import sys
    name = sys.argv[1] if len(sys.argv) > 1 else "engine_log"
    files = archive_files(name)
    n = sum(1 for _ in iter_entries(name))
    print("%s: %d entries across %d file(s)" % (name, n, len(files)))
    for f in files:
        print("   %s (%.1f MB)" % (os.path.basename(f), os.path.getsize(f) / 1e6))
