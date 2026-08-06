#!/bin/bash
# Re-push sends and bounces to the tracker.
#
# The Render free tier has no persistent disk, so the tracker stores its
# database in /tmp and loses it on every deploy, restart and idle spin-down.
# The local sent_log*.json files are the real record; this restores the mirror
# from them.
#
# No SMTP is involved: IMAP to read bounce notifications, HTTPS to sync. It
# does not probe mailboxes and cannot affect the Spamhaus listing.
cd /Users/arnavsingla/Desktop/Cold-Mail-DB || exit 1
./venv/bin/python -u sender/bounce_scan.py >> tracker_sync.log 2>&1
