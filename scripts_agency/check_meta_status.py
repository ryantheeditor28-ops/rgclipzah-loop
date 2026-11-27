"""Check whether the Meta business-account restriction touches organic posting.

Reads FB_PAGE_ACCESS_TOKEN / FB_PAGE_ID from .env and asks the Graph API
about the Page: is it still alive, is it still accepting organic content,
and does the token still work?
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent

for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)

tok = os.environ.get("FB_PAGE_ACCESS_TOKEN", "")
pid = os.environ.get("FB_PAGE_ID", "")
print(f"page id : {pid}")
print(f"token   : {len(tok)} chars, prefix {tok[:6]}...")

FIELDS = ("id,name,access_token,restricted_posting,"
          "is_published,instagram_actor_id,fan_count,category")

r = requests.get(
    f"https://graph.facebook.com/v21.0/{pid}",
    params={"fields": FIELDS, "access_token": tok},
    timeout=30,
)
print(f"GET /{pid} -> HTTP {r.status_code}")
data = r.json()
if "error" in data:
    print("ERROR:", json.dumps(data["error"], indent=1)[:900])
else:
    data.pop("access_token", None)
    print(json.dumps(data, indent=1))

# Does the page still accept an organic read of its own recent posts?
r2 = requests.get(
    f"https://graph.facebook.com/v21.0/{pid}/posts",
    params={"fields": "id,created_time,permalink_url", "limit": 3, "access_token": tok},
    timeout=30,
)
print(f"\nGET /{pid}/posts -> HTTP {r2.status_code}")
d2 = r2.json()
if "error" in d2:
    print("ERROR:", json.dumps(d2["error"], indent=1)[:900])
else:
    for p in d2.get("data", []):
        print(f"  {p.get('created_time')}  {p.get('permalink_url')}")

# Business/ads account status, if the token can see it.
for edge in ("businesses", "adaccounts"):
    try:
        r3 = requests.get(
            "https://graph.facebook.com/v21.0/me/" + edge,
            params={"fields": "id,name,account_status,disable_status,restrictions",
                    "access_token": tok},
            timeout=30,
        )
        d3 = r3.json()
        print(f"\nGET /me/{edge} -> HTTP {r3.status_code}")
        if "error" in d3:
            print("  ", json.dumps(d3["error"])[:300])
        else:
            print("  ", json.dumps(d3)[:700])
    except Exception as exc:  # noqa: BLE001
        print(f"\n/me/{edge} failed: {exc}")
