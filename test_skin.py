"""Offline tests for the skin check: links, safe unpacking, skin lookup, comparing and rendering."""

import io
import json
import os
import re
import socket
import stat
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import review
import skin

SKIN_ADDON = ('<?xml version="1.0"?><addon id="skin.test" version="1.2.0">'
              '<requires><import addon="xbmc.gui" version="5.18.0"/></requires>'
              '<extension point="xbmc.gui.skin"/></addon>')


PO = "language/resource.language.{}/strings.po"


def run(*rows, errors=0, warnings=0, muted=0):
    """Return a kdk report of findings given as (check, severity, file, line, message[, fields])."""
    categories = {}
    for check, severity, file, line, message, *fields in rows:
        item = {"rule": f"{check.split()[0].lower()}.test", "subject": "", "severity": severity,
                "file": file, "line": line, "message": message, **(fields[0] if fields else {})}
        categories.setdefault(check, []).append(item)
    return {"kdk_version": "3.0.1", "errors": errors, "warnings": warnings, "muted": muted,
            "notices": [], "categories": categories}


def changes(base, head):
    """Stand in for kdk compare, matching findings by rule, subject, file and message."""
    def items(report):
        return [i for rows in (report or {}).get("categories", {}).values() for i in rows]

    def same(a, b):
        return all(a.get(key) == b.get(key) for key in ("rule", "subject", "file", "message"))

    left, rows = items(base), []
    for item in items(head):
        match = next((b for b in left if same(b, item)), None)
        if match:
            left.remove(match)
        if item.get("muted") and not (match or {}).get("muted"):
            rows.append({**item, "status": "muted"})
        elif not match:
            rows.append({**item, "status": "new"})
    return {"rows": rows + [{**b, "status": "fixed"} for b in left]}


def folded(rule, severity, languages, line, message, status="new"):
    """Return a kdk compare row folded across the strings.po of some languages, a line apart."""
    files = [PO.format(language) for language in languages]
    return {"rule": rule, "severity": severity, "file": files[0], "line": line, "subject": "",
            "message": message.format(file=files[0]), "status": status, "count": len(files),
            "files": files, "lines": [line + n for n in range(len(files))]}


class LinkTests(unittest.TestCase):
    """Reading the link out of a skin ask and telling GitHub pages from downloads."""

    def test_asked_source(self):
        """The first link counts, with the word after it as a revision when it looks like one."""
        cases = {" https://github.com/o/skin.x": ("https://github.com/o/skin.x", ""),
                 " <https://github.com/o/skin.x> d71315b": ("https://github.com/o/skin.x",
                                                            "d71315b"),
                 " [here](https://x.org/s.zip). Thanks!": ("https://x.org/s.zip", ""),
                 " https://github.com/o/r/tree/v1, please": ("https://github.com/o/r/tree/v1",
                                                            "please"),
                 " https://github.com/o/skin.x again": ("https://github.com/o/skin.x", ""),
                 " please check it": None,
                 " http://x.org/s.zip": None}
        for text, expected in cases.items():
            self.assertEqual(skin.asked_source(text), expected, text)

    def test_github_page(self):
        """Repo pages give owner, repo and ref; downloads and other hosts are not pages."""
        cases = {"https://github.com/o/r": ("o", "r", ""),
                 "https://github.com/o/r.git": ("o", "r", ""),
                 "https://github.com/o/r/": ("o", "r", ""),
                 "https://github.com/o/r/tree/omega": ("o", "r", "omega"),
                 "https://github.com/o/r/commit/abc123": ("o", "r", "abc123"),
                 "https://github.com/o/r/releases/tag/v1.0.2": ("o", "r", "v1.0.2"),
                 "https://github.com/o/r/archive/refs/tags/v1.zip": None,
                 "https://github.com/o/r/releases/download/v1/s.zip": None,
                 "https://example.org/o/r": None}
        for link, expected in cases.items():
            self.assertEqual(skin.github_page(link), expected, link)
        self.assertEqual(skin.github_page("https://github.com/o/r", "d71315b"),
                         ("o", "r", "d71315b"))
        self.assertEqual(skin.github_page("https://github.com/o/r/tree/v2", "x"), ("o", "r", "v2"))

    def test_check_public(self):
        """Only https on the default port to public addresses passes."""
        def resolve(*addresses):
            return lambda *_: [(socket.AF_INET, 0, 0, "", (a, 443)) for a in addresses]
        with mock.patch.object(skin.socket, "getaddrinfo", resolve("140.82.112.3")):
            skin.check_public("https://github.com/o/r")
            for link in ("http://github.com/o/r", "https://github.com:8443/o/r", "file:///etc"):
                with self.assertRaises(skin.SkinError, msg=link):
                    skin.check_public(link)
        for address in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "::1"):
            with mock.patch.object(skin.socket, "getaddrinfo", resolve("140.82.112.3", address)):
                with self.assertRaises(skin.SkinError, msg=address):
                    skin.check_public("https://x.org/s.zip")


