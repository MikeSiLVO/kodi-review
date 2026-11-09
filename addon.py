"""Add-on check: run kdk's add-on checker on a repo-scripts pull request, plus the bot's rules."""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any

from review import (CREDENTIAL, REPO, RUN_DIR, WITHHELD, addon_build, addon_comments,
                    hide_summaries, paged, request, unlinked)
from skin import (ADDON_XML_LIMIT, COMMENT_LIMIT, REPORT_LIMIT, SEVERITY_WORDS, USER_AGENT,
                  OutOfScope, SkinError, check_public, check_sandbox, code, commits_checked,
                  compared, download, existing, findings, git_tree, kdk_link, plain, plural,
                  problems, report_rows, run_kdk, run_link, sandbox_command, severity_section,
                  skin_part, verdict)

RELEASES = {"matrix": "Kodi 19 (Matrix)", "nexus": "Kodi 20 (Nexus)",
            "omega": "Kodi 21 (Omega)", "piers": "Kodi 22 (Piers)"}
ADDON_WORDS = {"title": "Add-on check", "noun": "add-on", "releases": RELEASES, "listed": True}
BOT_RULES = {"pr.one-addon": "One add-on folder per pull request",
             "pr.single-commit": "One commit per add-on update",
             "pr.merge-commit": "Merge commit in the pull request",
             "pr.commit-subject": "Commit subject is not [<id>] <version>",
             "pr.title": "Pull request title is not [<id>] <version>",
             "pr.base-branch": "Target is not a Kodi release branch",
             "pr.removal": "Add-on deleted instead of marked broken",
             "addon.url-broken": "Metadata link does not open",
             "addon.url-redirect": "Metadata link redirects elsewhere",
             "addon.provider-changed": "Provider or submitter changed"}
OLD_RELEASES = ("gotham", "helix", "isengard", "jarvis", "krypton", "leia")
INDEX_URL = "https://mirrors.kodi.tv/addons/{}/addons.xml.gz"
INDEX_RETRY_PAUSES = (10, 30)
RELEASED_RULES = ("addon.version-not-higher",)
METADATA_POINTS = ("xbmc.addon.metadata", "kodi.addon.metadata")
LINK_TAGS = ("forum", "source", "website")
WEB_SCHEMES = ("https", "http")
QUIET_HOSTS = ("forum.kodi.tv", "kodi.wiki")
LINK_TIMEOUT = 10
RETRY_STATUSES = (403, 405, 429)
BROWSER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                 "Chrome/130.0 Safari/537.36")
RELEASE_TITLE = re.compile(r"\[([^\]\s@]+)(?:@([a-z]+))?\] v?(\S+)")
PULL_CHECK = "Pull request"
LINK_CHECK = "Links"
PYTHONS = {"matrix": "3.8", "nexus": "3.8", "omega": "3.8",
           "piers": "3.14 in Kodi's own builds; Linux distro builds use the distro's Python"}
LEFT_OUT = re.compile(r"\.(po|pot|png|jpe?g|gif|webp|bmp|ico|tiff?|svg|ttf|otf|woff2?|zip|gz|7z|"
                      r"rar|mp3|mp4|mkv|ogg|wav|flac|db|sqlite|so|dll|dylib|exe|pyc|pyo|xbt)$",
                      re.I)
DIFF_SECTION = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)
PATHS_LISTED = 40
FACTS_FILE = Path(__file__).resolve().parent / "ADDON_FACTS.md"


def bot_row(check, rule, severity, message, file=""):
    """Return a finding the bot makes itself, shaped like a new row of kdk compare."""
    return {"check": check, "rule": rule, "subject": "", "severity": severity, "file": file,
            "line": 0, "message": message, "muted": False, "reason": "", "status": "new",
            "count": 1}


def pull_request():
    """Return the open pull request, its merge base, its files and its commits."""
    pr = os.environ["FORK_PR"]
    meta = request("GET", f"/repos/{REPO}/pulls/{pr}")
    if meta["state"] != "open":
        raise OutOfScope("the pull request is closed")
    base = request("GET", f"/repos/{REPO}/compare/{meta['base']['ref']}...{meta['head']['sha']}")
    files = list(paged(f"/repos/{REPO}/pulls/{pr}/files"))
    commits = list(paged(f"/repos/{REPO}/pulls/{pr}/commits"))
    return meta, base["merge_base_commit"]["sha"], files, commits


