#!/usr/bin/env python3
"""mute_tracking.py -- stop the tracker counting opens while you read your own mail.

Opening your own Sent folder registers as the recipient reading the email, and
the tracker cannot tell the difference. Gmail never loads the pixel from the
reader's browser: it proxies every image through its own servers, so the hit
arrives from 66.249.x.x with a ggpht.com user agent whether the reader is the
recipient or you. The Sent copy and the delivered copy carry the identical URL.

Nothing in the request distinguishes them, so the only reliable fix is to say
in advance that the next few minutes are yours.

Usage:
  python3 mute_tracking.py            # 10 minutes
  python3 mute_tracking.py 30
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import send_emails as se  # noqa: E402


def main() -> int:
    minutes = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    cfg = se.CONFIG
    url = cfg.get("tracker_url", "").rstrip("/")
    if not url:
        print("TRACKER_URL is not set in .env")
        return 1
    req = urllib.request.Request(
        f"{url}/api/mute",
        data=json.dumps({"minutes": minutes}).encode(),
        headers=se.tracker_headers(cfg, "application/json"),
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
        print(f"  opens ignored for the next {d['minutes']} minutes "
              f"(until {d['muted_until_utc']} UTC)")
        print("  go read your Sent folder; nothing will be recorded")
        return 0
    except Exception as e:
        print(f"  failed: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
