"""Try a kdk build on pinned real skins through the bot's own skin check, diffed with past runs."""

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from unittest import mock

import skin

KDK = Path(__file__).resolve().parent.parent / "kdk"
WORK = Path.home() / ".cache" / "kodi-review-kdkcheck"
LXML = "lxml==6.1.3"
SHOWN = 12
CASES = {
    "copacetic-467": {"kind": "skins", "repo": "xbmc/repo-skins", "branch": "omega",
                      "head": "a3ea6ad94b7a9ac30053d20df52681cd664533cf",
                      "base": "df0e3021651b741b23369f0147ceebc031e01482",
                      "files": ["skin.copacetic/addon.xml"]},
    "metropolis-470": {"kind": "skins", "repo": "xbmc/repo-skins", "branch": "omega",
                       "head": "a30ee4bb55ce79db908bb4c07b939d702e156ba0",
                       "base": "d438cc4521e1c9595ef637f292ab1d9743f01079",
                       "files": ["skin.metropolis/addon.xml"]},
    "tempevo-488": {"kind": "skins", "repo": "xbmc/repo-skins", "branch": "piers",
                    "head": "bfe41686bf194c6f4b4701ac0b319292fa6597fe",
                    "base": "7cf1190a68e731faaf3930c186b5e4a5f4f599e1",
                    "files": ["skin.tempevo.limasky/addon.xml"]},
    "tajo-497": {"kind": "link", "repo": "xbmc/repo-skins",
                 "archive": "https://codeload.github.com/manfeed/skin.aeon.tajo/tar.gz/"
                            "d71315be3496f072b9d80312bad0bc0cf0e8cdee",
                 "key": "github:manfeed/skin.aeon.tajo@d71315be3496f072b9d80312bad0bc0cf0e8cdee",
                 "shown": "manfeed/skin.aeon.tajo@d71315be3496",
                 "base": "df0e3021651b741b23369f0147ceebc031e01482"},
    "estuary-29913": {"kind": "kodi", "repo": "xbmc/xbmc", "branch": "master", "release": "piers",
                      "head": "2428b3d7854768d7381976ef9973f7bb22823156",
                      "base": "c7c26084f84b258fb59bb51a84cd10a70256b26a",
                      "files": ["addons/skin.estuary/xml/Variables.xml"]},
}


def sh(*args):
    """Run a command and return its output, raising when it fails."""
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def build(source, committed, work):
    """Build a wheel from a copy of a kdk checkout; return it and the run's label."""
    head = sh("git", "-C", str(source), "rev-parse", "--short=8", "HEAD").strip()
    copy = work / "build"
    shutil.rmtree(copy, ignore_errors=True)
    (copy / "src").mkdir(parents=True)
    if committed:
        archive = subprocess.run(["git", "-C", str(source), "archive", "HEAD"], check=True,
                                 capture_output=True).stdout
        subprocess.run(["tar", "x", "-C", str(copy / "src")], input=archive, check=True)
        label = head
    else:
        listed = sh("git", "-C", str(source), "ls-files", "-co", "--exclude-standard", "-z")
        digest = hashlib.sha256()
        for name in sorted(n for n in listed.split("\0") if n and (source / n).is_file()):
            target = copy / "src" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / name, target)
            digest.update(name.encode() + b"\0" + target.read_bytes())
        dirty = sh("git", "-C", str(source), "status", "--porcelain").strip()
        label = head + (f"+{digest.hexdigest()[:7]}" if dirty else "")
    sh(str(work / "venv" / "bin" / "pip"), "wheel", "-q", "--no-deps", "-w", str(copy / "dist"),
       str(copy / "src"))
    return next((copy / "dist").glob("kdk-*.whl")), label


def make_venv(work):
    """Make the venv kdk runs from, with lxml, when it is missing; return its folder."""
    venv = work / "venv"
    if not venv.exists():
        sh(sys.executable, "-m", "venv", str(venv))
        sh(str(venv / "bin" / "pip"), "install", "-q", LXML)
    return venv


def clean(scratch):
    """Clear a case's trees and kdk output from its last run, keeping the fetched commits."""
    for side in ("head", "base"):
        for entry in (scratch / side).glob("*"):
            if entry.name.startswith(".git-"):
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    for name in ("probe", "rules", "download"):
        path = scratch / name
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)


def cached(link, cache):
    """Return a download stand-in that fetches a link once, then copies it from the cache."""
    real = skin.download

    def fetch(_link, target):
        if not cache.exists():
            cache.parent.mkdir(parents=True, exist_ok=True)
            real(link, cache)
        shutil.copy(cache, target)
    return fetch


def check(name, case, work):
    """Run one case through skin.py's own check path; return its skin.json facts."""
    scratch = work / "cases" / name
    clean(scratch)
    patches = [mock.patch.object(skin, "REPO", case["repo"])]
    if case["kind"] == "link":
        branch = {"commit": {"sha": case["base"]}}
        patches += [mock.patch.object(skin, "issue_link", return_value=(("", ""), False)),
                    mock.patch.object(skin, "resolve_link", return_value=(
                        case["archive"], case["key"], case["shown"])),
                    mock.patch.object(skin, "checked_before", return_value=False),
                    mock.patch.object(skin, "download",
                                      cached(case["archive"], work / "cache" / name)),
                    mock.patch.object(skin, "request", return_value=branch)]
        run = skin.check_issue
    else:
        pr = (case["head"], case["base"], case["branch"], case["files"])
        patches.append(mock.patch.object(skin, "pull_request", return_value=pr))
        if case["kind"] == "kodi":
            patches.append(mock.patch.object(skin, "kodi_release", return_value=case["release"]))
        run = skin.check_kodi_pr if case["kind"] == "kodi" else skin.check_skins_pr
    facts: dict[str, Any]
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        try:
            skin.check_sandbox(scratch / "probe")
            facts = run(scratch)
            skin.add_rules(facts, scratch)
        except skin.SkinError as err:
            facts = {"key": "error", "error": str(err)}
    facts["run"] = "local"
    facts["info_source"] = sorted({json.loads(p.read_text()).get("info_source")
                                   for p in (scratch / "head").glob("out-*/head/kdk.json")})
    return facts