def changed_places(files):
    """Return the top-level folders the changed files are in, and the files at the repo root."""
    names = [f["filename"] for f in files]
    return sorted({n.split("/")[0] for n in names if "/" in n}), sorted(n for n in names
                                                                         if "/" not in n)


def names_release(text, addon, branch):
    """Whether subject or title reads [<id>] <version> for the add-on, v and @<target> allowed."""
    match = RELEASE_TITLE.fullmatch(text.strip())
    return (bool(match) and match[1] == addon["id"] and match[2] in (None, branch)
            and match[3] == addon["version"])


def commit_rows(commits, folders, loose):
    """Check what the pull request changes, its commit count and any merge commit."""
    rows = []
    if len(folders) > 1 or loose:
        places = ", ".join([*folders, *loose][:5])
        rows.append(bot_row(PULL_CHECK, "pr.one-addon", "error",
                            f"The pull request changes more than one add-on folder or files "
                            f"outside one: {places}"))
    if len(commits) > 1:
        rows.append(bot_row(PULL_CHECK, "pr.single-commit", "error",
                            f"The pull request has {len(commits)} commits; an add-on update is "
                            f"one commit"))
    if any(len(c.get("parents") or []) > 1 for c in commits):
        rows.append(bot_row(PULL_CHECK, "pr.merge-commit", "error",
                            "A commit in the pull request is a merge commit"))
    return rows


def title_rows(meta, commits, addon):
    """Check the last commit's subject and the pull request's title name the add-on release."""
    wanted = f"[{addon['id']}] {addon['version']}"
    subject = (commits[-1]["commit"]["message"] or "").split("\n", 1)[0] if commits else ""
    title, branch = meta.get("title") or "", meta["base"]["ref"]
    rows = []
    if not names_release(subject, addon, branch):
        rows.append(bot_row(PULL_CHECK, "pr.commit-subject", "warning",
                            f"The commit subject is '{subject.strip()}', not '{wanted}'"))
    if not names_release(title, addon, branch):
        rows.append(bot_row(PULL_CHECK, "pr.title", "hint",
                            f"The pull request title is '{title.strip()}', not '{wanted}'"))
    return rows


def metadata_links(path):
    """Return (tag, link) for each forum, source and website an addon.xml gives."""
    try:
        data = path.read_bytes()[:ADDON_XML_LIMIT]
    except OSError:
        return []
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        return []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    return [(tag, e.text.strip()) for ext in root.iter("extension")
            if ext.get("point") in METADATA_POINTS
            for tag in LINK_TAGS for e in ext.findall(tag) if e.text and e.text.strip()]


class PublicLinks(urllib.request.HTTPRedirectHandler):
    """Redirect handler for metadata links: every hop must be http or https at a public address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Redirect to the new location only once it passes the public check."""
        check_public(newurl, WEB_SCHEMES)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def quiet(url):
    """Whether a link is on a host that refuses automated requests."""
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith(f".{h}") for h in QUIET_HOSTS)


def open_link(url):
    """Open a link with HEAD, then GET as a browser where HEAD is refused; return the final URL."""
    check_public(url, WEB_SCHEMES)
    opener = urllib.request.build_opener(PublicLinks())
    head = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with opener.open(head, timeout=LINK_TIMEOUT) as resp:
            return resp.geturl()
    except urllib.error.HTTPError as err:
        if err.code not in RETRY_STATUSES:
            raise
    get = urllib.request.Request(url, headers={"User-Agent": BROWSER_AGENT})
    with opener.open(get, timeout=LINK_TIMEOUT) as resp:
        return resp.geturl()


def same_place(given, final):
    """Whether a redirect changed only the scheme, a www. prefix or a trailing slash."""
    def place(url):
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower().removeprefix("www.")
        return host, parts.path.rstrip("/"), parts.query
    return place(given) == place(final)