class UnpackTests(unittest.TestCase):
    """Unpacking strangers' archives without links, escapes or bombs."""

    def setUp(self):
        """Make a scratch folder."""
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        """Remove the scratch folder."""
        self.tmp.cleanup()

    def zipped(self, entries):
        """Write a zip of (name, data, mode) entries and return its path."""
        path = self.root / "a.zip"
        with zipfile.ZipFile(path, "w") as zf:
            for name, data, mode in entries:
                info = zipfile.ZipInfo(name)
                info.external_attr = mode << 16
                zf.writestr(info, data)
        return path

    def tarred(self, entries):
        """Write a gzipped tar of (name, data, type) entries and return its path."""
        path = self.root / "a.tar.gz"
        with tarfile.open(path, "w:gz") as tf:
            for name, data, kind in entries:
                info = tarfile.TarInfo(name)
                info.type = kind
                if kind == tarfile.SYMTYPE:
                    info.linkname = data
                    tf.addfile(info)
                else:
                    info.size = len(data)
                    tf.addfile(info, io.BytesIO(data))
        return path

    def test_unpacks_a_zip_and_a_tar(self):
        """Plain files and folders come out of either archive type."""
        file = stat.S_IFREG | 0o644
        skin.unpack(self.zipped([("skin.x/addon.xml", SKIN_ADDON, file),
                                 ("skin.x/xml/", "", stat.S_IFDIR | 0o755)]), self.root / "z")
        self.assertTrue((self.root / "z/skin.x/addon.xml").is_file())
        skin.unpack(self.tarred([("top/skin.x/addon.xml", SKIN_ADDON.encode(), tarfile.REGTYPE)]),
                    self.root / "t")
        self.assertTrue((self.root / "t/top/skin.x/addon.xml").is_file())

    def test_leaves_out_git_folders(self):
        """A download's .git folder is left out, keeping a stranger's git config off the runner."""
        file = stat.S_IFREG | 0o644
        skin.unpack(self.zipped([("skin.x/.git/config", "[core]\nfsmonitor = sh evil", file),
                                 ("skin.x/addon.xml", SKIN_ADDON, file)]), self.root / "z")
        skin.unpack(self.tarred([("top/.git/config", b"x", tarfile.REGTYPE),
                                 ("top/addon.xml", b"x", tarfile.REGTYPE)]), self.root / "t")
        unpacked = [p.relative_to(self.root).as_posix() for p in self.root.rglob("*")
                    if p.is_file() and p.parent != self.root]
        self.assertEqual(sorted(unpacked), ["t/top/addon.xml", "z/skin.x/addon.xml"])

    def test_drops_kodi_links(self):
        """Links in Kodi's own folders are left out; a link in the skin still stops the unpack."""
        archive = self.tarred([("system/settings/freebsd.xml", "linux.xml", tarfile.SYMTYPE),
                               ("system/colors.xml", b"x", tarfile.REGTYPE)])
        skin.unpack(archive, self.root / "k", ("system",))
        self.assertEqual([p.name for p in (self.root / "k").rglob("*") if p.is_file()],
                         ["colors.xml"])
        archive = self.tarred([("addons/skin.estuary/media", "/etc", tarfile.SYMTYPE)])
        with self.assertRaises(skin.SkinError):
            skin.unpack(archive, self.root / "s", ("system",))

    def test_refuses_escapes_and_links(self):
        """Absolute paths, parent steps, backslashes, drives and links stop the unpack."""
        file, link = stat.S_IFREG | 0o644, stat.S_IFLNK | 0o777
        archives = [self.zipped([("../evil.xml", "x", file)]),
                    self.zipped([("/etc/evil.xml", "x", file)]),
                    self.zipped([("skin\\..\\evil.xml", "x", file)]),
                    self.zipped([("C:/evil.xml", "x", file)]),
                    self.zipped([("skin.x/media", "/etc", link)]),
                    self.tarred([("skin.x/media", "/etc", tarfile.SYMTYPE)]),
                    self.tarred([("skin.x/../../evil", b"x", tarfile.REGTYPE)])]
        for archive in archives:
            with self.assertRaises(skin.SkinError):
                skin.unpack(archive, self.root / "out")
        self.assertFalse((self.root / "evil.xml").exists())

    def test_refusal_quotes_the_entry_name(self):
        """A refused entry's name sits in a code span, where markup in it stays text."""
        archive = self.zipped([("../![x](https://evil.org/p.png)", "x", stat.S_IFREG | 0o644)])
        with self.assertRaisesRegex(skin.SkinError,
                                    r"unsafe path: `\.\./!\[x\]\(https://evil\.org/p\.png\)`$"):
            skin.unpack(archive, self.root / "out")

    def test_refuses_too_many_or_too_big(self):
        """Archives past the file count or the unpacked size stop before writing anything."""
        archive = self.zipped([(f"f{i}", "x", stat.S_IFREG | 0o644) for i in range(5)])
        with mock.patch.object(skin, "FILE_LIMIT", 4), self.assertRaises(skin.SkinError):
            skin.unpack(archive, self.root / "many")
        with mock.patch.object(skin, "UNPACKED_LIMIT", 3), self.assertRaises(skin.SkinError):
            skin.unpack(archive, self.root / "big")
        self.assertFalse(any((self.root / "big").iterdir()))

    def test_refuses_what_is_no_archive(self):
        """A file that is neither zip nor tar gives a reason, not a traceback."""
        path = self.root / "page.html"
        path.write_text("<html></html>")
        with self.assertRaises(skin.SkinError):
            skin.unpack(path, self.root / "out")


