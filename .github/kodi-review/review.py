"""Workflow helpers for the Kodi PR review: last reviewed commit, Kodi 22 files, status, posting."""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
UPSTREAM = os.environ.get("UPSTREAM", "xbmc/xbmc")
RUN_DIR = Path(".kodi-review-run")
MARKER = re.compile(r"<!-- kodi-review sha=([0-9a-f]{40}|) -->")
PIERS_FILE_LIMIT = 40
DISCUSSION_LIMIT = 25_000
REQUIRED = ("summary", "verdict", "kodi22", "kodi22_reason", "next_step", "findings")
BLOCKING_LABEL = re.compile(r"^(Don't merge|On hold|No Jenkins|RFC|WIP)$")
BACKPORT_OF = re.compile(r"backport(?:s| of)?\s+\S*?(?:#|/pull/)(\d+)", re.I)
BACKPORT_TAG = re.compile(r"^\s*\[backport\]\s*", re.I)
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", re.M)
NOT_BUILDS = {"CodeRabbit", "Mergeable"}
BUILD_NAMES = {"default": "Jenkins"}
FAILED = {"failure", "error", "timed_out", "action_required", "startup_failure"}
SEVERITIES = ("Serious", "Moderate", "Minor")
CODE = re.compile(r"(```.*?```|`[^`\n]*`)", re.S)
UPSTREAM_LINK = re.compile(rf"(?:https?://)?(?:www\.)?github\.com/{re.escape(UPSTREAM)}/"
                           r"(?:pull|issues)/(\d+)(?:/[\w/-]*)?(?:[?#][\w=&-]*)?")
UPSTREAM_REF = re.compile(rf"\b{re.escape(UPSTREAM)}#(\d+)\b")
BARE_REF = re.compile(r"(?<![\w&/])#(\d{3,})\b")
MENTION = re.compile(r"(?<![\w/])@(?=[A-Za-z0-9])")
CREDENTIAL = re.compile(r"sk-ant-|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_")
WITHHELD = ("Withheld: the review output contained something that looks like a credential. "
            "Check the run.")
BOT_LOGIN = "github-actions"
PRIOR_SEVERITY = re.compile(r"^\*\*(Serious|Moderate|Minor)\b")
RABBIT = r"auto-generated comment: release notes by coderabbit\.ai -->"
RABBIT_NOTES = re.compile(rf"<!-- This is an {RABBIT}.*?<!-- end of {RABBIT}", re.S)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
UNTICKED = re.compile(r"\n[ \t]*- \[ \][^\n]*")
THREADS_QUERY = """query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) {
    reviewThreads(first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { id isResolved path line comments(first: 20) { nodes { author { login } body } } }
}}}}"""
RESOLVE_MUTATION = """mutation($id: ID!) {
  resolveReviewThread(input: {threadId: $id}) { thread { isResolved } }
}"""


def request(method, path, body=None, raw=False) -> Any:
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


def reviewed_sha():
    """Return the head commit the last finished review covered, or an empty string."""
    comment = summary_comment()
    match = MARKER.match(comment["body"]) if comment else None
    return match.group(1) if match else ""


def cmd_prev():
    """Print the last reviewed commit for the prep step, or nothing for a full review."""
    print(reviewed_sha())


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


def trimmed_body(body):
    """Return a pull request description without template comments, unticked boxes or bot notes."""
    body = HTML_COMMENT.sub("", RABBIT_NOTES.sub("", body or ""))
    return re.sub(r"\n{3,}", "\n\n", UNTICKED.sub("", body)).strip()


def cmd_pr():
    """Write the upstream pull request's title, trimmed description and target to pr.json."""
    pr = request("GET", f"/repos/{UPSTREAM}/pulls/{os.environ['UPSTREAM_PR']}")
    facts = {"number": pr["number"], "title": pr["title"], "body": trimmed_body(pr["body"]),
             "author": pr["user"]["login"], "target": pr["base"]["ref"], "state": pr["state"],
             "draft": pr["draft"]}
    RUN_DIR.mkdir(exist_ok=True)
    (RUN_DIR / "pr.json").write_text(json.dumps(facts, indent=1) + "\n")