def failure(err):
    """Say in a few words why a link did not open."""
    if isinstance(err, urllib.error.HTTPError):
        return f"HTTP {err.code}"
    if isinstance(err, TimeoutError):
        return f"no answer in {LINK_TIMEOUT} s"
    reason = err.reason if isinstance(err, urllib.error.URLError) else err
    return str(reason)[:120]


def link_rows(tag, link):
    """Check one metadata link: a warning when it does not open, a hint when it lands elsewhere."""
    tried = [link] if "://" in link else [f"https://{link}", f"http://{link}"]
    why = ""
    for url in tried:
        if quiet(url):
            return []
        try:
            final = open_link(url)
        except (SkinError, urllib.error.URLError, TimeoutError, OSError, ValueError) as err:
            why = failure(err)
            continue
        if quiet(final) or same_place(url, final):
            return []
        return [bot_row(LINK_CHECK, "addon.url-redirect", "hint",
                        f"<{tag}> {link} redirects to {final}", "addon.xml")]
    return [bot_row(LINK_CHECK, "addon.url-broken", "warning",
                    f"<{tag}> {link} does not open: {why}", "addon.xml")]


def provider_rows(meta, addon, base_addon, folder, branch):
    """Check the provider name and the submitter against the copy on the branch."""
    if not base_addon:
        return []
    rows = []
    old, new = base_addon.get("provider", ""), addon.get("provider", "")
    if old != new:
        rows.append(bot_row(PULL_CHECK, "addon.provider-changed", "hint",
                            f"The provider name changed from '{old}' to '{new}'", "addon.xml"))
    path = urllib.parse.quote(folder)
    last = request("GET", f"/repos/{REPO}/commits?sha={urllib.parse.quote(branch)}&path={path}"
                          "&per_page=1")
    before = ((last[0].get("author") or {}).get("login") or "") if last else ""
    submitter = (meta.get("user") or {}).get("login") or ""
    if before and submitter and before.lower() != submitter.lower():
        rows.append(bot_row(PULL_CHECK, "addon.provider-changed", "hint",
                            f"{submitter} submits an add-on that {before} submitted last"))
    return rows


def fetch_indexes(folder):
    """Fetch each branch's add-on index a folder lacks, retrying twice, skipping any that fail."""
    folder.mkdir(parents=True, exist_ok=True)
    for branch in (*OLD_RELEASES, *RELEASES):
        target = folder / f"{branch}.xml.gz"
        for pause in (*INDEX_RETRY_PAUSES, None):
            if target.exists() and target.stat().st_size:
                break
            try:
                download(INDEX_URL.format(branch), target)
            except SkinError as err:
                print(f"No {branch} index: {err}")
                target.unlink(missing_ok=True)
                if pause:
                    time.sleep(pause)


def own_copies(folder, branch):
    """Return whether each newer branch holds its own add-on copy; failed checks left out."""
    names = list(RELEASES)
    copies = {}
    for newer in names[names.index(branch) + 1:]:
        path = f"/repos/{REPO}/contents/{urllib.parse.quote(folder)}?ref={newer}"
        try:
            request("GET", path)
        except urllib.error.HTTPError as err:
            if err.code != 404:
                continue
            copies[newer] = False
        else:
            copies[newer] = True
    return copies


def addon_tree(sha, folder, tree, index):
    """Unpack an add-on folder at a commit, with its git file list and the indexes beside it."""
    if not git_tree(REPO, sha, [folder], tree):
        return False
    work = tree.parent / f".git-{sha[:12]}"
    listing = subprocess.run(["git", "-C", str(work), "ls-tree", "-r", sha, "--", folder],
                             capture_output=True, text=True, check=True).stdout
    (tree / ".tree").write_text(listing)
    shutil.copytree(index, tree / ".index", copy_function=os.link)
    return True


def validate(tree, folder, release, out):
    """Run kdk addon validate on one folder of a tree; return its report."""
    return run_kdk(tree, out, "addon", "validate", "--isolated", "--release", release,
                   "--index", "/check/.index", "--tree", "/check/.tree", f"/check/{folder}")