class SkinLookupTests(unittest.TestCase):
    """Finding the skin in an unpacked download and reading its xbmc.gui version."""

    def test_find_skins(self):
        """A skin's addon.xml is found a level or two down; other add-ons and DTDs are not skins."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repo-abc/skin.test").mkdir(parents=True)
            (root / "repo-abc/skin.test/addon.xml").write_text(SKIN_ADDON)
            (root / "repo-abc/script.x").mkdir()
            (root / "repo-abc/script.x/addon.xml").write_text(
                '<addon id="script.x"><extension point="xbmc.python.script"/></addon>')
            (root / "repo-abc/evil").mkdir()
            (root / "repo-abc/evil/addon.xml").write_text(
                '<!DOCTYPE a [<!ENTITY e "x">]>' + SKIN_ADDON)
            found = skin.find_skins(root)
        self.assertEqual([(p.name, i) for p, i in found],
                         [("skin.test", {"id": "skin.test", "version": "1.2.0", "gui": "5.18.0"})])

    def test_release_for(self):
        """Version 5.17 of xbmc.gui is Omega, 5.18 is Piers, anything else is not covered."""
        self.assertEqual([skin.release_for(v) for v in ("5.17.0", "5.18.2", "5.16.0", "")],
                         ["omega", "piers", None, None])

    def test_addon_id(self):
        """An id Kodi accepts passes; one that could escape a folder or render is refused."""
        for good in ("skin.estuary", "skin.arctic.zephyr.martian", "skin.a_b-2"):
            self.assertEqual(skin.addon_id({"id": good}), good)
        for bad in ("../x", "/etc", "a/b", ".hidden", "", "x y", "<b>x</b>"):
            with self.assertRaises(skin.SkinError, msg=bad):
                skin.addon_id({"id": bad})
        with self.assertRaisesRegex(skin.SkinError, r"^`\.\./x` is not a valid add-on id$"):
            skin.addon_id({"id": "../x"})


class FooterTests(unittest.TestCase):
    """The footer's links to what was checked."""

    def test_one_skin_links_its_folder_at_both_commits(self):
        """A single skin's head and base link to its folder's files at each commit."""
        with mock.patch.object(skin, "REPO", "xbmc/repo-skins"):
            text = skin.commits_checked("a" * 40, "b" * 40, ["skin.test"], True)
        self.assertEqual(text, f"[{'a' * 12}](https://github.com/xbmc/repo-skins/tree/{'a' * 40}"
                               f"/skin.test) against [{'b' * 12}](https://github.com/xbmc/"
                               f"repo-skins/tree/{'b' * 40}/skin.test)")

    def test_new_skin_names_no_base(self):
        """A skin with nothing to compare against names only the head."""
        with mock.patch.object(skin, "REPO", "xbmc/repo-skins"):
            text = skin.commits_checked("a" * 40, "b" * 40, ["skin.test"], False)
        self.assertNotIn("against", text)

    def test_two_skins_link_the_whole_tree(self):
        """Two skins link each commit's files at the repo root."""
        with mock.patch.object(skin, "REPO", "xbmc/xbmc"):
            text = skin.commits_checked("a" * 40, "b" * 40, ["addons/skin.a", "addons/skin.b"],
                                        True)
        self.assertIn(f"(https://github.com/xbmc/xbmc/tree/{'a' * 40})", text)

    def test_kdk_version_links_its_build(self):
        """A build stamped with a commit links it, a release its tag, anything else nothing."""
        self.assertEqual(skin.kdk_link("3.1.0+513cf7e"),
                         "[kdk 3.1.0+513cf7e](https://github.com/MikeSiLVO/kdk/commit/513cf7e)")
        self.assertEqual(skin.kdk_link("3.1.0"),
                         "[kdk 3.1.0](https://github.com/MikeSiLVO/kdk/releases/tag/v3.1.0)")
        self.assertEqual(skin.kdk_link("3.1.0-dev](x)"), "kdk `3.1.0-dev](x)`")

    def test_link_footer_opens_the_skin_files(self):
        """A GitHub link resolves to its commit, named as owner/repo@sha linked to its files."""
        answers = {"/repos/o/skin.x": {"default_branch": "main"},
                   "/repos/o/skin.x/commits/main": {"sha": "c" * 40}}
        with mock.patch.object(skin, "request", lambda method, path: answers[path]):
            archive, key, shown = skin.resolve_link("https://github.com/o/skin.x", "")
        self.assertEqual(archive, f"https://codeload.github.com/o/skin.x/tar.gz/{'c' * 40}")
        self.assertEqual(key, f"github:o/skin.x@{'c' * 40}")
        self.assertEqual(shown, f"[o/skin.x@{'c' * 12}](https://github.com/o/skin.x/tree/"
                                f"{'c' * 40})")


