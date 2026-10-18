"""Skin check: fetch a skin from a pull request or a submission link, run kdk on it, post it."""

import hashlib
import ipaddress
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from review import (CREDENTIAL, REPO, RUN_DIR, SKIN_ASK, WITHHELD, hide_summaries, paged,
                    request, skin_comments, unlinked)

KODI_REPOS = ("xbmc/xbmc", "MikeSiLVO/xbmc")
GUI_RELEASES = {"5.17": "omega", "5.18": "piers"}
RELEASE_NAMES = {"omega": "Kodi 21 (Omega)", "piers": "Kodi 22 (Piers)"}
KODI_PATHS = ("system", "addons/resource.language.en_gb", "addons/xbmc.gui")
DEFAULT_SKIN = "addons/skin.estuary"
SKIN_LIMIT = 2
DOWNLOAD_LIMIT = 200 * 2**20
UNPACKED_LIMIT = 600 * 2**20
FILE_LIMIT = 30_000
ADDON_XML_LIMIT = 2**20
KDK_TIMEOUT = 900
LIST_LIMIT = 30
REPORT_LIMIT = 900_000
ASK_START = re.compile(r"@kodi-review", re.I)
LINK = re.compile(r"https://[^\s<>()\[\]`'\"]+")
REVISION = re.compile(r"\w[\w./-]{0,99}")
GITHUB_PAGE = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?"
                         r"(?:/(?:tree|commit|releases/tag)/([^?#]+?))?/?(?:[?#].*)?")
LANGUAGE_FILE = re.compile(r"language/resource\.language\.[^/]+/strings\.po")
LINE_NUMBER = re.compile(r"\bline \d+")
USER_AGENT = "kodi-review"


class SkinError(Exception):
    """A reason the skin check cannot go on, worded to follow "not checked," in the comment."""


def asked_source(text):
    """Return the first link in the text and the word after it, or None without a link."""
    match = LINK.search(text)
    if not match:
        return None
    after = text[match.end():].lstrip(")>]`'\"").split(None, 1)
    word = after[0].strip("`'\"") if after else ""
    return match[0].rstrip(".,;:!"), word if REVISION.fullmatch(word) else ""


def github_page(link, revision=""):
    """Return owner, repo and ref for a GitHub repo page link, or None for any other link."""
    match = GITHUB_PAGE.fullmatch(link)
    if not match or match[2] in ("releases", "archive"):
        return None
    return match[1], match[2], match[3] or revision


def check_public(link):
    """Check a link is https on the default port and resolves only to public addresses."""
    parts = urllib.parse.urlsplit(link)
    if parts.scheme != "https" or not parts.hostname or parts.port not in (None, 443):
        raise SkinError(f"only https links can be checked, not {link[:200]}")
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parts.hostname, 443)}
    except OSError as err:
        raise SkinError(f"could not look up {parts.hostname}") from err
    if not addresses or not all(ipaddress.ip_address(str(a).split("%")[0]).is_global
                                for a in addresses):
        raise SkinError(f"{parts.hostname} is not a public address")


