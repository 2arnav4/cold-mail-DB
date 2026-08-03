#!/usr/bin/env python3
"""extract_brand.py — pull a company's real brand colours and fonts off their site.

outreach/companies/<slug>.yaml has a `brand.primary` and `brand.accent` that the
deck template renders with. Guessing those from a screenshot gets you close and
wrong; reading the stylesheet gets you the actual hex the company ships.

Fetches the homepage plus any same-origin stylesheets, then ranks:
  - CSS custom properties named like --primary / --brand / --accent, which is
    where a design system states its intent
  - otherwise, hex codes by frequency, skipping near-white and near-black since
    every site is mostly text on background

Fonts come from font-family declarations and Google Fonts links.

Usage:
  python3 extract_brand.py hiredue.com bedrindia.com cnear.in
"""
import re
import sys
from collections import Counter
from urllib.parse import urljoin, urlparse

import requests

TIMEOUT = 12
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

HEX = re.compile(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
VAR_COLOUR = re.compile(
    r"--([\w-]*(?:primary|brand|accent|secondary|main|theme)[\w-]*)\s*:\s*([^;]+);", re.I)
FONT_FAMILY = re.compile(r"font-family\s*:\s*([^;}]+)[;}]", re.I)
GOOGLE_FONT = re.compile(r"fonts\.googleapis\.com/css2?\?family=([^&\"']+)", re.I)
STYLESHEET = re.compile(r'<link[^>]+rel=["\']stylesheet["\'][^>]*href=["\']([^"\']+)["\']', re.I)


def norm_hex(h: str) -> str:
    h = h.lower().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return "#" + h


def luminance(h: str) -> float:
    r, g, b = (int(h[i:i + 2], 16) for i in (1, 3, 5))
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255


def saturation(h: str) -> float:
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
    mx, mn = max(r, g, b), min(r, g, b)
    return 0.0 if mx == 0 else (mx - mn) / mx


def fetch(url: str) -> str:
    r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": UA}, allow_redirects=True)
    r.raise_for_status()
    return r.text


def analyse(domain: str) -> dict:
    base = domain if domain.startswith("http") else f"https://{domain}"
    try:
        html = fetch(base)
    except Exception as e:
        return {"domain": domain, "error": str(e)}

    css = ""
    for href in STYLESHEET.findall(html)[:6]:
        url = urljoin(base, href)
        if urlparse(url).netloc != urlparse(base).netloc:
            continue  # skip CDNs, their palettes are not the company's
        try:
            css += fetch(url)
        except Exception:
            continue

    blob = html + css

    # Named custom properties first: a variable called --primary is the company
    # telling you what its primary colour is, which beats counting pixels.
    declared = {}
    for name, value in VAR_COLOUR.findall(blob):
        m = HEX.search(value)
        if m:
            declared.setdefault(name.lower(), norm_hex(m.group(0)))

    counts = Counter(norm_hex(m.group(0)) for m in HEX.finditer(blob))
    branded = [
        (h, n) for h, n in counts.most_common(60)
        if 0.06 < luminance(h) < 0.93 and saturation(h) > 0.25
    ]

    fonts = []
    for decl in FONT_FAMILY.findall(blob):
        first = decl.split(",")[0].strip().strip("'\"")
        if first and not first.startswith("var(") and first.lower() not in (
                "inherit", "initial", "unset", "sans-serif", "serif", "monospace"):
            fonts.append(first)
    for fam in GOOGLE_FONT.findall(blob):
        fonts.append(fam.split(":")[0].replace("+", " "))

    return {
        "domain": domain,
        "declared": declared,
        "top_colours": branded[:6],
        "fonts": [f for f, _ in Counter(fonts).most_common(4)],
    }


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    for domain in sys.argv[1:]:
        r = analyse(domain)
        print(f"\n{r['domain']}")
        if r.get("error"):
            print(f"  fetch failed: {r['error'][:90]}")
            continue
        if r["declared"]:
            print("  declared brand variables:")
            for k, v in list(r["declared"].items())[:6]:
                print(f"    --{k:24} {v}")
        if r["top_colours"]:
            print("  most used saturated colours:")
            for h, n in r["top_colours"]:
                print(f"    {h}  ({n} occurrences)")
        print(f"  fonts: {', '.join(r['fonts']) or 'none declared'}")


if __name__ == "__main__":
    main()
