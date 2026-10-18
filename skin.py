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
KODI_PATHS = ("system", "addons/resource.language.en_gb", "addons/xbmc.gui",
              "xbmc/GUIInfoManager.cpp")
DEFAULT_SKIN = "addons/skin.estuary"
SKIN_LIMIT = 2
DOWNLOAD_LIMIT = 200 * 2**20
UNPACKED_LIMIT = 600 * 2**20
FILE_LIMIT = 30_000
ADDON_XML_LIMIT = 2**20
KDK_TIMEOUT = 900
COMMENT_LIMIT = 60_000
PLACES_SHOWN = 10
ROWS_SHOWN = 25
SECTION_ICONS = {"error": "🔴", "warning": "🟡", "fixed": "✅"}
SEVERITY_WORDS = {"error": "Error", "warning": "Warning"}
REPORT_LIMIT = 900_000
ASK_START = re.compile(r"@kodi-review", re.I)
LINK = re.compile(r"https://[^\s<>()\[\]`'\"]+")
REVISION = re.compile(r"\w[\w./-]{0,99}")
ADDON_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
GITHUB_PAGE = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?"
                         r"(?:/(?:tree|commit|releases/tag)/([^?#]+?))?/?(?:[?#].*)?")
LANGUAGE_CODE = re.compile(r"language/resource\.language\.([^/]+)/strings\.po")
NAME = re.compile(r"'([^'\n]{1,120})'|(</?[A-Za-z][^<>\n]{0,80}>)|(?<=: )([^\s'`<>]+)$")
MARKUP = re.compile(r"([\\`*_\[\]#|~!])")
ISSUE_REF = re.compile(r"#(?=\d)")
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
        raise SkinError(f"only https links can be checked, not {code(link[:200])}")
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parts.hostname, 443)}
    except OSError as err:
        raise SkinError(f"could not look up {code(parts.hostname)}") from err
    if not addresses or not all(ipaddress.ip_address(str(a).split("%")[0]).is_global
                                for a in addresses):
        raise SkinError(f"{code(parts.hostname)} is not a public address")


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
        raise SkinError(f"could not download {code(link[:200])}: {code(str(err)[:200])}") from err


def safe_name(name):
    """Return an archive entry's path parts, refusing absolute paths and escapes from the folder."""
    if not name or name.startswith(("/", "\\")) or "\\" in name or ":" in name.split("/")[0]:
        raise SkinError(f"the archive has an unsafe path: {code(name[:200])}")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise SkinError(f"the archive has an unsafe path: {code(name[:200])}")
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
                raise SkinError("the archive holds a link or special file: "
                                f"{code(info.filename[:200])}")
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
                raise SkinError("the archive holds a link or special file: "
                                f"{code(member.name[:200])}")
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
        raise SkinError(f"could not unpack the archive: {code(str(err)[:200])}") from err


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


def run_kdk(tree, out, *args):
    """Run a kdk command on a read-only tree inside the sandbox; return the JSON it writes."""
    out.mkdir(parents=True, exist_ok=True)
    command = sandbox_command(tree, out, "-m", "kdk", *args, "--json", "--exit-zero",
                              "-o", "/out/kdk.json")
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=KDK_TIMEOUT)
    except subprocess.TimeoutExpired as err:
        raise SkinError(f"kdk did not finish in {KDK_TIMEOUT // 60} minutes") from err
    if done.returncode == 0:
        return json.loads((out / "kdk.json").read_text())
    try:
        reason = json.loads(done.stdout).get("error")
    except (json.JSONDecodeError, AttributeError):
        reason = None
    print(done.stderr[-4000:], file=sys.stderr)
    shown = code(str(reason)[:200]) if reason else f"exit {done.returncode}"
    raise SkinError(f"kdk could not finish: {shown}")


def validate(tree, skin, release, kodi, out):
    """Run kdk validate on one skin folder of a tree; return its report."""
    args = ["validate", "--isolated", "--release", release]
    if kodi:
        args += ["--kodi-path", "/check"]
    return run_kdk(tree, out, *args, f"/check/{skin}")


