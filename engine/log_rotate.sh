#!/bin/bash
# Monthly log rotation — compress & KEEP FOREVER, never delete.
#
# Moves each large append-only log to a gzip'd, month-labelled archive and lets
# the writer create a fresh empty file on its next append (safe: the engine
# reopens the log on every write, so there is no held file handle and no lost
# lines). ~7x compression. logtools.py reads archives + live file as one stream.
#
# Run on the 1st of each month via cron:  0 5 1 * *  (05:00 UTC)
# Nothing here deletes data — archives accumulate and are backed up to Ash's Mac.
set -e
cd /root/grid-engine || exit 1

# Label the archive with the month that just ended (rotation runs early next month).
STAMP=$(date -u -d "yesterday" +%Y-%m 2>/dev/null || date -u +%Y-%m)

# Logs to rotate. engine_log is the big one (tail-only reads, safe). Others are
# smaller / read in full by tools — add them here only once their readers use
# logtools.iter_entries (so rotation stays invisible to analysis).
LOGS="engine_log"

for LOG in $LOGS; do
  SRC="${LOG}.jsonl"
  [ -s "$SRC" ] || continue
  DEST="${LOG}.${STAMP}.jsonl"
  if [ -e "${DEST}.gz" ]; then
    # Same-month re-run: fold current data into the existing archive, in order.
    gunzip "${DEST}.gz"
    cat "$SRC" >> "$DEST"
    rm -f "$SRC"
    gzip -f "$DEST"
  else
    mv "$SRC" "$DEST"
    gzip -f "$DEST"
  fi
  echo "$(date -u +%FT%TZ) rotated ${SRC} -> ${DEST}.gz ($(du -h ${DEST}.gz | cut -f1))" >> log_rotate.log
done
