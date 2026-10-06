"""Workflow helpers for the Kodi PR review: last reviewed commit, Kodi 22 files, posting."""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
REPO = os.environ["GITHUB_REPOSITORY"]
UPSTREAM = os.environ.get("UPSTREAM", "xbmc/xbmc")
RUN_DIR = Path(".kodi-review-run")
MARKER = re.compile(r"<!-- kodi-review sha=([0-9a-f]{40}) -->")
PIERS_FILE_LIMIT = 40
REQUIRED = ("summary", "verdict", "kodi22", "kodi22_reason", "findings")


def request(method, path, body=None, raw=False):
    """Request a GitHub API endpoint and return the decoded JSON, or a file's bytes."""
    accept = "application/vnd.github.raw+json" if raw else "application/vnd.github+json"
    req = urllib.request.Request(
        path if path.startswith("http") else API + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {os.environ['GH_TOKEN']}", "Accept": accept,
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "kodi-review"})
    with urllib.request.urlopen(req) as resp:
        data = resp.read()
    if raw:
        return data
    return json.loads(data) if data else None


def paged(path):
    """Yield every item of a paginated list endpoint."""
    page = 1
    while True:
        items = request("GET", f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
        yield from items
        if len(items) < 100:
            return
        page += 1


def summary_comment():
    """Return the bot's summary comment on the mirrored pull request, or None."""
    for comment in paged(f"/repos/{REPO}/issues/{os.environ['FORK_PR']}/comments"):
        if comment["user"]["type"] == "Bot" and MARKER.match(comment["body"]):
            return comment
    return None


def changed_files():
    """Return the mirrored pull request's files with their patches."""
    return list(paged(f"/repos/{REPO}/pulls/{os.environ['FORK_PR']}/files"))


def cmd_prev():
    """Print the head commit the last review covered, or nothing."""
    comment = summary_comment()
    print(MARKER.match(comment["body"]).group(1) if comment else "")


def cmd_piers():
    """Save Kodi 22 copies of the touched files that already existed and list any not saved."""
    missing, skipped = [], []
    names = [f["filename"] for f in changed_files() if f["status"] != "added"]
    for name in names[:PIERS_FILE_LIMIT]:
        url = f"/repos/{UPSTREAM}/contents/{urllib.parse.quote(name)}?ref=Piers"
        try:
            data = request("GET", url, raw=True)
        except urllib.error.HTTPError as err:
            (missing if err.code == 404 else skipped).append(name)
            continue
        dest = RUN_DIR / "piers" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    skipped += names[PIERS_FILE_LIMIT:]
    lines = [f"Not in Kodi 22: {n}" for n in missing] + [f"Not fetched: {n}" for n in skipped]
    (RUN_DIR / "piers.txt").write_text("\n".join(lines) + "\n" if lines else "")


def attachable_lines(patch):
    """Return the line numbers on the new side of a patch that a review comment can point at."""
    lines, new = set(), 0
    for row in (patch or "").splitlines():
        hunk = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", row)
        if hunk:
            new = int(hunk.group(1))
        elif not row.startswith(("-", "\\")):
            lines.add(new)
            new += 1
    return lines


def load_result():
    """Load the structured review result, or None when it is missing or malformed."""
    try:
        result = json.loads(os.environ.get("RESULT") or "null")
    except json.JSONDecodeError:
        return None
    if not isinstance(result, dict) or any(key not in result for key in REQUIRED):
        return None
    return result


def finding_text(finding):
    """Format one finding as comment markdown."""
    return f"**{finding['severity']}:** {finding['problem']}\n\n**Fix:** {finding['fix']}"


def upsert_summary(head, text):
    """Upsert the summary comment, tagged with the head commit it covers."""
    body = f"<!-- kodi-review sha={head} -->\n{text}"
    comment = summary_comment()
    if comment:
        request("PATCH", f"/repos/{REPO}/issues/comments/{comment['id']}", {"body": body})
    else:
        request("POST", f"/repos/{REPO}/issues/{os.environ['FORK_PR']}/comments", {"body": body})


def cmd_post():
    """Post findings inline where they fit, the rest in the summary comment, or note a failure."""
    head = os.environ["HEAD_SHA"]
    result = load_result()
    if result is None:
        upsert_summary(head, "The review did not finish, so nothing was posted. Re-run it.")
        return
    lines = {f["filename"]: attachable_lines(f.get("patch")) for f in changed_files()}
    inline, loose = [], []
    for finding in result["findings"]:
        if finding["line"] in lines.get(finding["path"], ()):
            inline.append({"path": finding["path"], "line": finding["line"], "side": "RIGHT",
                           "body": finding_text(finding)})
        else:
            loose.append(f"`{finding['path']}:{finding['line']}`\n{finding_text(finding)}")
    if inline:
        request("POST", f"/repos/{REPO}/pulls/{os.environ['FORK_PR']}/reviews",
                {"commit_id": head, "event": "COMMENT", "comments": inline})
    count = len(result["findings"])
    verdict = f"**{result['verdict']}** · Kodi 22: **{result['kodi22']}**."
    parts = [f"{verdict} {result['kodi22_reason']}", result["summary"],
             f"{count} problem{'s' if count != 1 else ''}, {len(inline)} on the changed lines."
             if count else "No problems found."]
    if loose:
        parts.append("Not on a changed line:\n\n" + "\n\n".join(loose))
    rerun = " (new commits only)" if (RUN_DIR / "new.diff").exists() else ""
    parts.append(f"<sub>Reviewed up to {head[:12]}{rerun}.</sub>")
    upsert_summary(head, "\n\n".join(parts))


COMMANDS = {"prev": cmd_prev, "piers": cmd_piers, "post": cmd_post}

if __name__ == "__main__":
    COMMANDS[sys.argv[1]]()
