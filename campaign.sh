#!/bin/bash
# One command for a whole campaign: wake the tracker, restore it, send, sync,
# then open the dashboard.
#
#   ./campaign.sh              dry run. Loads the drafts, shows every subject,
#                              sends nothing.
#   ./campaign.sh 15           send 15
#   ./campaign.sh 15 4         send 15, paced at 4 per hour
#   nohup ./campaign.sh 15 4 & send in the background and close the laptop lid
#
# Runnable from any directory: it cds to its own location first, so the venv,
# the database and the .env are always the ones next to this file.
#
# ── Why the tracker comes first ──
#
# Render's free plan sleeps the service after ~15 idle minutes and keeps the
# database in /tmp, so a woken instance starts empty. The /t/ pixel route only
# records an open against a send it already knows about, which means a mail
# sent while the tracker is empty is invisible forever: the open arrives,
# finds no matching send row, and is dropped. Restoring before sending is not
# tidiness, it is the difference between having open data and not.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

LIMIT="${1:-}"
PER_HOUR="${2:-}"
PY=./venv/bin/python
TRACKER=https://cold-mail-tracker-qmmh.onrender.com

mkdir -p logs
LOG="logs/campaign-$(date +%Y%m%d-%H%M).log"
say() { printf '\n=== %s === %s\n' "$1" "$(date '+%H:%M:%S')" | tee -a "$LOG"; }

KEY=$(grep '^TRACKER_SECRET=' .env | cut -d= -f2-)
[ -z "$KEY" ] && { echo "TRACKER_SECRET missing from .env"; exit 1; }

say "WAKE THE TRACKER"
# A cold start on the free tier takes most of a minute. Ask for it and wait,
# rather than letting the first real API call time out mid-send.
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 120 "$TRACKER/ping")
echo "  /ping -> $code" | tee -a "$LOG"
[ "$code" != "200" ] && echo "  WARNING: tracker did not answer. Opens will not be recorded." | tee -a "$LOG"

say "RESTORE ITS DATA"
$PY -u sender/bounce_scan.py 2>&1 | grep -E "Tracker synced|Detected" | tee -a "$LOG"

say "SEND"
./run_batch.sh ${LIMIT:+"$LIMIT"} ${PER_HOUR:+"$PER_HOUR"} 2>&1 | tee -a "$LOG"

say "DASHBOARD"
echo "  $TRACKER/stats?key=$KEY" | tee -a "$LOG"
# Only pop a browser for a real send. A dry run in a background job should not
# steal focus.
if [ -n "$LIMIT" ] && [ -t 1 ]; then
  open "$TRACKER/stats?key=$KEY"
fi

say "DONE"
echo "log: $LOG" | tee -a "$LOG"