def mute_reason(item):
    """Return the reason a kdk-ignore comment gives for muting a finding, or nothing if unmuted."""
    mute = item.get("muted")
    if not mute:
        return ""
    return (mute.get("reason") if isinstance(mute, dict) else "") or "no reason given"


def own_file_dropped(item):
    """Return a finding's message without the copy of its own file path that kdk may put in it."""
    message, file = item.get("message", ""), item.get("file", "")
    return message.replace(f"{file}: ", "", 1) if file else message


def finding(check, item):
    """Return one kdk finding as a row of check, rule, subject, severity, place, message, mute."""
    return {"check": check, "rule": item.get("rule", ""), "subject": item.get("subject", ""),
            "severity": item.get("severity", ""), "file": item.get("file", ""),
            "line": item.get("line") or 0, "message": own_file_dropped(item),
            "muted": bool(item.get("muted")), "reason": mute_reason(item)}


def findings(run):
    """Flatten a kdk report into finding rows."""
    return [finding(category, item)
            for category, items in (run.get("categories") or {}).items() for item in items]


def compared(changes, runs):
    """Turn kdk compare rows into finding rows with their status, count and any folded places."""
    checks = {item.get("rule"): category for run in runs if run
              for category, items in (run.get("categories") or {}).items() for item in items}
    rows = []
    for item in changes.get("rows") or []:
        row = finding(checks.get(item.get("rule"), ""), item)
        row.update(status=item.get("status", ""), count=item.get("count", 1))
        if item.get("files"):
            row.update(files=item["files"], lines=item.get("lines") or [0] * len(item["files"]))
        rows.append(row)
    return rows


def totals(run):
    """Return the error, warning and muted counts of a kdk report."""
    return {"errors": run.get("errors", 0), "warnings": run.get("warnings", 0),
            "muted": run.get("muted", 0)}


def existing(head_rows, rows):
    """Count the unmuted head findings per kind that are not new: [check, rule, severity, n]."""
    counts = Counter((r["check"], r["rule"], r["severity"]) for r in head_rows if not r["muted"])
    for row in rows:
        if row["status"] == "new":
            counts[(row["check"], row["rule"], row["severity"])] -= row["count"]
    return [[*kind, n] for kind, n in counts.items() if n > 0]


def skin_result(head, base, release, kodi, branch):
    """Run kdk on the head and any base (tree, skin folder) pair; return facts for the comment."""
    tree, skin = head
    info = addon_info(tree / skin / "addon.xml") or {}
    out = tree.parent / f"out-{tree.name}"
    head_run = validate(tree, skin, release, kodi, out / "head")
    base_run = validate(base[0], base[1], release, kodi, out / "base") if base else None
    sides = ["/check/base/kdk.json"] * bool(base_run) + ["/check/head/kdk.json"]
    changes = run_kdk(out, out, "compare", *sides)
    rows, head_rows = compared(changes, (head_run, base_run)), findings(head_run)
    return {"id": info.get("id") or Path(skin).name, "version": info.get("version", ""),
            "release": release, "branch": branch, "kdk": head_run.get("kdk_version", ""),
            "notices": head_run.get("notices") or [], "new_skin": base_run is None,
            "head": totals(head_run), "base": totals(base_run) if base_run else None,
            "rows": rows, "existing": existing(head_rows, rows) if base_run else [],
            "all": head_rows}


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


def tree_link(name, repo, sha, folder=""):
    """Link a name to a commit's files in a GitHub repo, at one folder when given."""
    path = f"/{urllib.parse.quote(folder)}" if folder else ""
    return f"[{name}](https://github.com/{repo}/tree/{sha}{path})"


def commits_checked(head, base, folders, based):
    """Name the head and any base commit, linked to their files at the one skin folder checked."""
    folder = folders[0] if len(folders) == 1 else ""
    text = tree_link(head[:12], REPO, head, folder)
    return text + (f" against {tree_link(base[:12], REPO, base, folder)}" if based else "")


