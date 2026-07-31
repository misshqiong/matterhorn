from __future__ import annotations

import json
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8000/healthz") as response:
    health = json.load(response)
request = urllib.request.Request(
    "http://127.0.0.1:8000/v1/list_matters",
    data=b'{"scope_id":"service-demo"}',
    headers={"content-type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request) as response:
    matters = json.load(response)
print(f"health={health['status']}")
print(f"matters={len(matters)}")
