#!/bin/bash
# Wake the tracker, restore its data, then open the dashboard.
#
# The Render free tier sleeps a service after ~15 minutes idle and keeps the
# database in /tmp, so a freshly-woken instance always shows zeros. The local
# sent_log*.json files are the real record; this pushes them back up before you
# look, which is why the dashboard is empty if you open it any other way.
cd /Users/arnavsingla/Desktop/Cold-Mail-DB || exit 1

echo "waking the tracker..."
curl -s -o /dev/null --max-time 90 https://cold-mail-tracker-qmmh.onrender.com/ping

echo "restoring sends and bounces from local logs..."
./venv/bin/python -u sender/bounce_scan.py 2>&1 | grep -E "Tracker synced|Detected"

KEY=$(grep '^TRACKER_SECRET=' .env | cut -d= -f2-)
open "https://cold-mail-tracker-qmmh.onrender.com/stats?key=$KEY"
