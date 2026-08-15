#!/bin/bash
# One command for the whole day. Run it in the morning, it finishes by evening.
#
#   ./morning.sh              research + draft 30, then send them at 7/hour
#   ./morning.sh 20           same, 20 emails
#   ./morning.sh 30 8         30 emails at 8/hour
#   nohup ./morning.sh &      and close the laptop
#
# Two stages, and the split matters:
#
#   1. draft  daily_drafts.py researches each company and writes one personal
#             email per contact using Groq. No Claude tokens are spent. This
#             stage is fast (a couple of minutes) and does not send anything, so
#             a bad run here costs nothing but a re-run.
#   2. send   run_batch.sh does the actual sending, and owns the daily cap,
#             the pacing, the bounce scan and the tracker sync. Nothing about
#             sending is reimplemented here.
#
# Stage 2 only starts if stage 1 wrote at least one draft, so a Groq outage or a
# bad API key stops the morning quietly instead of falling through to the sender
# and mailing the generic template to 30 founders.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

COUNT="${1:-30}"
PER_HOUR="${2:-7}"
PY=./venv/bin/python
DRAFTS_FILE="drafts-$(date +%Y-%m-%d).json"

mkdir -p logs
LOG="logs/morning-$(date +%Y%m%d-%H%M).log"
say() { printf '\n=== %s === %s\n' "$1" "$(date '+%H:%M:%S')" | tee -a "$LOG"; }

say "DRAFT"
$PY -u daily_drafts.py --count "$COUNT" --out "$DRAFTS_FILE" 2>&1 | tee -a "$LOG"
if [ "${PIPESTATUS[0]}" -ne 0 ]; then
  say "DRAFTING FAILED"
  echo "nothing was sent. see $LOG" | tee -a "$LOG"
  exit 1
fi

# Count what was actually written. daily_drafts.py drops a contact rather than
# ship a draft that fails validation, so this is usually <= COUNT.
WROTE=$($PY -c "import json,sys; print(len(json.load(open('$DRAFTS_FILE'))))" 2>/dev/null || echo 0)
if [ "$WROTE" -lt 1 ]; then
  say "NOTHING DRAFTED"
  echo "no drafts written, not starting the sender. see $LOG" | tee -a "$LOG"
  exit 1
fi
echo "  $WROTE drafts ready" | tee -a "$LOG"

say "SEND"
echo "  $WROTE emails at $PER_HOUR/hour, about $((WROTE * 60 / PER_HOUR)) minutes" | tee -a "$LOG"
DRAFTS="$DRAFTS_FILE" ./run_batch.sh "$WROTE" "$PER_HOUR" 2>&1 | tee -a "$LOG"

say "DONE"
echo "log: $LOG" | tee -a "$LOG"