def check_kodi_pr(scratch):
    """Check the skins a Kodi pull request changes, or Estuary when it changes none."""
    head, base, branch, files = pull_request()
    release = kodi_release(base)
    skins = sorted({"/".join(f.split("/")[:2]) for f in files
                    if f.startswith("addons/skin.") and f.count("/") >= 2}) or [DEFAULT_SKIN]
    results, folders = [], []
    for skin in skins[:SKIN_LIMIT]:
        trees = {}
        for side, sha in (("head", head), ("base", base)):
            tree = scratch / side / Path(skin).name
            if (git_tree(REPO, sha, [skin, *KODI_PATHS], tree, KODI_PATHS)
                    and (tree / skin).is_dir()):
                trees[side] = (tree, skin)
        if "head" in trees:
            results.append(skin_result(trees["head"], trees.get("base"), release, True, branch))
            folders.append(skin)
    based = any(not r["new_skin"] for r in results)
    return {"key": f"sha={head}", "checked": commits_checked(head, base, folders, based),
            "skins": results}


def check_skins_pr(scratch):
    """Check the skin folders a repo-skins pull request changes, against its release branch."""
    head, base, branch, files = pull_request()
    if branch not in RELEASE_NAMES:
        raise SkinError(f"the pull request targets {branch}; the skin check covers "
                        + " and ".join(RELEASE_NAMES))
    skins = sorted({f.split("/")[0] for f in files if f.startswith("skin.") and "/" in f})
    if not skins:
        raise SkinError("the pull request changes no skin folder")
    results, folders = [], []
    for skin in skins[:SKIN_LIMIT]:
        head_tree, base_tree = scratch / "head" / skin, scratch / "base" / skin
        if not git_tree(REPO, head, [skin], head_tree):
            continue
        if not addon_info(head_tree / skin / "addon.xml"):
            raise SkinError(f"{code(skin + '/addon.xml')} does not declare a skin")
        base_side = (base_tree, skin) if git_tree(REPO, base, [skin], base_tree) else None
        results.append(skin_result((head_tree, skin), base_side, branch, False, branch))
        folders.append(skin)
    based = any(not r["new_skin"] for r in results)
    return {"key": f"sha={head}", "checked": commits_checked(head, base, folders, based),
            "skins": results}


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
        raise SkinError(f"could not find {code(ref) or 'the default branch'} in "
                        f"{code(f'{owner}/{name}')}") from err
    return (f"https://codeload.github.com/{owner}/{name}/tar.gz/{sha}",
            f"github:{owner}/{name}@{sha}", tree_link(f"{owner}/{name}@{sha[:12]}",
                                                      f"{owner}/{name}", sha))


def addon_id(info):
    """Return a skin's add-on id, raising unless Kodi accepts it and it is safe as a folder name."""
    if not ADDON_ID.fullmatch(info["id"]):
        raise SkinError(f"{code(info['id'][:100]) or 'an empty id'} is not a valid add-on id")
    return info["id"]


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
    skin = addon_id(info)
    release = release_for(info["gui"])
    if not release:
        raise SkinError(f"{code(skin)} asks for xbmc.gui {code(info['gui'][:40]) or 'no version'}; "
                        "the skin check covers " + " and ".join(RELEASE_NAMES))
    named = scratch / "head" / "named"
    named.mkdir()
    folder.rename(named / skin)
    branch = request("GET", f"/repos/{REPO}/branches/{release}")["commit"]["sha"]
    base_tree = scratch / "base" / skin
    base = (base_tree, skin) if git_tree(REPO, branch, [skin], base_tree) else None
    result = skin_result((named, skin), base, release, False, release)
    shipped = tree_link(f"{release} {branch[:12]}", REPO, branch, skin)
    return {"key": f"source={key}", "checked": shown + (f" against {shipped}" if base else ""),
            "skins": [result]}


