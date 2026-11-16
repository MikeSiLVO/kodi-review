"""Redeliver: replay the App's recent webhook deliveries that no attempt got through."""

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.github.com"
WINDOW = timedelta(hours=2)
ATTEMPT_LIMIT = 3
NEXT_PAGE = re.compile(r'<([^>]+)>;\s*rel="next"')


def part(data):
    """Encode bytes as a JWT part: base64url without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def app_jwt(client_id, key, now):
    """Sign a GitHub App JWT that lasts nine minutes, with openssl."""
    claims = json.dumps({"iat": now - 60, "exp": now + 540, "iss": client_id}).encode()
    signed = part(b'{"alg":"RS256","typ":"JWT"}') + b"." + part(claims)
    with tempfile.NamedTemporaryFile("w") as pem:
        pem.write(key)
        pem.flush()
        signature = subprocess.run(["openssl", "dgst", "-sha256", "-sign", pem.name],
                                   input=signed, capture_output=True, check=True).stdout
    return (signed + b"." + part(signature)).decode()


def call(method, url, token):
    """Call the App API as the App; return the decoded JSON and the next page's URL or None."""
    req = urllib.request.Request(
        url if url.startswith("http") else API + url, method=method,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "kodi-review"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
        link = NEXT_PAGE.search(resp.headers.get("Link") or "")
    return (json.loads(data) if data else None), (link[1] if link else None)


def delivered(item):
    """Return the time GitHub made a delivery attempt."""
    return datetime.fromisoformat(item["delivered_at"].replace("Z", "+00:00"))


def recent(token, since):
    """Return the App's delivery attempts made since a time, newest first."""
    found, url = [], "/app/hook/deliveries?per_page=100"
    while url:
        page, url = call("GET", url, token)
        found += page or []
        if not page or delivered(page[-1]) < since:
            break
    return [item for item in found if delivered(item) >= since]


def undelivered(attempts):
    """Return the newest attempt of each event first sent in the list, none 2xx, under the limit."""
    events = {}
    for item in attempts:
        events.setdefault(item["guid"], []).append(item)
    owed = []
    for tries in events.values():
        if (any(200 <= (t.get("status_code") or 0) < 300 for t in tries)
                or all(t.get("redelivery") for t in tries) or len(tries) >= ATTEMPT_LIMIT):
            continue
        owed.append(max(tries, key=delivered))
    return owed


def main():
    """Replay recent events owed a retry, or list them on a dry run; exit 1 if a replay fails."""
    now = time.time()
    token = app_jwt(os.environ["APP_CLIENT_ID"], os.environ["APP_KEY"], int(now))
    since = datetime.fromtimestamp(now, timezone.utc) - WINDOW
    attempts = recent(token, since)
    owed = undelivered(attempts)
    print(f"{len(attempts)} delivery attempts since {since:%H:%M} UTC, {len(owed)} to replay")
    errors = 0
    for item in owed:
        tries = sum(1 for a in attempts if a["guid"] == item["guid"])
        what = (f"{item['guid']} {item['event']}.{item.get('action') or '-'} after {tries} "
                f"tries, last {item.get('status_code')} {item.get('status')}")
        if os.environ.get("DRY_RUN") == "true":
            print(f"would replay {what}")
            continue
        try:
            call("POST", f"/app/hook/deliveries/{item['id']}/attempts", token)
            print(f"replayed {what}")
        except urllib.error.URLError as err:
            print(f"could not replay {what}: {err}")
            errors += 1
    if errors:
        sys.exit(f"{errors} replays could not start")


if __name__ == "__main__":
    main()