def search(query):
    """Search the upstream issues and pull requests, first page only."""
    q = urllib.parse.quote(f"repo:{UPSTREAM} {query}")
    return request("GET", f"/search/issues?q={q}&per_page=20")["items"]


def pr_state(pr):
    """Return a pull request's state as merged, draft, open or closed."""
    if pr.get("merged") or (pr.get("pull_request") or {}).get("merged_at"):
        return "merged"
    return "draft" if pr["state"] == "open" and pr.get("draft") else pr["state"]


def label_problems(title, labels, milestone):
    """List the milestone, label and title rules the upstream mergeable check fails on."""
    problems = []
    if not milestone:
        problems.append("no milestone")
    elif "Abandoned" in milestone:
        problems.append("milestone Abandoned")
    if not any(re.search(r"^v|Infrastructure", name) for name in labels):
        problems.append("no version label")
    problems += [f"labeled {name}" for name in labels if BLOCKING_LABEL.match(name)]
    tag = re.search(r"\[(RFC|WIP)\]", title)
    if tag:
        problems.append(f"{tag.group(0)} in title")
    return problems


def author_activity(pr):
    """Return the latest commit or force push time and the author's last comment or review time."""
    author, pushed, commented = pr["user"]["login"], "", ""
    for event in paged(f"/repos/{UPSTREAM}/issues/{pr['number']}/timeline"):
        kind = event["event"]
        if kind == "committed":
            pushed = max(pushed, event["committer"]["date"])
        elif kind == "head_ref_force_pushed":
            pushed = max(pushed, event["created_at"])
        elif kind == "commented" and event["actor"]["login"] == author:
            commented = max(commented, event["created_at"])
        elif kind == "reviewed" and event["user"]["login"] == author:
            commented = max(commented, event["submitted_at"])
    return pushed, commented