def rule_descriptions(scratch):
    """Return kdk's one-line description of each rule id, or nothing when kdk cannot list them."""
    out = scratch / "rules"
    out.mkdir(parents=True, exist_ok=True)
    try:
        done = subprocess.run(sandbox_command(out, out, "-m", "kdk", "rules", "--json"),
                              capture_output=True, text=True, timeout=60)
        return {r["id"]: r["description"] for r in json.loads(done.stdout)["rules"]}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, TypeError):
        return {}


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
        facts["rules"] = rule_descriptions(scratch)
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


def cell(text):
    """Format text from a skin as a code span that also fits a table cell."""
    return code(text).replace("|", "\\|")


def plain(text):
    """Escape text from a skin so it shows as typed, with no markup, links, mentions or refs."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = MARKUP.sub(r"\\\1", ISSUE_REF.sub("#\u200b", text))
    return text.replace("@", "@\u200b").replace("://", ":\u200b//").replace("www.", "www\u200b.")


def nice(message):
    """Format a kdk message: quoted names, tags and a final value as code, the rest escaped."""
    text = " ".join(str(message).split())
    parts, pos = [], 0
    for match in NAME.finditer(text):
        parts += [plain(text[pos:match.start()]), cell(match[1] or match[2] or match[3])]
        pos = match.end()
    return "".join(parts) + plain(text[pos:])


def plural(count, word):
    """Format a count with its noun, adding an s past one."""
    return f"{count} {word}{'s' if count != 1 else ''}"


def count(rows):
    """Count the findings some rows stand for."""
    return sum(r["count"] for r in rows)


def tidy(check, message):
    """Drop a message's leading check name, which the comment already shows."""
    if message.lower().startswith(f"{check.lower()}: "):
        return message[len(check) + 2:]
    return message


def problems(rows):
    """Split rows into kinds of problem by check, rule and severity; errors first, then most."""
    kinds = defaultdict(list)
    for row in rows:
        kinds[(row["check"], row["rule"], row["severity"])].append(row)
    return sorted(kinds.items(), key=lambda k: (k[0][2] != "error", -count(k[1]), k[0]))


def problem_name(kind, rows, rules):
    """Name a kind of problem by kdk's description of its rule, or by its first message."""
    return rules.get(kind[1]) or tidy(kind[0], rows[0]["message"])


def places(rows):
    """Return the lines per file some rows were found at, most first, and (language, line) pairs."""
    files, languages = {}, []
    for row in rows:
        if row.get("files"):
            languages += [(LANGUAGE_CODE.sub(r"\1", f), line)
                          for f, line in zip(row["files"], row["lines"], strict=False)]
        else:
            files.setdefault(row["file"], []).append(row["line"])
    listed = sorted(((f, sorted(lines)) for f, lines in files.items()),
                    key=lambda e: (-len(e[1]), e[0]))
    return listed, sorted(languages)


def spread(rows):
    """Count the files some rows span, each language file counted once."""
    listed, languages = places(rows)
    return len(listed) + len({language for language, _ in languages})


def where(rows):
    """Format the files and lines some rows were found at, up to the cap, then how many more."""
    listed, languages = places(rows)
    spots = [(cell(f), str(line) if line else "") for f, lines in listed for line in lines]
    spots += [("`strings.po`", f"{plain(language)} {line}" if line else plain(language))
              for language, line in languages]
    groups = {}
    for group, item in spots[:PLACES_SHOWN]:
        groups.setdefault(group, []).append(item)
    text = "; ".join(" ".join([group, ", ".join(i for i in items if i)]).strip()
                     for group, items in groups.items())
    rest = len(spots) - PLACES_SHOWN
    return text + (f" and {rest} more" if rest > 0 else "")


def example(kind, rows):
    """Format a kind's rule id, one place it was found at and that place's message."""
    first = rows[0]
    where_found = code(first["file"]) + (f" line {first['line']}" if first["line"] else "")
    message = tidy(kind[0], first["message"])
    return f"{code(kind[1] or kind[0])} · for example {where_found}: {code(message)}"