def totals(run):
    """Return the error, warning, hint and muted counts of an add-on report."""
    return {key: run.get(key, 0) for key in ("errors", "warnings", "hints", "muted")}


def report_facts(run, folder):
    """Return the id, version and provider name an add-on report gives."""
    return {"id": run.get("addon_id") or folder, "version": run.get("addon_version", ""),
            "provider": run.get("provider_name", "")}


def unchanged(head_rows, rows):
    """Return the unmuted head findings no new row stands for, which the branch's copy had too."""
    new = Counter()
    for row in rows:
        if row["status"] == "new":
            places = zip(row["files"], row.get("lines") or [0] * len(row["files"]), strict=False) \
                if row.get("files") else [(row["file"], row["line"])]
            new.update((row["rule"], row["subject"], file, line) for file, line in places)
    there = []
    for row in head_rows:
        key = (row["rule"], row["subject"], row["file"], row["line"])
        if row["muted"]:
            continue
        if new[key]:
            new[key] -= 1
        else:
            there.append({**row, "status": "there", "count": 1})
    return there


def addon_result(head_tree, base_tree, folder, release):
    """Run the add-on check on the head and any base copy; return facts for the comment."""
    out = head_tree.parent.parent / f"out-{folder}"
    head_run = validate(head_tree, folder, release, out / "head")
    base_run = validate(base_tree, folder, release, out / "base") if base_tree else None
    sides = ["/check/base/kdk.json"] * bool(base_run) + ["/check/head/kdk.json"]
    changes = run_kdk(out, out, "compare", *sides)
    rows = [r for r in compared(changes, (head_run, base_run))
            if not (r["status"] == "fixed" and r["rule"] in RELEASED_RULES)]
    head_rows = findings(head_run)
    return {**report_facts(head_run, folder), "folder": folder, "release": release,
            "bundled": head_run.get("bundled") or [], "kdk": head_run.get("kdk_version", ""),
            "checker": head_run.get("addoncheck_version", ""),
            "notices": head_run.get("notices") or [], "new": base_run is None,
            "head": totals(head_run), "base": totals(base_run) if base_run else None,
            "base_facts": report_facts(base_run, folder) if base_run else None,
            "rows": rows, "existing": existing(head_rows, rows) if base_run else [],
            "there": unchanged(head_rows, rows) if base_run else [],
            "all": head_rows}


def check_scripts_pr(scratch):
    """Check the add-on a repo-scripts pull request changes, against the copy on its branch."""
    meta, base, files, commits = pull_request()
    head, branch = meta["head"]["sha"], meta["base"]["ref"]
    if branch in OLD_RELEASES:
        raise OutOfScope(f"the pull request targets {branch}; the add-on check covers "
                         + ", ".join(RELEASES))
    folders, loose = changed_places(files)
    facts: dict[str, Any] = {"key": f"sha={head}", "head": head, "base": base, "branch": branch,
                             "pull": commit_rows(commits, folders, loose), "addons": []}
    if branch not in RELEASES:
        facts["pull"].insert(0, bot_row(PULL_CHECK, "pr.base-branch", "error",
                                        f"The pull request targets {branch}, not a Kodi release "
                                        f"branch ({', '.join(RELEASES)})"))
        return facts
    index = scratch / "index"
    fetch_indexes(index)
    for folder in [f for f in folders if not f.startswith(".")][:1]:
        (index / "copies.json").write_text(json.dumps(own_copies(folder, branch)))
        head_tree, base_tree = scratch / "head" / folder, scratch / "base" / folder
        has_base = addon_tree(base, folder, base_tree, index)
        if not addon_tree(head, folder, head_tree, index):
            if has_base:
                facts["pull"].append(bot_row(PULL_CHECK, "pr.removal", "warning",
                                             f"The pull request deletes {folder}; mark it broken "
                                             "in a version bump instead"))
            continue
        result = addon_result(head_tree, base_tree if has_base else None, folder, branch)
        extra = [r for tag, link in metadata_links(head_tree / folder / "addon.xml")
                 for r in link_rows(tag, link)]
        extra += provider_rows(meta, result, result["base_facts"], folder, branch)
        result["rows"] += extra
        result["all"] += extra
        facts["pull"] += title_rows(meta, commits, result)
        facts["addons"].append(result)
    checked = [a["folder"] for a in facts["addons"]]
    based = any(not a["new"] for a in facts["addons"])
    facts["checked"] = commits_checked(head, base, checked, based)
    return facts