class PublicRedirects(urllib.request.HTTPRedirectHandler):
    """Redirect handler that checks every hop with the same rules as the first link."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Redirect to the new location only once it passes the public check."""
        check_public(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(link, target):
    """Download a link into a file, stopping past the size cap."""
    check_public(link)
    opener = urllib.request.build_opener(PublicRedirects())
    size = 0
    try:
        req = urllib.request.Request(link, headers={"User-Agent": USER_AGENT})
        with opener.open(req, timeout=60) as resp, open(target, "wb") as out:
            while chunk := resp.read(2**20):
                size += len(chunk)
                if size > DOWNLOAD_LIMIT:
                    raise SkinError(f"the download is over {DOWNLOAD_LIMIT // 2**20} MB")
                out.write(chunk)
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise SkinError(f"could not download {link[:200]}: {err}") from err


def safe_name(name):
    """Return an archive entry's path parts, refusing absolute paths and escapes from the folder."""
    if not name or name.startswith(("/", "\\")) or "\\" in name or ":" in name.split("/")[0]:
        raise SkinError(f"the archive has an unsafe path: {name[:200]}")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise SkinError(f"the archive has an unsafe path: {name[:200]}")
    return parts


def count_entry(totals, size):
    """Count one entry into the running totals, refusing an archive past the file or size cap."""
    totals[0] += 1
    totals[1] += size
    if totals[0] > FILE_LIMIT:
        raise SkinError(f"the archive has more than {FILE_LIMIT} files")
    if totals[1] > UNPACKED_LIMIT:
        raise SkinError(f"the archive unpacks to more than {UNPACKED_LIMIT // 2**20} MB")


def unpack_zip(archive, target, _dropped=()):
    """Unpack a zip into a folder without .git, after checking every entry."""
    totals = [0, 0]
    with zipfile.ZipFile(archive) as zf:
        entries = [i for i in zf.infolist() if ".git" not in safe_name(i.filename)]
        for info in entries:
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode) or (mode and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                raise SkinError(f"the archive holds a link or special file: {info.filename[:200]}")
            count_entry(totals, info.file_size)
        for info in entries:
            path = target.joinpath(*safe_name(info.filename))
            if info.is_dir():
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(path, "wb") as out:
                while chunk := src.read(2**20):
                    out.write(chunk)


def unpack_tar(archive, target, dropped=()):
    """Unpack a tar without .git or symlinks in Kodi's folders, after checking every entry."""
    totals = [0, 0]
    with tarfile.open(archive) as tf:
        members = [m for m in tf.getmembers() if ".git" not in safe_name(m.name)
                   and not (m.issym() and m.name.startswith(tuple(f"{d}/" for d in dropped)))]
        for member in members:
            if not (member.isfile() or member.isdir()):
                raise SkinError(f"the archive holds a link or special file: {member.name[:200]}")
            count_entry(totals, member.size)
        tf.extractall(target, members=members, filter="data")


def unpack(archive, target, dropped=()):
    """Unpack an archive without .git, refusing escapes, bombs and links outside Kodi's folders."""
    target.mkdir(parents=True, exist_ok=True)
    with open(archive, "rb") as f:
        zipped = f.read(4) == b"PK\x03\x04"
    try:
        (unpack_zip if zipped else unpack_tar)(archive, target, dropped)
    except (zipfile.BadZipFile, tarfile.TarError, EOFError, OSError, ValueError) as err:
        raise SkinError(f"could not unpack the archive: {err}") from err


def addon_info(path):
    """Return an addon.xml's id, version and gui version; None if unsafe, unreadable or no skin."""
    try:
        data = path.read_bytes()[:ADDON_XML_LIMIT]
    except OSError:
        return None
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        return None
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    points = {e.get("point") for e in root.iter("extension")}
    if root.tag != "addon" or "xbmc.gui.skin" not in points:
        return None
    gui = next((i.get("version", "") for i in root.iter("import")
                if i.get("addon") == "xbmc.gui"), "")
    return {"id": root.get("id", ""), "version": root.get("version", ""), "gui": gui}


def find_skins(root, depth=3):
    """Return (folder, addon info) for each skin within a few levels of a folder."""
    found = []
    for folder, dirs, files in os.walk(root):
        if "addon.xml" in files and (info := addon_info(Path(folder, "addon.xml"))):
            found.append((Path(folder), info))
            dirs.clear()
        elif len(Path(folder).relative_to(root).parts) >= depth:
            dirs.clear()
    return found


def release_for(gui):
    """Return the kdk release for an xbmc.gui version, or None for one kdk does not cover."""
    return GUI_RELEASES.get(".".join(gui.split(".")[:2]))


def git_tree(repo, sha, paths, target, dropped=()):
    """Unpack the paths that exist at a commit of a GitHub repo; return the ones found."""
    work = target.parent / f".git-{sha[:12]}"
    if not work.exists():
        subprocess.run(["git", "init", "-q", str(work)], check=True)
        subprocess.run(["git", "-C", str(work), "fetch", "-q", "--no-tags", "--depth=1",
                        "--filter=blob:none", f"https://github.com/{repo}.git", sha], check=True)
    listing = subprocess.run(["git", "-C", str(work), "ls-tree", "--name-only", sha, "--", *paths],
                             capture_output=True, text=True, check=True).stdout.split()
    if not listing:
        return []
    archive = target.parent / f".tree-{sha[:12]}.tar"
    with open(archive, "wb") as out:
        subprocess.run(["git", "-C", str(work), "archive", "--format=tar", sha, "--", *listing],
                       stdout=out, check=True)
    unpack(archive, target, dropped)
    archive.unlink()
    return listing


def sandbox_command(tree, out, *args):
    """Return the bubblewrap command running kdk's Python on a read-only tree with no network."""
    python = os.environ["KDK_PYTHON"]
    venv = str(Path(python).parent.parent)
    return ["bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
            "--tmpfs", "/tmp", "--proc", "/proc", "--dev", "/dev",
            "--ro-bind", "/usr", "/usr", "--symlink", "usr/lib", "/lib",
            "--symlink", "usr/lib64", "/lib64", "--symlink", "usr/bin", "/bin",
            "--ro-bind", venv, venv, "--ro-bind", str(tree), "/check", "--bind", str(out), "/out",
            "--setenv", "HOME", "/tmp", "--setenv", "XDG_CACHE_HOME", "/tmp/cache",
            "--chdir", "/tmp", "--", python, "-I", *args]


def check_sandbox(scratch):
    """Check bubblewrap can start kdk's Python with kdk and lxml importable, raising if not."""
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        done = subprocess.run(sandbox_command(scratch, scratch, "-c", "import kdk, lxml"),
                              capture_output=True, text=True, timeout=60)
    except (OSError, KeyError, subprocess.TimeoutExpired) as err:
        raise SkinError("kdk or its sandbox is unavailable") from err
    if done.returncode != 0:
        print(done.stderr, file=sys.stderr)
        raise SkinError("kdk or its sandbox is unavailable")


def run_kdk(tree, skin, release, kodi, out):
    """Run kdk on one skin folder of a tree inside the sandbox; return its JSON report."""
    out.mkdir(parents=True, exist_ok=True)
    args = ["-m", "kdk", "validate", "--json", "--isolated", "--exit-zero",
            "--release", release, "-o", "/out/run.json"]
    if kodi:
        args += ["--kodi-path", "/check"]
    args.append(f"/check/{skin}")
    try:
        done = subprocess.run(sandbox_command(tree, out, *args), capture_output=True, text=True,
                              timeout=KDK_TIMEOUT)
    except subprocess.TimeoutExpired as err:
        raise SkinError(f"kdk did not finish in {KDK_TIMEOUT // 60} minutes") from err
    if done.returncode == 0:
        return json.loads((out / "run.json").read_text())
    try:
        reason = json.loads(done.stdout).get("error")
    except (json.JSONDecodeError, AttributeError):
        reason = None
    print(done.stderr[-4000:], file=sys.stderr)
    raise SkinError(f"kdk could not finish: {reason or f'exit {done.returncode}'}")


def findings(run):
    """Flatten a kdk report into rows of check, severity, file, line, message and mute."""
    return [{"rule": category, "severity": item.get("severity", ""),
             "file": item.get("file", ""), "line": item.get("line") or 0,
             "message": item.get("message", ""), "muted": bool(item.get("muted"))}
            for category, items in (run.get("categories") or {}).items() for item in items]


def fold_key(row):
    """Return a finding's match key across runs, one for a finding in every language file."""
    if LANGUAGE_FILE.fullmatch(row["file"]):
        return (row["rule"], row["severity"], "language/*/strings.po",
                LINE_NUMBER.sub("line #", row["message"]), row["muted"])
    return (row["rule"], row["severity"], row["file"], row["message"], row["muted"])


def compare(base, head):
    """Compare two kdk reports: rows new on head and fixed since base, all new without a base."""
    groups = {"head": defaultdict(list), "base": defaultdict(list)}
    for side, run in (("head", head), ("base", base)):
        for row in findings(run) if run else []:
            groups[side][fold_key(row)].append(row)
    rows = []
    for status, mine, theirs in (("new", "head", "base"), ("fixed", "base", "head")):
        for key, items in groups[mine].items():
            extra = len(items) - len(groups[theirs].get(key, ()))
            if extra <= 0:
                continue
            files = sorted({i["file"] for i in items})
            row: dict[str, Any] = {**items[-1], "status": status, "count": extra}
            if len(files) > 1:
                row.update(file=key[2], line=0, files=files, message=key[3])
            rows.append(row)
    severity = {"error": 0, "warning": 1}
    rows.sort(key=lambda r: (r["status"] != "new", r["muted"], severity.get(r["severity"], 2),
                             r["file"], r["line"], r["message"]))
    return rows


def totals(run):
    """Return the error, warning and muted counts of a kdk report."""
    return {"errors": run.get("errors", 0), "warnings": run.get("warnings", 0),
            "muted": run.get("muted", 0)}


def skin_result(head, base, release, kodi):
    """Run kdk on the head and any base (tree, skin folder) pair; return facts for the comment."""
    tree, skin = head
    info = addon_info(tree / skin / "addon.xml") or {}
    out = tree.parent / f"out-{tree.name}"
    head_run = run_kdk(tree, skin, release, kodi, out / "head")
    base_run = run_kdk(base[0], base[1], release, kodi, out / "base") if base else None
    return {"id": info.get("id") or Path(skin).name, "version": info.get("version", ""),
            "release": release, "kdk": head_run.get("kdk_version", ""),
            "notices": head_run.get("notices") or [], "new_skin": base_run is None,
            "head": totals(head_run), "base": totals(base_run) if base_run else None,
            "rows": compare(base_run, head_run), "all": findings(head_run)}


def pull_request():
    """Return the pull request's head and merge base commits, base branch and changed files."""
    pr = os.environ["FORK_PR"]
    meta = request("GET", f"/repos/{REPO}/pulls/{pr}")
    if meta["state"] != "open":
        raise SkinError("the pull request is closed")
    head = meta["head"]["sha"]
    base = request("GET", f"/repos/{REPO}/compare/{meta['base']['ref']}...{head}")
    files = [f["filename"] for f in paged(f"/repos/{REPO}/pulls/{pr}/files")]
    return head, base["merge_base_commit"]["sha"], meta["base"]["ref"], files


def kodi_release(sha):
    """Return the kdk release for Kodi's own xbmc.gui version at a commit of the Kodi repo."""
    raw = request("GET", f"/repos/{REPO}/contents/addons/xbmc.gui/addon.xml?ref={sha}", raw=True)
    version = ET.fromstring(raw).get("version", "")
    release = release_for(version)
    if not release:
        raise SkinError(f"kdk does not cover xbmc.gui {version} yet")
    return release


def check_kodi_pr(scratch):
    """Check the skins a Kodi pull request changes, or Estuary when it changes none."""
    head, base, _, files = pull_request()
    release = kodi_release(base)
    skins = sorted({"/".join(f.split("/")[:2]) for f in files
                    if f.startswith("addons/skin.") and f.count("/") >= 2}) or [DEFAULT_SKIN]
    results = []
    for skin in skins[:SKIN_LIMIT]:
        trees = {}
        for side, sha in (("head", head), ("base", base)):
            tree = scratch / side / Path(skin).name
            if (git_tree(REPO, sha, [skin, *KODI_PATHS], tree, KODI_PATHS)
                    and (tree / skin).is_dir()):
                trees[side] = (tree, skin)
        if "head" in trees:
            results.append(skin_result(trees["head"], trees.get("base"), release, True))
    return {"key": f"sha={head}", "checked": f"{head[:12]} against {base[:12]}", "skins": results}


def check_skins_pr(scratch):
    """Check the skin folders a repo-skins pull request changes, against its release branch."""
    head, base, branch, files = pull_request()
    if branch not in RELEASE_NAMES:
        raise SkinError(f"the pull request targets {branch}; the skin check covers "
                        + " and ".join(RELEASE_NAMES))
    skins = sorted({f.split("/")[0] for f in files if f.startswith("skin.") and "/" in f})
    if not skins:
        raise SkinError("the pull request changes no skin folder")
    results = []
    for skin in skins[:SKIN_LIMIT]:
        head_tree, base_tree = scratch / "head" / skin, scratch / "base" / skin
        if not git_tree(REPO, head, [skin], head_tree):
            continue
        if not addon_info(head_tree / skin / "addon.xml"):
            raise SkinError(f"{skin}/addon.xml does not declare a skin")
        base_side = (base_tree, skin) if git_tree(REPO, base, [skin], base_tree) else None
        results.append(skin_result((head_tree, skin), base_side, branch, False))
    return {"key": f"sha={head}", "checked": f"{head[:12]} against {base[:12]}", "skins": results}


def issue_link():
    """Return the link and revision the asking comment gives after the skin ask."""
    number = os.environ["COMMENT"].partition("/")[2]
    body = request("GET", f"/repos/{REPO}/issues/comments/{number}")["body"] or ""
    source = asked_source(SKIN_ASK.sub("", ASK_START.split(body, 1)[-1], count=1))
    if not source:
        raise SkinError("give the skin's link: `@kodi-review skin <link>`, a GitHub repo at a "
                        "commit, branch or tag, or a zip")
    return source


def resolve_link(link, revision):
    """Resolve a link to the archive to download, the check's key and how the comment names it."""
    page = github_page(link, revision)
    if not page:
        return link, "zip:" + hashlib.sha256(link.encode()).hexdigest()[:24], code(link[:200])
    owner, name, ref = page
    try:
        ref = ref or request("GET", f"/repos/{owner}/{name}")["default_branch"]
        sha = request("GET", f"/repos/{owner}/{name}/commits/{urllib.parse.quote(ref)}")["sha"]
    except urllib.error.HTTPError as err:
        raise SkinError(f"could not find {ref or 'the default branch'} in {owner}/{name}") from err
    return (f"https://codeload.github.com/{owner}/{name}/tar.gz/{sha}",
            f"github:{owner}/{name}@{sha}", f"{owner}/{name}@{sha[:12]}")


def checked_before(key):
    """Whether the bot already posted a check of this source on the issue."""
    return any(f"source={key} " in c["body"] for c in skin_comments())


def check_issue(scratch):
    """Check the skin a submission issue's comment links to, against what its branch ships."""
    archive, key, shown = resolve_link(*issue_link())
    if checked_before(key):
        return {"key": f"source={key}", "skip": "already checked"}
    download(archive, scratch / "download")
    tree = scratch / "head" / "link"
    unpack(scratch / "download", tree)
    skins = find_skins(tree)
    if len(skins) != 1:
        raise SkinError("the link holds no skin" if not skins
                        else "the link holds more than one skin")
    folder, info = skins[0]
    release = release_for(info["gui"])
    if not release:
        raise SkinError(f"{info['id']} asks for xbmc.gui {info['gui'] or 'no version'}; the skin "
                        "check covers " + " and ".join(RELEASE_NAMES))
    named = scratch / "head" / "named"
    named.mkdir()
    folder.rename(named / info["id"])
    branch = request("GET", f"/repos/{REPO}/branches/{release}")["commit"]["sha"]
    base_tree = scratch / "base" / info["id"]
    base = (base_tree, info["id"]) if git_tree(REPO, branch, [info["id"]], base_tree) else None
    result = skin_result((named, info["id"]), base, release, False)
    return {"key": f"source={key}", "checked": f"{shown} against {release} {branch[:12]}",
            "skins": [result]}


def run_link():
    """Return the link to this workflow run's page, where the full report is."""
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    return (f"{server}/{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/"
            f"{os.environ.get('GITHUB_RUN_ID', '')}")


def cmd_check():
    """Check the asked-for skin; write skin.json for the post job and the report to the summary."""
    scratch = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "skin"
    RUN_DIR.mkdir(exist_ok=True)
    facts: dict[str, Any]
    try:
        if os.environ["UPSTREAM"] not in os.environ.get("ALLOWED_REPOS", "").split():
            raise SkinError(f"{os.environ['UPSTREAM']} is not a repository the bot checks")
        check_sandbox(scratch / "probe")
        if os.environ["UPSTREAM"] in KODI_REPOS:
            facts = check_kodi_pr(scratch)
        elif os.environ.get("SOURCE") == "issue":
            facts = check_issue(scratch)
        else:
            facts = check_skins_pr(scratch)
    except (SkinError, subprocess.CalledProcessError, urllib.error.URLError, OSError,
            ET.ParseError, KeyError, ValueError) as err:
        print(f"Skin check failed: {err!r}")
        reason = str(err) if isinstance(err, SkinError) else "the check failed"
        facts = {"key": f"run={os.environ.get('GITHUB_RUN_ID', '')}", "error": reason}
    facts["run"] = run_link()
    report = report_text(facts)
    for skin in facts.get("skins", []):
        del skin["all"]
    (RUN_DIR / "skin.json").write_text(json.dumps(facts, indent=1) + "\n")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as f:
            f.write(report)


def code(text):
    """Wrap text from a skin in a code span, where it cannot link, mention or render HTML."""
    text = " ".join(str(text).replace("`", "'").split())
    return f"`{text}`" if text else ""


def place(row):
    """Format a finding's file and line, or its file count when folded across languages."""
    where = row["file"] + (f":{row['line']}" if row.get("line") else "")
    files = row.get("files")
    return code(where) + (f" in {len(files)} files" if files else "")


def plural(count, word):
    """Format a count with its noun, adding an s past one."""
    return f"{count} {word}{'s' if count != 1 else ''}"


def row_line(row):
    """Format one finding as a list item: severity, place, message, and any count past one."""
    times = f" ({row['count']}×)" if row["count"] > 1 and not row.get("files") else ""
    return f"- {row['severity'].capitalize()} · {place(row)} · {code(row['message'])}{times}"


def by_check(rows):
    """Format the unmuted finding counts per check, biggest first."""
    counts = Counter()
    for row in rows:
        if not row["muted"]:
            counts[row["rule"]] += row["count"]
    return ", ".join(f"{rule} {n}" for rule, n in counts.most_common())


def new_counts(rows):
    """Format how many new errors and warnings the rows hold, naming only the kinds present."""
    counts = Counter()
    for row in rows:
        counts[row["severity"]] += row["count"]
    kinds = [plural(n, f"new {kind}") for kind, n in sorted(counts.items()) if n]
    return ", ".join(kinds) or "no new problems"


def skin_lines(skin, run):
    """Format one skin's part of the comment: its line, new findings, and what was there."""
    name = code(f"{skin['id']} {skin['version']}")
    against = RELEASE_NAMES.get(skin["release"], skin["release"])
    head = skin["head"]
    new = [r for r in skin["rows"] if r["status"] == "new" and not r["muted"]]
    muted = sum(r["count"] for r in skin["rows"] if r["status"] == "new" and r["muted"])
    fixed = sum(r["count"] for r in skin["rows"] if r["status"] == "fixed" and not r["muted"])
    if skin["new_skin"]:
        parts = [f"**Skin:** {name} is new here. Against {against}: "
                 f"{plural(head['errors'], 'error')}, {plural(head['warnings'], 'warning')}."]
        if counts := by_check(new):
            parts.append(f"By check: {counts}.")
        listed = [r for r in new if r["severity"] == "error"]
    else:
        parts = [f"**Skin:** {name} against {against}: {new_counts(new)}"
                 + (f", {fixed} fixed." if fixed else ".")]
        listed = new
    if listed:
        shown = listed[:LIST_LIMIT]
        more = len(listed) - len(shown)
        parts.append("\n".join(row_line(r) for r in shown)
                     + (f"\n- {more} more in the [full report]({run})." if more else ""))
    if muted:
        parts.append(f"{plural(muted, 'new finding')} muted by `kdk-ignore` comments; the full "
                     "report lists them.")
    if skin["base"]:
        parts.append(f"Already there: {plural(skin['base']['errors'], 'error')}, "
                     f"{plural(skin['base']['warnings'], 'warning')}.")
    parts += [f"Note: {code(notice)}" for notice in skin["notices"]]
    return parts


def skin_text(facts):
    """Format the skin check comment from skin.json."""
    if "error" in facts:
        return f"**Skin:** not checked, {facts['error']}."
    if not facts.get("skins"):
        return "**Skin:** not checked, no skin was found to check."
    run = facts.get("run", "")
    parts = [part for skin in facts["skins"] for part in skin_lines(skin, run)]
    versions = sorted({s["kdk"] for s in facts["skins"] if s["kdk"]})
    parts.append(f"[Full report]({run})")
    parts.append(f"<sub>kdk {', '.join(versions) or 'unknown'} · checked {facts['checked']}.</sub>")
    return "\n\n".join(parts)


def cell(text):
    """Format text from a skin for a report table cell."""
    return code(text).replace("|", "\\|")


def report_rows(skin):
    """Format one skin's findings as a table per check, new and muted ones marked."""
    new = {(r["rule"], r["file"], r["line"], r["message"]) for r in skin["rows"]
           if r["status"] == "new" and not r.get("files")}
    new_files = {(r["rule"], f) for r in skin["rows"] if r["status"] == "new"
                 for f in r.get("files", ())}
    by_rule = defaultdict(list)
    for row in skin["all"]:
        by_rule[row["rule"]].append(row)
    lines = []
    for rule, rows in sorted(by_rule.items()):
        lines += [f"### {rule} ({len(rows)})", "", "| | Severity | Where | Finding |",
                  "|---|---|---|---|"]
        for r in sorted(rows, key=lambda r: (r["file"], r["line"])):
            mark = "new" if ((r["rule"], r["file"], r["line"], r["message"]) in new
                             or (r["rule"], r["file"]) in new_files) else ""
            where = r["file"] + (f":{r['line']}" if r["line"] else "")
            lines.append(f"| {'muted' if r['muted'] else mark} | {r['severity']} | {cell(where)} "
                         f"| {cell(r['message'])} |")
        lines.append("")
    return lines


def report_text(facts):
    """Format the run summary report: every finding on the head side, new ones marked."""
    lines = ["# Skin check", ""]
    if "error" in facts or "skip" in facts:
        lines.append(f"Not checked: {facts.get('error') or facts.get('skip')}.")
        return "\n".join(lines) + "\n"
    for skin in facts.get("skins", []):
        head = skin["head"]
        lines += [f"## {skin['id']} {skin['version']}".rstrip(), "",
                  f"{RELEASE_NAMES.get(skin['release'], skin['release'])}, kdk {skin['kdk']}: "
                  f"{plural(head['errors'], 'error')}, {plural(head['warnings'], 'warning')}, "
                  f"{head['muted']} muted.", ""]
        lines += report_rows(skin)
    text = "\n".join(lines) + "\n"
    if len(text) > REPORT_LIMIT:
        text = text[:REPORT_LIMIT].rsplit("\n", 1)[0] + "\n\nThe rest does not fit the summary.\n"
    return text


def load_facts():
    """Load skin.json, or the facts of a check that left none."""
    try:
        return json.loads((RUN_DIR / "skin.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {"key": "missing", "error": "the check left no result"}


def cmd_render():
    """Render the skin check comment without posting it."""
    print(unlinked(skin_text(load_facts())))


def cmd_post():
    """Post the skin check, hiding the bot's earlier ones; exit 1 on a credential."""
    facts = load_facts()
    if "skip" in facts:
        print(f"Not posting: {facts['skip']}")
        return
    earlier = skin_comments()
    text = unlinked(skin_text(facts))
    withheld = bool(CREDENTIAL.search(text))
    request("POST", f"/repos/{REPO}/issues/{os.environ['FORK_PR']}/comments",
            {"body": f"<!-- kodi-review skin {facts['key']} -->\n{WITHHELD if withheld else text}"})
    if earlier:
        hide_summaries(earlier)
    if withheld:
        sys.exit(1)


COMMANDS = {"check": cmd_check, "render": cmd_render, "post": cmd_post}

if __name__ == "__main__":
    COMMANDS[sys.argv[1]]()