def finding_table(kind, rows, rows_shown=None):
    """Format a kind's findings as a Finding, Count, Where table, a row a message up to the cap."""
    groups = {}
    for row in rows:
        groups.setdefault(tidy(kind[0], row["message"]), []).append(row)
    ordered = sorted(groups.items(), key=lambda g: (-count(g[1]), g[0]))
    shown = ordered if rows_shown is None else ordered[:rows_shown]
    lines = ["| Finding | Count | Where |", "| --- | --: | --- |"]
    lines += [f"| {cell(message)} | {count(group)} | {where(group)} |" for message, group in shown]
    if len(shown) < len(ordered):
        rest = [row for _, group in ordered[len(shown):] for row in group]
        lines.append(f"| and {plural(len(ordered) - len(shown), 'more finding')} | "
                     f"{count(rest)} | |")
    return "\n".join(lines)


def fold(summary, body, opened=False, indent=False):
    """Format a fold under a summary line, its body indented when asked."""
    attr = " open" if opened else ""
    if indent:
        body = f"<dl><dd>\n\n{body}\n\n</dd></dl>"
    return f"<details{attr}><summary>{summary}</summary>\n\n{body}\n\n</details>"


def kind_fold(kind, rows, rules, rows_shown, table):
    """Format one kind of problem as a fold: example and finding table, or the example alone."""
    files = spread(rows)
    summary = (f"<b>{plain(kind[0])}</b> · {plain(problem_name(kind, rows, rules))} "
               f"<code>{count(rows)}</code> in {plural(files, 'file')}")
    body = example(kind, rows)
    if table:
        body += "\n\n" + finding_table(kind, rows, rows_shown)
    return fold(summary, body)


def severity_section(kinds, severity, rules, new_skin, rows_shown, bare):
    """Format one severity's kinds as an indented fold under its icon and counts."""
    members = [(k, rows) for k, rows in kinds if k[2] == severity]
    if not members:
        return ""
    noun = ("" if new_skin else "new ") + severity
    title = (f"{SECTION_ICONS[severity]} <b>{plural(sum(count(r) for _, r in members), noun)}, "
             f"{plural(len(members), 'kind')}</b>")
    inner = "\n".join(kind_fold(k, rows, rules, rows_shown, k not in bare) for k, rows in members)
    return fold(title, inner, opened=severity == "error", indent=True)


def digest(kinds, rules):
    """List kinds of problem one per line with their counts and any mute reasons."""
    lines = []
    for kind, n, reasons in kinds:
        reason = f", muted as {', '.join(code(r) for r in reasons)}" if reasons else ""
        lines.append(f"- {SEVERITY_WORDS.get(kind[2], kind[2])} · {plain(kind[0])} · "
                     f"{plain(rules.get(kind[1]) or kind[1])} <code>{n}</code>{reason}")
    return "\n".join(lines)


def digest_of(rows):
    """Turn rows into digest entries: kind, count and the mute reasons given."""
    return [(kind, count(items), sorted({r["reason"] for r in items if r["reason"]}))
            for kind, items in problems(rows)]


def verdict(skin, new, muted, fixed, there):
    """Say what is new, then what was fixed, muted and already there."""
    errors = sum(r["count"] for r in new if r["severity"] == "error")
    warnings = count(new) - errors
    prefix = "" if skin["new_skin"] else "new "
    found = " and ".join(plural(n, prefix + kind) for kind, n in (("error", errors),
                                                                   ("warning", warnings)) if n)
    if not found:
        found = "no problems" if skin["new_skin"] else "no new problems"
    extras = [f"{count(fixed)} fixed" if fixed else "",
              f"{count(muted)} muted by the skin" if muted else "",
              f"{there} already there before {skin['version'] or 'this version'}" if there else ""]
    extras = [e for e in extras if e]
    return found[0].upper() + found[1:] + "." + (f" {', '.join(extras).capitalize()}."
                                                 if extras else "")