def summary(facts):
    """Reduce a case's facts to counts per status and rule and the rows behind them."""
    if "error" in facts:
        return {"error": facts["error"]}
    skins = {}
    for result in facts["skins"]:
        counts = Counter()
        for row in result["rows"]:
            counts[f"{row['status']} {row['rule']}"] += row["count"]
        for _check, rule, _severity, n in result.get("existing", []):
            counts[f"there {rule}"] += n
        rows = sorted({json.dumps([r["status"], r["rule"], r["file"], r["line"], r["subject"],
                                   r["message"], r.get("files", [])]) for r in result["rows"]})
        text = skin.skin_text({**facts, "skins": [result]})
        skins[result["id"]] = {"verdict": text.split("\n\n")[1], "notices": result["notices"],
                               "counts": dict(counts), "rows": rows}
    return {"skins": skins, "info_source": facts["info_source"]}


def changes(before, after):
    """List what changed between two runs of a case, per rule and per finding."""
    if "error" in before or "error" in after:
        same = before.get("error") == after.get("error")
        return [] if same else [f"- error: {before.get('error')} -> {after.get('error')}"]
    lines = []
    for sid in sorted(set(before["skins"]) | set(after["skins"])):
        old, new = before["skins"].get(sid, {}), after["skins"].get(sid, {})
        was, now = old.get("counts", {}), new.get("counts", {})
        lines += [f"- {key}: {was.get(key, 0)} -> {now.get(key, 0)}"
                  for key in sorted(set(was) | set(now)) if was.get(key, 0) != now.get(key, 0)]
        for mark, rows in (("-", set(old.get("rows", [])) - set(new.get("rows", []))),
                           ("+", set(new.get("rows", [])) - set(old.get("rows", [])))):
            for row in sorted(rows)[:SHOWN]:
                status, rule, file, line, subject, message, files = json.loads(row)
                where = f"{file}:{line}" + (f" (+{len(files) - 1} files)" if files else "")
                lines.append(f"  {mark} {status} {rule} {where} [{subject}] {message}")
            if len(rows) > SHOWN:
                lines.append(f"  {mark} and {len(rows) - SHOWN} more")
    return lines


def main():
    """Build or take a kdk wheel, run the cases, and write their comments and a diffed report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kdk", type=Path, default=KDK, help="kdk checkout to build")
    parser.add_argument("--wheel", type=Path, help="try this wheel instead of building one")
    parser.add_argument("--committed", action="store_true", help="build HEAD, not the tree")
    parser.add_argument("--against", help="label of the run to diff with, the last by default")
    parser.add_argument("--work", type=Path, default=WORK, help="where runs and caches go")
    parser.add_argument("cases", nargs="*", help=f"cases, all by default: {', '.join(CASES)}")
    args = parser.parse_args()
    if unknown := set(args.cases) - set(CASES):
        parser.error(f"no such case: {', '.join(sorted(unknown))}")
    args.work.mkdir(parents=True, exist_ok=True)
    venv = make_venv(args.work)
    if args.wheel:
        wheel, label = args.wheel, args.wheel.name.split("-")[1]
    else:
        wheel, label = build(args.kdk.resolve(), args.committed, args.work)
    sh(str(venv / "bin" / "pip"), "install", "-q", "--force-reinstall", "--no-deps", str(wheel))
    os.environ["KDK_PYTHON"] = str(venv / "bin" / "python")
    runs = args.work / "runs"
    folder = runs / label
    folder.mkdir(parents=True, exist_ok=True)
    last = (runs / "LAST").read_text().strip() if (runs / "LAST").exists() else ""
    against = args.against or (last if last != label else "")
    report = [f"# kdk {label}" + (f" against {against}" if against else ""), ""]
    for name in args.cases or CASES:
        facts = check(name, CASES[name], args.work)
        if "error" not in facts:
            (folder / f"{name}.md").write_text(skin.skin_text(facts) + "\n")
        now = summary(facts)
        (folder / f"{name}.json").write_text(json.dumps(now, indent=1) + "\n")
        report.append(f"## {name}")
        report.append(f"not checked: {now['error']}" if "error" in now
                      else f"info_source: {', '.join(map(str, now['info_source']))}")
        for sid, part in now.get("skins", {}).items():
            report.append(f"{sid}: {part['verdict']}")
            report += [f"notice: {n}" for n in part["notices"]]
        past = runs / against / f"{name}.json"
        if against and past.exists():
            report += changes(json.loads(past.read_text()), now) or ["no change"]
        report.append("")
    (folder / "report.md").write_text("\n".join(report))
    (runs / "LAST").write_text(label + "\n")
    print("\n".join(report))
    print(f"Comments and report: {folder}")


if __name__ == "__main__":
    main()