class CompareTests(unittest.TestCase):
    """Reading kdk's compare rows into the rows the comment is made from."""

    def test_rows_take_their_check_from_either_run(self):
        """A new row is named by head's check and a fixed one by base's."""
        base = run(("Fonts", "warning", "xml/Font.xml", 10, "Unused font: a",
                    {"rule": "fonts.unused", "subject": "a"}))
        head = run(("Variables", "error", "xml/Colors.xml", 140, "Duplicate variable 'c'",
                    {"rule": "vars.duplicate", "subject": "c"}))
        rows = skin.compared(changes(base, head), (head, base))
        self.assertEqual([(r["status"], r["check"], r["rule"], r["count"]) for r in rows],
                         [("new", "Variables", "vars.duplicate", 1),
                          ("fixed", "Fonts", "fonts.unused", 1)])

    def test_folded_row_keeps_its_files_without_its_path(self):
        """A row kdk folded across languages keeps its count and files, its message no path."""
        head = run(("Kodi Repo", "warning", PO.format("am_et"), 200, "x",
                    {"rule": "repo.po-parse"}))
        report = {"rows": [folded("repo.po-parse", "warning", ("am_et", "ar_sa", "bg_bg"), 200,
                                  "Kodi repo: {file}: unescaped double quote found")]}
        [row] = skin.compared(report, (head, None))
        self.assertEqual((row["check"], row["count"], len(row["files"]), row["message"]),
                         ("Kodi Repo", 3, 3, "Kodi repo: unescaped double quote found"))

    def test_muted_row_keeps_its_reason(self):
        """A row the skin muted keeps the reason its kdk-ignore comment gives."""
        head = run(("Labels", "warning", "xml/a.xml", 3, "Not translated",
                    {"muted": {"line": 2, "reason": "theme name"}}))
        [row] = skin.compared(changes(run(), head), (head, None))
        self.assertEqual((row["status"], row["muted"], row["reason"]),
                         ("muted", True, "theme name"))


