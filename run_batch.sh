#!/bin/bash
# Send a batch of written emails, unattended. No assistant in the loop.
#
#   ./run_batch.sh                 # dry run, shows who would get what
#   ./run_batch.sh 15              # actually send 15
#   ./run_batch.sh 15 4            # send 15, paced at 4 per hour
#   nohup ./run_batch.sh 15 4 &    # and go to bed
#
# Three stages, in this order for a reason:
#
#   1. load     drafts -> personalized_emails.local.json. Existing hand-written
#               entries always win, so this never overwrites your own copy.
#   2. send     send_emails.py with --only-personalized, so the run is confined
#               to addresses that have a written email. Without that flag the
#               sender falls back to the generic template for whoever ranks
#               next, which is not what a batch of researched drafts is for.
#   3. sync     push sends and bounces back to the tracker. Render's free tier
#               keeps its database in /tmp and loses it on every spin-down, so
#               the dashboard reads zero until this runs. Doing it here means
#               the numbers are correct by the time you look.
#
# Everything appends to logs/batch-<date>.log. Nothing here probes mailboxes
# over SMTP, so it cannot affect the Spamhaus listing.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

LIMIT="${1:-}"
PER_HOUR="${2:-}"
DRAFTS="${DRAFTS:-drafts-2026-08-15.json}"
PY=./venv/bin/python

mkdir -p logs
LOG="logs/batch-$(date +%Y%m%d-%H%M).log"

say() { printf '\n=== %s === %s\n' "$1" "$(date '+%H:%M:%S')" | tee -a "$LOG"; }

[ -f "$DRAFTS" ] || { echo "no drafts file: $DRAFTS"; exit 1; }

# Refuse to start twice. Two senders sharing one sent_log.json double-send,
# and the daily quota is enforced per process, not per machine.
if pgrep -f "send_emails.py --only-personalized" > /dev/null; then
  echo "a batch is already running, refusing to start another" | tee -a "$LOG"
  exit 0
fi

say "LOAD"
# Loaded for real even on a dry run. Writing personalized_emails.local.json
# sends nothing by itself, and without it the dry run reports "0 with a written
# email" and shows you none of the copy, which is the one thing a dry run is
# for. --only-personalized keeps the eventual send confined to these addresses.
$PY -m sender.send_drafts --drafts "$DRAFTS" --load 2>&1 | tee -a "$LOG"

say "SEND"
if [ -z "$LIMIT" ]; then
  echo "(no limit given, dry run: nothing will be sent)" | tee -a "$LOG"
  $PY send_emails.py --dry-run --only-personalized --limit 15 2>&1 | tee -a "$LOG"
  say "DONE (dry run)"
  echo "to send for real:  ./run_batch.sh 15" | tee -a "$LOG"
  exit 0
fi

PACE=()
[ -n "$PER_HOUR" ] && PACE=(--per-hour "$PER_HOUR")
$PY -u send_emails.py --only-personalized --limit "$LIMIT" "${PACE[@]}" 2>&1 | tee -a "$LOG"

say "SYNC TRACKER"
$PY -u sender/bounce_scan.py 2>&1 | grep -E "Tracker synced|Detected" | tee -a "$LOG"

say "DONE"
echo "log: $LOG" | tee -a "$LOG"