def addon_rules(scratch):
    """Return the rule descriptions by id: kdk's add-on rules over the bot's own."""
    out = scratch / "rules"
    out.mkdir(parents=True, exist_ok=True)
    try:
        done = subprocess.run(sandbox_command(out, out, "-m", "kdk", "rules", "--json"),
                              capture_output=True, text=True, timeout=60)
        listed = json.loads(done.stdout)["addon_rules"]
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, TypeError):
        listed = []
    return {**BOT_RULES, **{r["id"]: r.get("description", "") for r in listed if r.get("id")}}


def cmd_check():
    """Check the run's pull request; write addon.json for the post job and the summary report."""
    scratch = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "addon"
    RUN_DIR.mkdir(exist_ok=True)
    facts: dict[str, Any]
    try:
        if os.environ["UPSTREAM"] not in os.environ.get("ALLOWED_REPOS", "").split():
            raise SkinError(f"{os.environ['UPSTREAM']} is not a repository the bot checks")
        check_sandbox(scratch / "probe")
        facts = check_scripts_pr(scratch)
        facts["rules"] = addon_rules(scratch)
    except (SkinError, subprocess.CalledProcessError, urllib.error.URLError, OSError,
            ET.ParseError, KeyError, ValueError) as err:
        print(f"Add-on check failed: {err!r}")
        reason = str(err) if isinstance(err, SkinError) else "the check failed"
        facts = {"key": f"run={os.environ.get('GITHUB_RUN_ID', '')}", "error": reason,
                 "quiet": isinstance(err, OutOfScope) and os.environ.get("SOURCE") == "push"}
    facts["run"] = run_link()
    report = report_text(facts)
    for result in facts.get("addons", []):
        del result["all"]
    (RUN_DIR / "addon.json").write_text(json.dumps(facts, indent=1) + "\n")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as f:
            f.write(report)


def pull_part(facts, rules):
    """Format the pull request's own findings when no add-on folder was checked."""
    rows = facts["pull"]
    kinds = problems(rows)
    parts = [f"**{ADDON_WORDS['title']}** · the pull request into {plain(facts['branch'])}",
             verdict({"new": True, "version": ""}, rows, [], [], 0, ADDON_WORDS["noun"])]
    parts += [part for severity in SEVERITY_WORDS
              if (part := severity_section(kinds, severity, rules, None, set()))]
    return "\n\n".join(parts)


def footer(facts):
    """Name the checker versions and the commits checked."""
    built = sorted({(a["kdk"], a["checker"]) for a in facts.get("addons", []) if a["kdk"]})
    names = ", ".join(f"{kdk_link(kdk)} with addoncheck {code(checker)}" for kdk, checker in built)
    return f"<sub>{names or 'no add-on checked'} · checked {facts.get('checked', '')}.</sub>"


def addon_text(facts):
    """Format the add-on check comment from addon.json."""
    title = ADDON_WORDS["title"]
    if "error" in facts:
        return f"**{title}** · not checked: {facts['error']}."
    rules = facts.get("rules") or BOT_RULES
    if not facts.get("addons"):
        if not facts.get("pull"):
            return f"**{title}** · not checked: the pull request changes no add-on folder."
        return f"{pull_part(facts, rules)}\n\n{footer(facts)}"
    room = COMMENT_LIMIT // len(facts["addons"])
    parts = [skin_part({**result, "branch": facts["branch"],
                        "rows": result["rows"] + (facts["pull"] if i == 0 else [])},
                       rules, room, facts.get("run", ""), ADDON_WORDS)
             for i, result in enumerate(facts["addons"])]
    return "\n\n".join([*parts, footer(facts)])