def skin_body(skin, rules, rows_shown, bare):
    """Format one skin's part of the comment with some kinds' tables left out; return its kinds."""
    new = [r for r in skin["rows"] if r["status"] == "new"]
    muted = [r for r in skin["rows"] if r["status"] == "muted"]
    fixed = [r for r in skin["rows"] if r["status"] == "fixed" and not r["muted"]]
    there = [((c, r, s), n, []) for c, r, s, n in skin.get("existing", [])]
    there.sort(key=lambda k: (k[0][2] != "error", -k[1]))
    against = RELEASE_NAMES.get(skin["release"], skin["release"])
    compared = (", a new skin" if skin["new_skin"]
                else f" against the version on {plain(skin.get('branch') or 'the base')}")
    kinds = problems(new)
    parts = [f"**Skin check** · {code(skin['id'])} {plain(skin['version'])}{compared} · {against}",
             verdict(skin, new, muted, fixed, sum(n for _, n, _ in there))]
    parts += [f"> {nice(notice)}" for notice in skin["notices"]]
    for severity in ("error", "warning"):
        if part := severity_section(kinds, severity, rules, skin["new_skin"], rows_shown, bare):
            parts.append(part)
    if fixed:
        parts.append(fold(f"{SECTION_ICONS['fixed']} <b>{count(fixed)} fixed</b>",
                          digest(digest_of(fixed), rules)))
    if muted:
        parts.append(fold(f"<b>{count(muted)} muted by the skin</b>",
                          digest(digest_of(muted), rules)))
    if there:
        total = sum(n for _, n, _ in there)
        parts.append(fold(f"<b>{total} already there before {plain(skin['version'])}</b>",
                          digest(there, rules)))
    return "\n\n".join(parts), kinds


def skin_part(skin, rules, room, run):
    """Format one skin within its room, cutting rows, then tables, warnings and smallest first."""
    text, kinds = skin_body(skin, rules, None, set())
    if len(text) <= room:
        return text
    text, _ = skin_body(skin, rules, ROWS_SHOWN, set())
    bare = set()
    for kind, _ in sorted(kinds, key=lambda k: (k[0][2] == "error", count(k[1]))):
        if len(text) <= room - 200:
            break
        bare.add(kind)
        text, _ = skin_body(skin, rules, ROWS_SHOWN, bare)
    note = (f"Some places are left out to fit a comment; the [run's summary]({run}) lists every "
            "finding.")
    head, _, rest = text.partition("\n\n")
    return f"{head}\n\n{note}\n\n{rest}"[:room]


def skin_text(facts):
    """Format the skin check comment from skin.json."""
    if "error" in facts:
        return f"**Skin check** · not checked: {facts['error']}."
    if not facts.get("skins"):
        return "**Skin check** · not checked: no skin was found to check."
    room = COMMENT_LIMIT // len(facts["skins"])
    rules = facts.get("rules") or {}
    parts = [skin_part(skin, rules, room, facts.get("run", "")) for skin in facts["skins"]]
    versions = sorted({s["kdk"] for s in facts["skins"] if s["kdk"]})
    parts.append(f"<sub>kdk {', '.join(versions) or 'unknown'} · checked {facts['checked']}.</sub>")
    return "\n\n".join(parts)


def report_rows(skin):
    """Format one skin's findings as a table per check, new and muted ones marked."""
    new = {(r["rule"], r["subject"], r["file"], r["line"]) for r in skin["rows"]
           if r["status"] == "new" and not r.get("files")}
    new_files = {(r["rule"], r["subject"], f) for r in skin["rows"] if r["status"] == "new"
                 for f in r.get("files", ())}
    by_check = defaultdict(list)
    for row in skin["all"]:
        by_check[row["check"]].append(row)
    lines = []
    for check, rows in sorted(by_check.items()):
        lines += [f"### {check} ({len(rows)})", "", "| | Severity | Where | Finding |",
                  "|---|---|---|---|"]
        for r in sorted(rows, key=lambda r: (r["file"], r["line"])):
            mark = "new" if ((r["rule"], r["subject"], r["file"], r["line"]) in new
                             or (r["rule"], r["subject"], r["file"]) in new_files) else ""
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