def review_status(pr):
    """Return the standing approvals and change requests, and any push or author comment since."""
    latest = {}
    for review in paged(f"/repos/{UPSTREAM}/pulls/{pr['number']}/reviews"):
        if review["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[review["user"]["login"]] = review
    reviews = [{"login": login, "state": r["state"], "at": r["submitted_at"],
                "team": r["author_association"] in ("MEMBER", "OWNER")}
               for login, r in latest.items() if r["state"] != "DISMISSED"]
    asks = [r for r in reviews if r["state"] == "CHANGES_REQUESTED"]
    if asks:
        pushed, commented = author_activity(pr)
        for r in asks:
            r["author_pushed_since"] = pushed > r["at"]
            r["author_commented_since"] = commented > r["at"]
    return reviews


def build_status(pr):
    """Return the head commit's test builds, naming the failing and pending ones with links."""
    head = pr["head"]["sha"]
    runs = request("GET", f"/repos/{UPSTREAM}/commits/{head}/check-runs?per_page=100")
    states = {run["name"]: (run["conclusion"] or "pending", run.get("details_url"))
              for run in runs["check_runs"]}
    for status in request("GET", f"/repos/{UPSTREAM}/commits/{head}/status")["statuses"]:
        states[status["context"]] = (status["state"], status.get("target_url"))
    builds = {BUILD_NAMES.get(name) or name: value for name, value in states.items()
              if name not in NOT_BUILDS}
    return {"count": len(builds),
            "failing": sorted(n for n, (s, _) in builds.items() if s in FAILED),
            "pending": sorted(n for n, (s, _) in builds.items() if s == "pending"),
            "links": {n: url for n, (_, url) in builds.items() if url}}


def normalized_patch(file):
    """Return a file's patch without hunk line numbers, or its blob sha when it has no patch."""
    patch = file.get("patch")
    return HUNK.sub("@@", patch) if patch is not None else f"blob {file['sha']}"


def differing_files(ours, theirs):
    """List the files whose changes differ between two pull requests, ignoring hunk line numbers."""
    a = {f["filename"]: normalized_patch(f) for f in ours}
    b = {f["filename"]: normalized_patch(f) for f in theirs}
    return sorted(name for name in a.keys() | b.keys() if a.get(name) != b.get(name))


def find_original(pr):
    """Find the master pull request a backport copies, or None."""
    text = f"{pr['title']}\n{pr['body'] or ''}"
    for number in dict.fromkeys(BACKPORT_OF.findall(text) + re.findall(r"#(\d+)", pr["title"])):
        try:
            original = request("GET", f"/repos/{UPSTREAM}/pulls/{number}")
        except urllib.error.HTTPError:
            continue
        if original["base"]["ref"] == "master":
            return original
    title = BACKPORT_TAG.sub("", pr["title"]).replace('"', "").strip()
    for item in search(f'is:pr base:master in:title "{title}"'):
        if item["title"].strip().casefold() == title.casefold():
            return request("GET", f"/repos/{UPSTREAM}/pulls/{item['number']}")
    return None


def find_backports(pr):
    """Find the Piers pull requests that mention a master one or share its title."""
    title = pr["title"].replace('"', "").strip()
    found = {item["number"]: pr_state(item) for item in search(f"is:pr base:Piers {pr['number']}")}
    for item in search(f'is:pr base:Piers in:title "{title}"'):
        if BACKPORT_TAG.sub("", item["title"]).strip().casefold() == title.casefold():
            found[item["number"]] = pr_state(item)
    return [{"number": number, "state": state} for number, state in found.items()]


def backport_status(pr):
    """Return a Piers backport's original, or the backports of a master pull request needing one."""
    if pr["base"]["ref"] == "Piers":
        original = find_original(pr)
        if original is None:
            return {"original": None}
        ours = paged(f"/repos/{UPSTREAM}/pulls/{pr['number']}/files")
        theirs = paged(f"/repos/{UPSTREAM}/pulls/{original['number']}/files")
        return {"original": original["number"], "state": pr_state(original),
                "differs": differing_files(ours, theirs)}
    if "Backport: Needed" in [label["name"] for label in pr["labels"]]:
        return {"backports": find_backports(pr)}
    return None


def cmd_status():
    """Write where the upstream pull request stands to status.json, noting what was unavailable."""
    status, unavailable = {}, []
    try:
        pr = request("GET", f"/repos/{UPSTREAM}/pulls/{os.environ['UPSTREAM_PR']}")
    except Exception:
        pr = None
        unavailable.append("pull request")
    if pr:
        labels = [label["name"] for label in pr["labels"]]
        milestone = (pr["milestone"] or {}).get("title")
        status = {"state": pr_state(pr), "base": pr["base"]["ref"], "head": pr["head"]["sha"],
                  "conflicts": pr["mergeable_state"] == "dirty", "labels": labels,
                  "milestone": milestone,
                  "label_problems": label_problems(pr["title"], labels, milestone)}
        for key, part in (("reviews", review_status), ("builds", build_status),
                          ("backport", backport_status)):
            try:
                status[key] = part(pr)
            except Exception:
                unavailable.append(key)
    status["unavailable"] = unavailable
    RUN_DIR.mkdir(exist_ok=True)
    (RUN_DIR / "status.json").write_text(json.dumps(status, indent=1) + "\n")


def discussion_text(comments):
    """Format people's comments, plus any comment a person replied to, newest first, capped."""
    by_id = {c["id"]: c for c in comments}
    kept = {c["id"]: c for c in comments if (c.get("user") or {}).get("type") == "User"}
    for c in list(kept.values()):
        parent = by_id.get(c.get("in_reply_to_id"))
        if parent:
            kept[parent["id"]] = parent
    entries, size = [], 0
    for c in sorted(kept.values(), key=lambda c: c["created_at"], reverse=True):
        where = f", {c['path']}:{c.get('line') or c.get('original_line')}" if c.get("path") else ""
        login = (c.get("user") or {}).get("login", "ghost")
        entry = f"{login}, {c['created_at'][:10]}{where}\n{(c.get('body') or '').strip()}\n"
        if size + len(entry) > DISCUSSION_LIMIT:
            break
        entries.append(entry)
        size += len(entry)
    return "\n".join(entries)


def cmd_discussion():
    """Write the upstream conversation to discussion.md."""
    number = os.environ["UPSTREAM_PR"]
    comments = list(paged(f"/repos/{UPSTREAM}/issues/{number}/comments"))
    comments += paged(f"/repos/{UPSTREAM}/pulls/{number}/comments")
    RUN_DIR.mkdir(exist_ok=True)
    (RUN_DIR / "discussion.md").write_text(discussion_text(comments))


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


def graphql(query, **variables):
    """Run a GitHub GraphQL query or mutation and return its data."""
    reply = request("POST", "/graphql", {"query": query, "variables": variables})
    if reply.get("errors"):
        raise RuntimeError(reply["errors"])
    return reply["data"]


def open_findings(threads):
    """Return the bot's unresolved threads that nobody else replied to, as earlier findings."""
    return [{"id": t["id"], "path": t["path"], "line": t["line"],
             "finding": t["comments"]["nodes"][0]["body"].split("\n", 1)[0][:160]}
            for t in threads if not t["isResolved"] and t["comments"]["nodes"]
            and all((c["author"] or {}).get("login") == BOT_LOGIN
                    for c in t["comments"]["nodes"])]


def cmd_prior():
    """Write the bot's open findings on the mirrored pull request to prior.json."""
    owner, name = REPO.split("/")
    threads, after = [], None
    while True:
        data = graphql(THREADS_QUERY, owner=owner, name=name,
                       number=int(os.environ["FORK_PR"]), after=after)
        page = data["repository"]["pullRequest"]["reviewThreads"]
        threads += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    RUN_DIR.mkdir(exist_ok=True)
    (RUN_DIR / "prior.json").write_text(json.dumps(open_findings(threads), indent=1) + "\n")


def load_prior():
    """Load the open findings the prep step listed, or none."""
    try:
        prior = json.loads((RUN_DIR / "prior.json").read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return prior if isinstance(prior, list) else []


def open_spots(prior, fixed):
    """Map each still-open earlier finding's file and line to its severity, once per spot."""
    spots = {}
    for f in prior:
        m = PRIOR_SEVERITY.match(f.get("finding") or "")
        if m and f.get("id") not in fixed:
            spots.setdefault((f.get("path"), f.get("line")), m.group(1))
    return spots


def load_status():
    """Load the status facts, or an empty dict when the status step wrote nothing usable."""
    try:
        return json.loads((RUN_DIR / "status.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def unlinked(text):
    """Return a copy with @mentions, upstream links and PR numbers outside code unlinked."""
    parts = CODE.split(text)
    for i in range(0, len(parts), 2):
        part = UPSTREAM_LINK.sub(r"upstream PR \1", parts[i])
        part = UPSTREAM_REF.sub(r"upstream PR \1", part)
        part = BARE_REF.sub(r"PR \1", part)
        parts[i] = MENTION.sub("@​", part)
    return "".join(parts)


def finding_text(finding):
    """Format one finding as comment markdown, its title in bold above the detail."""
    return (f"**{finding['severity']}: {finding['title']}**\n\n{finding['problem']}\n\n"
            f"**Fix:** {finding['fix']}")


def reviewer_text(review):
    """Format one reviewer's standing approval or change request."""
    who = review["login"] + (" (team)" if review["team"] else "")
    if review["state"] == "APPROVED":
        return f"approved by {who}"
    answers = [word for word, done in (("pushed", review.get("author_pushed_since")),
                                       ("commented", review.get("author_commented_since"))) if done]
    answer = f"author {' and '.join(answers)} since" if answers else "no answer yet"
    return f"changes requested by {who}, {answer}"


def backport_text(backport):
    """Format the backport facts that close the Kodi 22 line, or nothing when there are none."""
    if not backport:
        return ""
    if "backports" in backport:
        found = ", ".join(f"PR {b['number']} {b['state']}" for b in backport["backports"])
        return f" Backport: {found or 'none yet'}."
    if backport["original"] is None:
        return " Original master PR not found."
    state = {"merged": "merged", "closed": "closed unmerged"}.get(backport["state"],
                                                                 "not merged yet")
    differs = backport["differs"]
    same = f"changes differ in {', '.join(differs)}" if differs else "same changes"
    return f" Original PR {backport['original']} {state}, {same}."


def status_lines(status, result):
    """Format the Where it stands lines, skipping any with nothing to say."""
    lines = []
    if status.get("reviews"):
        lines.append("**Reviews:** " + "; ".join(reviewer_text(r) for r in status["reviews"]))
    builds = status.get("builds") or {}
    links = builds.get("links") or {}
    if builds.get("failing"):
        lines.append(f"**Test builds:** failing ({linked_builds(builds['failing'], links)})")
    elif builds.get("pending"):
        lines.append(f"**Test builds:** pending ({linked_builds(builds['pending'], links)})")
    elif builds.get("count"):
        lines.append("**Test builds:** passing")
    if status.get("conflicts"):
        lines.append(f"**Conflicts:** yes, needs a rebase onto {status['base']}")
    if status.get("label_problems"):
        lines.append("**Labels:** " + ", ".join(status["label_problems"]))
    lines.append(f"**Kodi 22:** {result['kodi22']}. {result['kodi22_reason']}"
                 + backport_text(status.get("backport")))
    lines.append(f"**Next:** {result['next_step']}")
    if status.get("unavailable"):
        lines.append("**Unavailable:** " + ", ".join(status["unavailable"]))
    return lines


def linked_builds(names, links):
    """Format build names, each linked to its job when the status gave one."""
    return ", ".join(f"[{name}]({links[name]})" if name in links else name for name in names)


def place_findings(findings, lines):
    """Place findings as comments on changed lines or as summary entries, without posting."""
    inline, loose = [], []
    for finding in findings:
        if finding["line"] in lines.get(finding["path"], ()):
            inline.append({"path": finding["path"], "line": finding["line"], "side": "RIGHT",
                           "body": finding_text(finding)})
        else:
            loose.append(f"`{finding['path']}:{finding['line']}`\n{finding_text(finding)}")
    return inline, loose


def settled_verdict(result, status):
    """Return the verdict matched to findings; a ready one waits on failing builds or conflicts."""
    verdict, findings = result["verdict"], result["findings"]
    if verdict == "Ready to merge" and findings:
        minor = all(f["severity"] == "Minor" for f in findings)
        verdict = "Merge after small fixes" if minor else "Needs more work"
    elif verdict == "Merge after small fixes" and not findings:
        verdict = "Ready to merge"
    if verdict == "Ready to merge":
        waits = [wait for wait, blocked in
                 (("the test builds pass", (status.get("builds") or {}).get("failing")),
                  ("the conflicts are resolved", status.get("conflicts"))) if blocked]
        if waits:
            verdict += " once " + " and ".join(waits)
    return verdict


def verdict_line(result, status):
    """Format the settled verdict, then the problem count by severity when there are any."""
    findings = result["findings"]
    verdict = settled_verdict(result, status)
    if not findings:
        return verdict
    counts = [f"{n} {s}" for s in SEVERITIES if (n := sum(f["severity"] == s for f in findings))]
    detail = findings[0]["severity"] if len(findings) == 1 else ", ".join(counts)
    plural = "s" if len(findings) != 1 else ""
    return f"{verdict} · {len(findings)} problem{plural} ({detail})"


def summary_text(result, status, head, loose, rerun, resolved=0, still_open=()):
    """Format the summary comment, verdict as a heading and the Where it stands facts as a list."""
    counted = dict(result, findings=result["findings"] + list(still_open))
    parts = [f"### {verdict_line(counted, status)}\n{result['summary']}"]
    if loose:
        parts.append("Not on a changed line:\n\n" + "\n\n".join(loose))
    parts.append("---")
    lines = status_lines(status, result)
    parts.append("**Where it stands**\n" + "\n".join(f"- {line}" for line in lines))
    footer = f"Reviewed up to {head[:12]}{' (new commits only)' if rerun else ''}."
    if resolved:
        footer += f" Resolved {resolved} earlier finding{'s' if resolved != 1 else ''}."
    if still_open:
        footer += f" {len(still_open)} earlier finding{'s' if len(still_open) != 1 else ''}"
        footer += " still open."
    parts.append(f"<sub>{footer}</sub>")
    return "\n\n".join(parts)


def upsert_summary(sha, text):
    """Upsert the summary comment, tagged with the commit the last finished review covered."""
    body = f"<!-- kodi-review sha={sha} -->\n{text}"
    comment = summary_comment()
    if comment:
        request("PATCH", f"/repos/{REPO}/issues/comments/{comment['id']}", {"body": body})
    else:
        request("POST", f"/repos/{REPO}/issues/{os.environ['FORK_PR']}/comments", {"body": body})


def resolve_threads(ids):
    """Resolve review threads, logging any that fail; return how many it resolved."""
    resolved = 0
    for tid in ids:
        try:
            graphql(RESOLVE_MUTATION, id=tid)
            resolved += 1
        except (urllib.error.URLError, RuntimeError, KeyError) as err:
            print(f"Could not resolve {tid}: {err}")
    return resolved


def cmd_post():
    """Post new findings, resolve fixed earlier ones, or note a failure; exit 1 on a credential."""
    head = os.environ["HEAD_SHA"]
    result = load_result()
    if result is None:
        upsert_summary(reviewed_sha(),
                       "The review did not finish, so nothing was posted. Re-run it.")
        return
    prior = load_prior()
    listed = {f.get("id") for f in prior}
    fixed = [tid for tid in dict.fromkeys(result.get("fixed") or []) if tid in listed]
    spots = open_spots(prior, fixed)
    rank = SEVERITIES.index
    result = dict(result, findings=[
        f for f in result["findings"] if (f["path"], f["line"]) not in spots
        or rank(f["severity"]) < rank(spots[(f["path"], f["line"])])])
    lines = {f["filename"]: attachable_lines(f.get("patch")) for f in changed_files()}
    inline, loose = place_findings(result["findings"], lines)
    rerun = (RUN_DIR / "new.diff").exists()
    still_open = [{"severity": severity} for severity in spots.values()]
    status = load_status()
    summary = unlinked(summary_text(result, status, head, loose, rerun, len(fixed), still_open))
    for comment in inline:
        comment["body"] = unlinked(comment["body"])
    if any(CREDENTIAL.search(text) for text in [summary] + [c["body"] for c in inline]):
        upsert_summary(reviewed_sha(), WITHHELD)
        sys.exit(1)
    resolved = resolve_threads(fixed)
    if resolved != len(fixed):
        summary = unlinked(summary_text(result, status, head, loose, rerun, resolved,
                                        still_open))
    if inline:
        request("POST", f"/repos/{REPO}/pulls/{os.environ['FORK_PR']}/reviews",
                {"commit_id": head, "event": "COMMENT", "comments": inline})
    upsert_summary(head, summary)


def cmd_render():
    """Render the summary comment a status.json and a result.json give, without posting."""
    status = json.loads(Path(sys.argv[2]).read_text())
    result = json.loads(Path(sys.argv[3]).read_text())
    _, loose = place_findings(result["findings"], {})
    print(unlinked(summary_text(result, status, status.get("head", ""), loose, False)))


def usage_lines(messages):
    """List each tool call with the size of its result, then the token totals; no contents."""
    calls, lines = {}, []
    for m in messages:
        content = (m.get("message") or {}).get("content")
        for part in content if isinstance(content, list) else []:
            if part.get("type") == "tool_use":
                given = part.get("input") or {}
                target = given.get("file_path") or given.get("path") or ""
                span = f"{given.get('offset', 0)}+{given['limit']}" if "limit" in given else ""
                calls[part.get("id")] = " ".join(
                    filter(None, [part.get("name"), target, given.get("pattern"), span]))
            elif part.get("type") == "tool_result":
                body = part.get("content")
                size = len(body) if isinstance(body, str) else len(json.dumps(body))
                lines.append(f"{size:>8} chars  {calls.get(part.get('tool_use_id'), '?')}")
    for m in messages:
        if m.get("type") == "result":
            lines.append(f"tokens {json.dumps(m.get('usage'))} turns {m.get('num_turns')} "
                         f"cost {m.get('total_cost_usd')}")
    return lines


def cmd_usage():
    """Print the review's tool calls and token totals from its execution file."""
    print("\n".join(usage_lines(json.loads(Path(sys.argv[2]).read_text()))))


COMMANDS = {"pr": cmd_pr, "prev": cmd_prev, "prior": cmd_prior, "piers": cmd_piers,
            "status": cmd_status,
            "discussion": cmd_discussion, "post": cmd_post, "render": cmd_render,
            "usage": cmd_usage}

if __name__ == "__main__":
    COMMANDS[sys.argv[1]]()
