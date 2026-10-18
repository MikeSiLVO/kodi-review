"""Offline tests for the skin check: links, safe unpacking, skin lookup, comparing and rendering."""

import io
import json
import os
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


def run(*rows, errors=0, warnings=0, muted=0):
    """Return a kdk report holding the rows as (check, severity, file, line, message[, muted])."""
    categories = {}
    for rule, severity, file, line, message, *mute in rows:
        item = {"severity": severity, "file": file, "line": line, "message": message}
        if mute:
            item["muted"] = mute[0]
        categories.setdefault(rule, []).append(item)
    return {"kdk_version": "2.1.0", "errors": errors, "warnings": warnings, "muted": muted,
            "notices": [], "categories": categories}


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


class CompareTests(unittest.TestCase):
    """Telling new findings from ones already there, folding repeats across language files."""

    def test_new_and_fixed(self):
        """A finding only on head is new, only on base fixed; lines moving changes nothing."""
        base = run(("Fonts", "warning", "xml/Font.xml", 10, "Unused font: a"),
                   ("Includes", "error", "xml/Home.xml", 5, "Include not defined: x"))
        head = run(("Fonts", "warning", "xml/Font.xml", 12, "Unused font: a"),
                   ("Variables", "error", "xml/Colors.xml", 140, "Duplicate variable 'c'"))
        rows = skin.compare(base, head)
        self.assertEqual([(r["status"], r["message"]) for r in rows],
                         [("new", "Duplicate variable 'c'"), ("fixed", "Include not defined: x")])

    def test_language_files_fold(self):
        """One string finding repeated in every language is one row naming its files."""
        po = "language/resource.language.{}/strings.po"
        base = run(("Kodi Repo", "warning", po.format("en_gb"), 1100,
                    "id 31496 twice (first on line 900)"))
        head = run(*[("Kodi Repo", "warning", po.format(lang), 1069,
                      f"id 31496 twice (first on line {962 + n})")
                     for n, lang in enumerate(("en_gb", "af_za", "de_de"))])
        rows = skin.compare(base, head)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["count"], rows[0]["file"], rows[0]["message"]),
                         (2, "language/*/strings.po", "id 31496 twice (first on line #)"))
        self.assertEqual(len(rows[0]["files"]), 3)

    def test_no_base_makes_all_new(self):
        """A skin with no base has every finding new, repeats counted once per row."""
        head = run(("IDs", "error", "xml/a.xml", 1, "Window id: 0"),
                   ("IDs", "error", "xml/a.xml", 1, "Window id: 0"))
        rows = skin.compare(None, head)
        self.assertEqual([(r["status"], r["count"]) for r in rows], [("new", 2)])

    def test_muted_stays_apart(self):
        """A finding muted on head is a separate row from the same finding unmuted on base."""
        base = run(("Labels", "warning", "xml/a.xml", 3, "Label not translated: x"))
        head = run(("Labels", "warning", "xml/a.xml", 3, "Label not translated: x", "theme name"))
        rows = skin.compare(base, head)
        self.assertEqual([(r["status"], r["muted"]) for r in rows],
                         [("new", True), ("fixed", False)])