def report_text(facts):
    """Format the run summary report: every finding on the head side, new ones marked."""
    lines = [f"# {ADDON_WORDS['title']}", ""]
    if "error" in facts:
        return "\n".join([*lines, f"Not checked: {facts['error']}.", ""])
    if facts.get("pull"):
        lines += report_rows({"rows": facts["pull"], "all": facts["pull"]})
    for result in facts.get("addons", []):
        head = result["head"]
        counts = ", ".join(plural(head[f"{s}s"], s) for s in SEVERITY_WORDS)
        lines += [f"## {result['id']} {result['version']}".rstrip(), "",
                  f"{RELEASES.get(result['release'], result['release'])}, kdk {result['kdk']}, "
                  f"addoncheck {result['checker']}: {counts}, {head['muted']} muted.", ""]
        lines += report_rows(result)
    text = "\n".join(lines) + "\n"
    if len(text) > REPORT_LIMIT:
        text = text[:REPORT_LIMIT].rsplit("\n", 1)[0] + "\n\nThe rest does not fit the summary.\n"
    return text


def load_facts():
    """Load addon.json, or the facts of a check that left none."""
    try:
        return json.loads((RUN_DIR / "addon.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {"key": "missing", "error": "the check left no result"}


def cmd_render():
    """Render the add-on check comment without posting it."""
    print(unlinked(addon_text(load_facts())))


def cmd_post():
    """Post the check, hide earlier ones and skip an out-of-scope push; exit 1 on a credential."""
    facts = load_facts()
    if facts.get("quiet"):
        print(f"Not posting: {facts['error']}")
        return
    earlier = addon_comments()
    text = unlinked(addon_text(facts))
    withheld = bool(CREDENTIAL.search(text))
    request("POST", f"/repos/{REPO}/issues/{os.environ['FORK_PR']}/comments",
            {"body": f"<!-- kodi-review addon {facts['key']} {addon_build()} -->\n"
                     f"{WITHHELD if withheld else text}"})
    if earlier:
        hide_summaries(earlier)
    if withheld:
        sys.exit(1)


def left_out(path, bundled):
    """Whether the review diff leaves a file out: translations, binaries and bundled code."""
    return bool(LEFT_OUT.search(path)) or any(path.startswith(root) for root in bundled)


def review_diff(diff, folder, bundled):
    """Split a diff into the sections the model reads and the paths left out of it."""
    roots = [f"{folder}/{root}" for root in bundled]
    starts = [m.start() for m in DIFF_SECTION.finditer(diff)] + [len(diff)]
    kept, dropped = [], []
    for start, end in zip(starts, starts[1:], strict=False):
        section = diff[start:end]
        header = DIFF_SECTION.match(section)
        path = header[2] if header else ""
        binary = "\nBinary files " in section or "\nGIT binary patch" in section
        (dropped if binary or left_out(path, roots) else kept).append((path, section))
    return "".join(s for _, s in kept), sorted(p for p, _ in dropped)


def manifest(path):
    """Return an addon.xml's name, extension points with their files and imports, or none."""
    try:
        data = path.read_bytes()[:ADDON_XML_LIMIT]
        if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
            return None
        root = ET.fromstring(data)
    except (OSError, ET.ParseError):
        return None
    points = [(e.get("point", ""), e.get("library", "")) for e in root.iter("extension")
              if e.get("point") not in METADATA_POINTS]
    imports = [(i.get("addon", ""), i.get("version", ""), i.get("optional") == "true")
               for i in root.iter("import")]
    return {"name": root.get("name", ""), "points": points, "imports": imports}


def addon_facts_text(result, branch, head_tree, dropped, copies):
    """Format addon.md: the add-on's facts and what full.diff leaves out."""
    folder = result["folder"]
    info = manifest(head_tree / folder / "addon.xml") or {"name": "", "points": [], "imports": []}
    base = result.get("base_facts")
    target = f"{branch}, {RELEASES.get(branch, branch)}, Python {PYTHONS.get(branch, '?')}"
    lines = [f"# {info['name'] or folder} ({result['id']}) {result['version']}", "",
             f"- Target: {target}",
             f"- Provider: {result.get('provider') or 'not given'}",
             ("- Update of version " + (base.get("version") or "?") + f" on {branch}") if base
             else "- New to this branch"]
    lines += [f"- Newer branch {name}: " + ("has its own copy" if own else "has no copy, so its "
              "Kodi gets this one") for name, own in copies.items()]
    lines += ["", "Extension points:"]
    lines += [f"- {point}" + (f": {library}" if library else "")
              for point, library in info["points"]]
    lines += ["", "Imports:"]
    lines += [f"- {name} {version}" + (" (optional)" if optional else "")
              for name, version, optional in info["imports"]]
    if result.get("bundled"):
        lines += ["", "Bundled third-party code, left out of full.diff:"]
        lines += [f"- {folder}/{root}" for root in result["bundled"]]
    if dropped:
        lines += ["", f"Left out of full.diff ({len(dropped)}):"]
        lines += [f"- {path}" for path in dropped[:PATHS_LISTED]]
        if len(dropped) > PATHS_LISTED:
            lines.append(f"- and {len(dropped) - PATHS_LISTED} more")
    return "\n".join(lines) + "\n"


def checks_text(result):
    """Format checks.md: every finding the add-on check reports on the head, new or not."""
    rows = [r for r in result["rows"] if r["status"] == "new"] + result.get("there", [])
    lines = ["# What the add-on check reports", "",
             "Posted in its own comment. Never report these again, or anything of their kinds.", ""]
    for row in sorted(rows, key=lambda r: (list(SEVERITY_WORDS).index(r["severity"])
                                           if r["severity"] in SEVERITY_WORDS else 9,
                                           r["file"], r["line"])):
        where = "/".join(p for p in (result["folder"], row["file"]) if p)
        where += f":{row['line']}" if row["line"] else ""
        message = " ".join(row["message"].split())
        lines.append(f"- {row['severity']} {row['rule']} {where}: {message}")
    if len(lines) == 4:
        lines.append("Nothing: the add-on check found no problems.")
    return "\n".join(lines) + "\n"


def cmd_prep():
    """Write the AI review's add-on inputs: addon.md, full.diff, checks.md and knowledge.md."""
    folder, branch = os.environ["ADDON_FOLDER"], os.environ["BASE_REF"]
    head, base = os.environ["HEAD_SHA"], os.environ["BASE_SHA"]
    scratch = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "addon"
    RUN_DIR.mkdir(exist_ok=True)
    (RUN_DIR / "knowledge.md").write_text(FACTS_FILE.read_text())
    index = scratch / "index"
    fetch_indexes(index)
    copies = own_copies(folder, branch)
    (index / "copies.json").write_text(json.dumps(copies))
    head_tree, base_tree = scratch / "head" / folder, scratch / "base" / folder
    has_base = addon_tree(base, folder, base_tree, index)
    if not addon_tree(head, folder, head_tree, index):
        raise SystemExit(f"{folder} is not in the head commit")
    try:
        check_sandbox(scratch / "probe")
        result = addon_result(head_tree, base_tree if has_base else None, folder, branch)
        (RUN_DIR / "checks.md").write_text(checks_text(result))
    except (SkinError, subprocess.CalledProcessError, OSError, ValueError) as err:
        print(f"The add-on check did not run: {err!r}")
        result = {**report_facts({}, folder), "folder": folder, "bundled": [],
                  "base_facts": {"version": ""} if has_base else None}
        (RUN_DIR / "checks.md").write_text("# What the add-on check reports\n\nIt could not run "
                                           "here; its own comment on the pull request has it.\n")
    raw = RUN_DIR / "raw.diff"
    diff, dropped = review_diff(raw.read_text(errors="replace"), folder, result["bundled"])
    raw.unlink()
    (RUN_DIR / "full.diff").write_text(diff)
    (RUN_DIR / "addon.md").write_text(addon_facts_text(result, branch, head_tree, dropped, copies))


COMMANDS = {"check": cmd_check, "render": cmd_render, "post": cmd_post, "prep": cmd_prep}

if __name__ == "__main__":
    COMMANDS[sys.argv[1]]()
