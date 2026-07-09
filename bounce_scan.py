#!/usr/bin/env python3
"""
bounce_scan.py — Standalone auto bounce scanner.
Run this independently via macOS LaunchAgent every 2 hours.
It checks Gmail IMAP for bounces, classifies them, syncs to the Render tracker,
and also re-syncs all historical data (bulk_sync) in case Render restarted.
"""
import sys
import os

# Make sure we run from the project directory
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from send_emails import CONFIG, check_and_sync_bounces, sync_tracker_from_logs

if __name__ == "__main__":
    cfg = CONFIG.copy()
    print("[bounce_scan] Starting auto bounce check + tracker sync...")
    check_and_sync_bounces(cfg)
    sync_tracker_from_logs(cfg)
    print("[bounce_scan] Done.")
