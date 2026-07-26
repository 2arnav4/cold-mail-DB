#!/usr/bin/env python3
"""
check_verify_services.py — pings the SMTP probe against a test address and
reports the raw verdict, plus whether the domain looks catch-all.

Usage: python3 check_verify_services.py [test_email]
Default test address is a real, known-deliverable Gmail inbox. Pass a
non-Gmail address as an arg for a more meaningful result, since Gmail/Google
Workspace domains are themselves catch-all-like at RCPT time.
"""
import os
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))


def _load_env(path=".env"):
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())
    except FileNotFoundError:
        pass


_load_env()

from email_verify import verify_smtp, is_catchall_domain

TEST_EMAIL = sys.argv[1] if len(sys.argv) > 1 else "singlaarnav2405@gmail.com"


def main():
    domain = TEST_EMAIL.split("@", 1)[1]
    print(f"Testing SMTP probe with: {TEST_EMAIL}")

    result = verify_smtp(TEST_EMAIL)
    if result is None:
        print("  INCONCLUSIVE — MX/connection issue, or server didn't give a clear answer")
    else:
        print(f"  Verdict: {'accepted (250)' if result else 'rejected (explicit bad address)'}")

    print(f"\nChecking whether {domain} is catch-all...")
    catchall = is_catchall_domain(domain)
    if catchall is None:
        print("  INCONCLUSIVE — couldn't determine")
    elif catchall:
        print("  CATCH-ALL — this domain accepts RCPT TO for anyone; a 'valid' result here can't be trusted")
    else:
        print("  Not catch-all — the domain properly rejects unknown mailboxes, so a 'valid' result is trustworthy")


if __name__ == "__main__":
    main()
