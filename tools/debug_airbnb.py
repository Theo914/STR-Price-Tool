"""One-off diagnostic: fetches an Airbnb listing URL exactly like the real
adapter does and prints hard facts about the response, instead of guessing
from a 300-char snippet. Run: python tools/debug_airbnb.py <url>
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from parity.adapters.http.airbnb import AIRBNB_NAVIGATION_HEADERS

url = sys.argv[1] if len(sys.argv) > 1 else "https://www.airbnb.co.uk/rooms/4554005"

client = httpx.Client(headers=AIRBNB_NAVIGATION_HEADERS, timeout=20.0, follow_redirects=True)
resp = client.get(url, params={"check_in": "2026-10-04", "check_out": "2026-10-11", "adults": 2, "currency": "EUR"})
html = resp.text

print(f"status: {resp.status_code}")
print(f"final url: {resp.url}")
print(f"length: {len(html)}")

title_m = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
print(f"title: {title_m.group(1).strip() if title_m else '(none found)'}")

print(f"has 'data-deferred-state-0' (exact id string): {'data-deferred-state-0' in html}")
print(f"has 'StaysPdpSections': {'StaysPdpSections' in html}")
print(f"has 'niobeClientData': {'niobeClientData' in html}")

# Show context around every 'captcha' occurrence (case-insensitive) so we
# can tell a real challenge page from a script that just mentions the word.
for m in re.finditer(r"captcha", html, re.IGNORECASE):
    start = max(0, m.start() - 80)
    print(f"'captcha' context @ {m.start()}: ...{html[start:m.start()+80]}...")

# List every script id on the page, the same way we checked in Chrome.
ids = re.findall(r'<script[^>]*\bid="([^"]+)"', html)
print(f"script ids found ({len(ids)}): {ids[:30]}")