class RenderTests(unittest.TestCase):
    """The skin check comment and the run summary report."""

    RULES = {"vars.duplicate": "A variable defined twice.", "fonts.unused": "A font nothing uses.",
             "ids.use-name": "A window referred to by number where its name works."}

    def facts(self, base, head, report=None):
        """Return skin.json facts for one skin, compared from the two reports unless given."""
        report = report or changes(base, head)
        rows, head_rows = skin.compared(report, (head, base)), skin.findings(head)
        result = {"id": "skin.test", "version": "1.2.0", "release": "piers", "branch": "piers",
                  "kdk": "3.0.1", "notices": [], "new_skin": base is None,
                  "head": skin.totals(head), "base": skin.totals(base) if base else None,
                  "rows": rows, "existing": skin.existing(head_rows, rows) if base else [],
                  "all": head_rows}
        return {"key": "sha=" + "a" * 40, "checked": "aaaa against bbbb", "skins": [result],
                "run": "https://github.com/MikeSiLVO/kodi-review/actions/runs/1",
                "rules": self.RULES}

    def update(self):
        """Return the comment for an update: new error, two new warnings, one fixed, one there."""
        base = run(("Fonts", "warning", "xml/Font.xml", 3, "Unused font: a",
                    {"rule": "fonts.unused", "subject": "a"}),
                   ("Fonts", "warning", "xml/Font.xml", 4, "Unused font: e",
                    {"rule": "fonts.unused", "subject": "e"}))
        head = run(("Variables", "error", "xml/Colors.xml", 140, "Duplicate variable 'c'",
                    {"rule": "vars.duplicate", "subject": "c"}),
                   *[("Fonts", "warning", "xml/Font.xml", n, f"Unused font: {name}",
                      {"rule": "fonts.unused", "subject": name})
                     for n, name in ((4, "e"), (9, "b"), (10, "d"))])
        return skin.skin_text(self.facts(base, head))

    def test_update_opens_with_title_verdict_and_open_errors(self):
        """An update leads with its title and verdict, then the errors open and warnings closed."""
        text = self.update()
        self.assertTrue(text.startswith(
            "**Skin check** · `skin.test` 1.2.0 against the version on piers · Kodi 22 (Piers)"
            "\n\n1 new error and 2 new warnings. 1 fixed, 1 already there before 1.2.0.\n\n"
            "<details open><summary>🔴 <b>1 new error, 1 kind</b></summary>\n\n<dl><dd>"))
        self.assertIn("<details><summary>🟡 <b>2 new warnings, 1 kind</b></summary>", text)
        self.assertTrue(text.endswith("<sub>[kdk 3.0.1](https://github.com/MikeSiLVO/kdk/"
                                      "releases/tag/v3.0.1) · checked aaaa against bbbb.</sub>"))
        self.assertNotIn("actions/runs", text)

    def test_kind_fold_holds_an_example_and_its_findings(self):
        """A kind folds its rule's description and count over an example and a finding table."""
        text = self.update()
        self.assertIn("<details><summary><b>Variables</b> · A variable defined twice. "
                      "<code>1</code> in 1 file</summary>\n\n`vars.duplicate` · for example "
                      "`xml/Colors.xml` line 140: `Duplicate variable 'c'`\n\n"
                      "| Finding | Count | Where |\n| --- | --: | --- |\n"
                      "| `Duplicate variable 'c'` | 1 | `xml/Colors.xml` 140 |\n\n</details>", text)
        self.assertIn("<b>Fonts</b> · A font nothing uses. <code>2</code> in 1 file", text)
        self.assertIn("| `Unused font: b` | 1 | `xml/Font.xml` 9 |\n"
                      "| `Unused font: d` | 1 | `xml/Font.xml` 10 |", text)

    def test_fixed_and_already_there_fold_as_digests(self):
        """Fixed and already there are closed one-line-per-kind lists with their counts."""
        text = self.update()
        self.assertIn("<details><summary>✅ <b>1 fixed</b></summary>\n\n"
                      "- Warning · Fonts · A font nothing uses. <code>1</code>\n\n</details>",
                      text)
        self.assertIn("<details><summary><b>1 already there before 1.2.0</b></summary>", text)

    def test_submission_blockers_count_as_errors(self):
        """A warning that blocks a submission becomes an error, with a note."""
        head = run(("Kodi Repo", "warning", "addon.xml", 33, "Kodi repo: <news> is 8902 characters",
                    {"rule": "repo.schema"}),
                   ("Kodi Repo", "warning", "a.pdf", 0, "Kodi repo: a.pdf has a file ending",
                    {"rule": "repo.file-type"}), warnings=2)
        facts = self.facts(None, head)
        skin.raise_failing(facts["skins"][0], {"repo.schema"})
        text = skin.skin_text(facts)
        self.assertIn("1 error and 1 warning.\n\nKodi repo findings that block a submission "
                      "count as errors.", text)
        self.assertIn("🔴 <b>1 error, 1 kind</b>", text)
        self.assertEqual(facts["skins"][0]["head"], {"errors": 1, "warnings": 1, "muted": 0})

    def test_kdk_marks_the_submission_blockers(self):
        """Only the rules kdk says block a submission are raised, add-on checker ones or not."""
        head = run(("Kodi Repo", "warning", "addon.xml", 20, "Kodi repo: no <screenshot>",
                    {"rule": "repo.no-screenshot"}),
                   ("Kodi Repo", "warning", "a.pdf", 0, "Kodi repo: a.pdf has a file ending",
                    {"rule": "repo.file-type"}), warnings=2)
        facts = self.facts(None, head)
        rules = [{"id": "repo.no-screenshot", "addon_checker": None, "blocks_submission": True},
                 {"id": "repo.file-type", "addon_checker": "warning", "blocks_submission": False}]
        with mock.patch.object(skin, "kdk_rules", return_value=rules):
            skin.add_rules(facts, Path("unused"))
        self.assertEqual(facts["skins"][0]["head"], {"errors": 1, "warnings": 1, "muted": 0})
        self.assertEqual([r["severity"] for r in facts["skins"][0]["all"]], ["error", "warning"])

    def test_clean_update(self):
        """An update adding nothing says so and folds only what was already there."""
        same = run(("Fonts", "warning", "xml/Font.xml", 3, "Unused font: a"))
        text = skin.skin_text(self.facts(same, same))
        self.assertIn("No new problems. 1 already there before 1.2.0.", text)
        self.assertNotIn("🔴", text)
        self.assertNotIn("🟡", text)

    def test_new_skin_counts_everything(self):
        """A new skin is named so and its sections count every finding."""
        head = run(("IDs", "error", "xml/a.xml", 1, "Window id: 0"),
                   ("Fonts", "warning", "xml/Font.xml", 2, "Unused font: a"), errors=1, warnings=1)
        text = skin.skin_text(self.facts(None, head))
        self.assertIn("`skin.test` 1.2.0, a new skin · Kodi 22 (Piers)\n\n"
                      "1 error and 1 warning.", text)
        self.assertIn("🔴 <b>1 error, 1 kind</b>", text)
        self.assertNotIn("already there", text)

    def test_one_row_per_message_with_capped_places(self):
        """Findings sharing a message share a row whose places stop at the cap, most found first."""
        head = run(*[("IDs", "error", f"xml/{n % 2}.xml", n, "Window id: 0")
                     for n in range(1, 41)], ("IDs", "error", "xml/b.xml", 7, "Window id: 7"))
        text = skin.skin_text(self.facts(None, head))
        self.assertIn("<code>41</code> in 3 files", text)
        self.assertIn("| `Window id: 0` | 40 | `xml/0.xml` 2, 4, 6, 8, 10, 12, 14, 16, 18, 20 "
                      "and 30 more |\n| `Window id: 7` | 1 | `xml/b.xml` 7 |", text)

    def test_places_span_files_in_one_cell(self):
        """A row's places group by file, a file-level finding naming the file alone."""
        head = run(("IDs", "error", "xml/a.xml", 3, "Window id: 0"),
                   ("IDs", "error", "xml/b.xml", 0, "Window id: 0"),
                   ("IDs", "error", "xml/a.xml", 9, "Window id: 0"))
        self.assertIn("| `Window id: 0` | 3 | `xml/a.xml` 3, 9; `xml/b.xml` |",
                      skin.skin_text(self.facts(None, head)))

    def language_facts(self, *languages):
        """Return facts for a new skin with one finding kdk folded across some languages."""
        head = run(*[("Kodi Repo", "warning", PO.format(lang), 9, "Syntax error",
                      {"rule": "repo.po-parse"}) for lang in languages])
        report = {"rows": [folded("repo.po-parse", "warning", languages, 9, "Syntax error")]}
        return self.facts(None, head, report)

    def test_language_rows_share_one_row(self):
        """Translation files kdk folded share one table row naming each language with its line."""
        text = skin.skin_text(self.language_facts("de_de", "en_gb"))
        self.assertIn("| `Syntax error` | 2 | `strings.po` de\\_de 9, en\\_gb 10 |", text)
        self.assertIn("<code>2</code> in 2 files", text)
        self.assertIn("for example `language/resource.language.de_de/strings.po` line 9", text)

    def test_language_codes_are_escaped(self):
        """A language folder name from the skin cannot inject HTML outside a code span."""
        text = skin.skin_text(self.language_facts("<img src=x>", "en_gb"))
        self.assertIn("&lt;img src=x&gt; 9, en\\_gb 10", text)
        self.assertNotIn("<img", re.sub(r"`[^`]*`", "", text))

    def test_skin_text_cannot_ping_link_or_break_the_table(self):
        """Text from a skin, kept in code spans, cannot mention, link or split a table cell."""
        head = run(("Variables", "error", "xml/a|b.xml", 1,
                    "Unused variable: @xbmc/team see https://evil.org #123 `x`"))
        text = review.unlinked(skin.skin_text(self.facts(run(), head)))
        self.assertIn("`Unused variable: @xbmc/team see https://evil.org #123 'x'`", text)
        self.assertIn("| 1 | `xml/a\\|b.xml` 1 |", text)
        outside = text.replace("`Unused variable: @xbmc/team see https://evil.org #123 'x'`", "")
        self.assertNotIn("@xbmc", outside)

    def test_muted_findings_fold_with_their_reason(self):
        """A finding the skin muted is counted apart and listed with the skin's reason."""
        head = run(("Labels", "warning", "xml/a.xml", 3, "Not translated",
                    {"muted": {"line": 2, "reason": "theme name"}}))
        text = skin.skin_text(self.facts(run(), head))
        self.assertIn("No new problems. 1 muted by the skin.", text)
        self.assertIn("<details><summary><b>1 muted by the skin</b></summary>", text)
        self.assertIn("<code>1</code>, muted as `theme name`", text)

    def test_overflow_keeps_the_largest_findings_first(self):
        """Past the room, a table keeps its largest findings and counts the rest in one row."""
        head = run(*[("IDs", "error", "xml/a.xml", n, f"Control ID {n % 60} not defined")
                     for n in range(1, 121)])
        with mock.patch.object(skin, "COMMENT_LIMIT", 2500):
            text = skin.skin_text(self.facts(None, head))
        self.assertIn("| and 35 more findings | 70 | |", text)

    def test_overflow_cuts_tables_and_points_at_the_run(self):
        """Past the room, small kinds lose their tables first and the comment says so."""
        head = run(*[("IDs", "error", f"xml/{n}.xml", n, f"Control ID {n} not defined",
                      {"rule": f"ids.r{n % 6}"}) for n in range(1, 200)])
        with mock.patch.object(skin, "COMMENT_LIMIT", 4000):
            text = skin.skin_text(self.facts(None, head))
        self.assertIn("Some places are left out to fit a comment; the [run's summary]"
                      "(https://github.com/MikeSiLVO/kodi-review/actions/runs/1)", text)
        self.assertLess(len(text), 4200)

    def test_not_checked(self):
        """A failed check states its reason."""
        self.assertEqual(skin.skin_text({"error": "the pull request targets nexus"}),
                         "**Skin check** · not checked: the pull request targets nexus.")

    def test_report_marks_new_rows(self):
        """The report lists every head finding by check, with new and muted ones marked."""
        base = run(("Fonts", "warning", "xml/Font.xml", 2, "Unused font: a"))
        head = run(("Fonts", "warning", "xml/Font.xml", 2, "Unused font: a"),
                   ("Fonts", "warning", "xml/Font.xml", 9, "Unused font: b|c"),
                   ("Labels", "warning", "xml/a.xml", 3, "Not translated",
                    {"muted": {"reason": "name"}}))
        report = skin.report_text(self.facts(base, head))
        self.assertIn("### Fonts (2)", report)
        self.assertIn("|  | warning | `xml/Font.xml:2` | `Unused font: a` |", report)
        self.assertIn("| new | warning | `xml/Font.xml:9` | `Unused font: b\\|c` |", report)
        self.assertIn("| muted | warning | `xml/a.xml:3` | `Not translated` |", report)