class RenderTests(unittest.TestCase):
    """The skin check comment and the run summary report."""

    def facts(self, base, head):
        """Return skin.json facts for one skin compared from the two reports."""
        result = {"id": "skin.test", "version": "1.2.0", "release": "piers", "kdk": "2.1.0",
                  "notices": [], "new_skin": base is None, "head": skin.totals(head),
                  "base": skin.totals(base) if base else None, "rows": skin.compare(base, head),
                  "all": skin.findings(head)}
        return {"key": "sha=" + "a" * 40, "checked": "aaaa against bbbb", "skins": [result],
                "run": "https://github.com/MikeSiLVO/kodi-review/actions/runs/1"}

    def test_update_lists_new_problems(self):
        """An update names its new problems and what was already there."""
        base = run(errors=4, warnings=9)
        head = run(("Variables", "error", "xml/Colors.xml", 140, "Duplicate variable 'c'"),
                   errors=5, warnings=9)
        text = skin.skin_text(self.facts(base, head))
        self.assertIn("**Skin:** `skin.test 1.2.0` against Kodi 22 (Piers): 1 new error.", text)
        two = run(("Variables", "error", "xml/Colors.xml", 140, "Duplicate variable 'c'"),
                  ("Fonts", "warning", "xml/Font.xml", 9, "Unused font: a"),
                  ("Fonts", "warning", "xml/Font.xml", 10, "Unused font: b"))
        self.assertIn("against Kodi 22 (Piers): 1 new error, 2 new warnings.",
                      skin.skin_text(self.facts(base, two)))
        self.assertIn("- Error · `xml/Colors.xml:140` · `Duplicate variable 'c'`", text)
        self.assertIn("Already there: 4 errors, 9 warnings.", text)
        self.assertIn("<sub>kdk 2.1.0 · checked aaaa against bbbb.</sub>", text)

    def test_clean_update(self):
        """An update adding nothing gets one line saying it has no new problems."""
        text = skin.skin_text(self.facts(run(errors=1), run(errors=1)))
        self.assertIn("against Kodi 22 (Piers): no new problems.", text)

    def test_new_skin_lists_errors_and_counts(self):
        """A new skin gets its totals, counts per check and only its errors listed."""
        head = run(("IDs", "error", "xml/a.xml", 1, "Window id: 0"),
                   ("Fonts", "warning", "xml/Font.xml", 2, "Unused font: a"), errors=1, warnings=1)
        text = skin.skin_text(self.facts(None, head))
        self.assertIn("is new here. Against Kodi 22 (Piers): 1 error, 1 warning.", text)
        self.assertIn("By check: IDs 1, Fonts 1.", text)
        self.assertIn("`Window id: 0`", text)
        self.assertNotIn("Unused font", text)

    def test_long_lists_point_at_the_report(self):
        """Past the list limit the rest is counted and linked to the full report."""
        head = run(*[("IDs", "error", f"xml/{n}.xml", 1, "Window id: 0") for n in range(35)])
        text = skin.skin_text(self.facts(None, head))
        self.assertIn("- 5 more in the [full report](https://github.com/MikeSiLVO/kodi-review/"
                      "actions/runs/1).", text)

    def test_skin_text_cannot_ping_or_link(self):
        """Names from a skin stay in code spans that mentions and backticks cannot leave."""
        head = run(("Variables", "error", "xml/a.xml", 1, "Unused variable: @xbmc/team `x` #123"))
        text = review.unlinked(skin.skin_text(self.facts(run(), head)))
        self.assertIn("`Unused variable: @xbmc/team 'x' #123`", text)

    def test_not_checked(self):
        """A failed check states its reason."""
        self.assertEqual(skin.skin_text({"error": "the pull request targets nexus"}),
                         "**Skin:** not checked, the pull request targets nexus.")

    def test_report_marks_new_rows(self):
        """The report lists every head finding by check, with new and muted ones marked."""
        base = run(("Fonts", "warning", "xml/Font.xml", 2, "Unused font: a"))
        head = run(("Fonts", "warning", "xml/Font.xml", 2, "Unused font: a"),
                   ("Fonts", "warning", "xml/Font.xml", 9, "Unused font: b|c"),
                   ("Labels", "warning", "xml/a.xml", 3, "Not translated", "name"))
        report = skin.report_text(self.facts(base, head))
        self.assertIn("### Fonts (2)", report)
        self.assertIn("|  | warning | `xml/Font.xml:2` | `Unused font: a` |", report)
        self.assertIn("| new | warning | `xml/Font.xml:9` | `Unused font: b\\|c` |", report)
        self.assertIn("| muted | warning | `xml/a.xml:3` | `Not translated` |", report)


class PostTests(unittest.TestCase):
    """Posting the skin check once per source, hiding the earlier ones."""

    def test_post_and_skip(self):
        """A result posts under its marker and hides earlier checks; a repeat posts nothing."""
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(skin, "RUN_DIR", Path(tmp)), \
                mock.patch.dict(os.environ, {"FORK_PR": "7"}), \
                mock.patch.object(skin, "skin_comments", return_value=[{"node_id": "n"}]), \
                mock.patch.object(skin, "hide_summaries") as hide, \
                mock.patch.object(skin, "request") as req:
            (Path(tmp) / "skin.json").write_text(json.dumps({"key": "sha=abc", "error": "x"}))
            skin.cmd_post()
            body = req.call_args.args[2]["body"]
            self.assertTrue(body.startswith("<!-- kodi-review skin sha=abc -->\n"))
            self.assertTrue(review.SKIN_MARKER.match(body))
            hide.assert_called_once()
            req.reset_mock()
            (Path(tmp) / "skin.json").write_text(json.dumps({"key": "source=k", "skip": "done"}))
            skin.cmd_post()
            req.assert_not_called()


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
