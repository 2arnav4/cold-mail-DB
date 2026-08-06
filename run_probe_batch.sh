#!/bin/bash
# Wrapper for cron. cron runs with a minimal environment and no working
# directory, so both are set explicitly here rather than assumed.
cd /Users/arnavsingla/Desktop/Cold-Mail-DB || exit 1
LOG=probe_cron.log
# Skip this slot if the previous run is still going: overlapping runs would
# double the probe rate, which is the thing the schedule exists to bound.
if pgrep -f "verify/probe_batch.py --size" > /dev/null; then
  echo "[$(date '+%F %H:%M')] previous batch still running, skipping" >> "$LOG"
  exit 0
fi
./venv/bin/python -u verify/probe_batch.py --size 250 --rate 30 --workers 3 --gap 3 --apply >> "$LOG" 2>&1