class PostTests(unittest.TestCase):
    """Posting the skin check once per source, hiding the earlier ones."""

    def test_checked_before_needs_the_same_build(self):
        """An issue's source counts as checked only under the same kdk and skin.py."""
        posted = [{"body": "<!-- kodi-review skin source=github:o/r@1 kdk=1 style=a -->\nx"},
                  {"body": "<!-- kodi-review skin source=github:o/r@2 -->\nx"}]
        with mock.patch.object(skin, "skin_comments", return_value=posted), \
                mock.patch.object(skin, "skin_build", return_value="kdk=1 style=a"):
            self.assertTrue(skin.checked_before("github:o/r@1"))
            self.assertFalse(skin.checked_before("github:o/r@2"))
        with mock.patch.object(skin, "skin_comments", return_value=posted), \
                mock.patch.object(skin, "skin_build", return_value="kdk=2 style=a"):
            self.assertFalse(skin.checked_before("github:o/r@1"))

    def test_post_and_skip(self):
        """A result posts under its marker and hides earlier checks; a repeat says why instead."""
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(skin, "RUN_DIR", Path(tmp)), \
                mock.patch.dict(os.environ, {"FORK_PR": "7"}), \
                mock.patch.object(skin, "skin_comments", return_value=[{"node_id": "n"}]), \
                mock.patch.object(skin, "hide_summaries") as hide, \
                mock.patch.object(skin, "skin_build", return_value="kdk=1 style=a"), \
                mock.patch.object(skin, "request") as req:
            (Path(tmp) / "skin.json").write_text(json.dumps({"key": "sha=abc", "error": "x"}))
            skin.cmd_post()
            body = req.call_args.args[2]["body"]
            self.assertTrue(body.startswith("<!-- kodi-review skin sha=abc kdk=1 style=a -->\n"))
            self.assertEqual(review.SKIN_MARKER.findall(body), [("sha", "abc", " kdk=1 style=a")])
            hide.assert_called_once()
            req.reset_mock()
            (Path(tmp) / "skin.json").write_text(json.dumps({"key": "source=k", "skip": "done",
                                                             "checked": "`o/r@1`"}))
            with mock.patch.object(skin, "tell") as tell:
                skin.cmd_post()
            req.assert_not_called()
            tell.assert_called_once_with(
                "skin source=k kdk=1 style=a",
                "Already checked `o/r@1`, and my checks haven't changed since.")


class SandboxTests(unittest.TestCase):
    """The bubblewrap command kdk runs under."""

    def test_sandbox_command(self):
        """The sandbox gives kdk no network or environment and a read-only skin to check."""
        with mock.patch.dict(os.environ, {"KDK_PYTHON": "/opt/kdk/bin/python"}):
            cmd = skin.sandbox_command(Path("/w/tree"), Path("/w/out"), "-m", "kdk")
        self.assertEqual(cmd[:6], ["bwrap", "--unshare-all", "--die-with-parent", "--new-session",
                                   "--clearenv", "--tmpfs"])
        windows = [cmd[i:i + 3] for i in range(len(cmd))]
        self.assertIn(["--ro-bind", "/w/tree", "/check"], windows)
        self.assertIn(["--bind", "/w/out", "/out"], windows)
        self.assertEqual(cmd[-4:], ["/opt/kdk/bin/python", "-I", "-m", "kdk"])


if __name__ == "__main__":
    unittest.main()
