"""Offline tests for the add-on check's own rules: pull request shape, titles, links, provider."""

import io
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest import mock

import addon
import skin

ADDON = {"id": "script.test", "version": "1.2.0", "provider": "someone"}
METADATA = ('<addon id="script.test" version="1.2.0"><extension point="xbmc.addon.metadata">'
            "<forum>https://forum.kodi.tv/showthread.php?tid=1</forum>"
            "<source> https://github.com/someone/script.test </source><website/>"
            '</extension><extension point="xbmc.python.script" library="default.py"/></addon>')


def rules(rows):
    """Return the rule ids of some rows, in order."""
    return [r["rule"] for r in rows]


def commit(subject, parents=1):
    """Return a pull request commit as the API lists it."""
    return {"commit": {"message": f"{subject}\n\nbody"}, "parents": [{}] * parents}


class PullShapeTests(unittest.TestCase):
    """Folders, commits and titles of a pull request."""

    def test_one_folder_one_commit(self):
        """One add-on folder in one commit raises nothing."""
        folders, loose = addon.changed_places([{"filename": "script.test/addon.xml"},
                                               {"filename": "script.test/default.py"}])
        self.assertEqual((folders, loose), (["script.test"], []))
        self.assertEqual(addon.commit_rows([commit("[script.test] 1.2.0")], folders, loose), [])

    def test_second_folder_or_root_file(self):
        """A second folder or a file at the repo root breaks the one add-on rule."""
        for files in (["script.a/addon.xml", "script.b/addon.xml"],
                      ["script.a/addon.xml", "README.md"]):
            folders, loose = addon.changed_places([{"filename": f} for f in files])
            self.assertEqual(rules(addon.commit_rows([commit("x")], folders, loose)),
                             ["pr.one-addon"])

    def test_commits(self):
        """More than one commit, or a merge commit, is an error."""
        rows = addon.commit_rows([commit("a"), commit("b", parents=2)], ["script.test"], [])
        self.assertEqual(rules(rows), ["pr.single-commit", "pr.merge-commit"])

    def test_titles(self):
        """Subject and title read [<id>] <version>; v and @<target> pass, other branches do not."""
        base = {"base": {"ref": "matrix"}}
        meta = {**base, "title": "[script.test@matrix] v1.2.0"}
        self.assertEqual(addon.title_rows(meta, [commit("[script.test] 1.2.0")], ADDON), [])
        rows = addon.title_rows({**base, "title": "Update"},
                                [commit("[script.test@omega] 1.2.0")], ADDON)
        self.assertEqual(rules(rows), ["pr.commit-subject", "pr.title"])
        self.assertIn("'[script.test@omega] 1.2.0', not '[script.test] 1.2.0'",
                      rows[0]["message"])


class LinkTests(unittest.TestCase):
    """Metadata links: which are read, which are quiet, and what a redirect or failure gives."""

    def test_links_read_from_metadata(self):
        """Forum and source links are read from the metadata; a DTD or entity reads none."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "addon.xml")
            path.write_text(METADATA)
            self.assertEqual(addon.metadata_links(path),
                             [("forum", "https://forum.kodi.tv/showthread.php?tid=1"),
                              ("source", "https://github.com/someone/script.test")])
            path.write_text('<!DOCTYPE x [<!ENTITY a "b">]>' + METADATA)
            self.assertEqual(addon.metadata_links(path), [])

    def test_quiet_hosts_never_opened(self):
        """Links on hosts that refuse automated requests are never opened."""
        with mock.patch.object(addon, "open_link") as opened:
            self.assertEqual(addon.link_rows("forum", "https://forum.kodi.tv/x"), [])
            self.assertEqual(addon.link_rows("website", "https://www.kodi.wiki/y"), [])
        opened.assert_not_called()

    def test_redirects(self):
        """A redirect elsewhere is a hint; scheme, www., slash changes and quiet hosts pass."""
        cases = [("http://example.com/a", "https://www.example.com/a/", []),
                 ("https://example.com/a", "https://example.org/a", ["addon.url-redirect"]),
                 ("https://example.com/a", "https://forum.kodi.tv/b", [])]
        for given, final, wanted in cases:
            with mock.patch.object(addon, "open_link", return_value=final):
                self.assertEqual(rules(addon.link_rows("website", given)), wanted)

    def test_failure_tries_https_then_http(self):
        """A link without a scheme tries https then http, then reports why it failed."""
        refused = urllib.error.HTTPError("u", 404, "Not Found", Message(), io.BytesIO())
        with mock.patch.object(addon, "open_link", side_effect=refused) as opened:
            rows = addon.link_rows("source", "example.com/repo")
        self.assertEqual([c.args[0] for c in opened.call_args_list],
                         ["https://example.com/repo", "http://example.com/repo"])
        self.assertEqual(rules(rows), ["addon.url-broken"])
        self.assertTrue(rows[0]["message"].endswith("does not open: HTTP 404"))

    def test_private_address_refused(self):
        """A link to a private address counts as broken without being opened."""
        with mock.patch.object(skin.socket, "getaddrinfo",
                               return_value=[(0, 0, 0, "", ("10.0.0.1", 80))]):
            rows = addon.link_rows("website", "http://intranet.example/")
        self.assertEqual(rules(rows), ["addon.url-broken"])


class CopiesTests(unittest.TestCase):
    """Newer release branches that hold their own copy of the add-on."""

    def test_newer_branches_only(self):
        """Only branches above the target are asked; a 404 is no copy, other failures unknown."""
        def answer(method, path):
            """Stand in for the contents API: piers lacks the folder, nexus fails."""
            if "ref=piers" in path:
                raise urllib.error.HTTPError(path, 404, "Not Found", Message(), io.BytesIO())
            if "ref=nexus" in path:
                raise urllib.error.HTTPError(path, 500, "Error", Message(), io.BytesIO())
            return []
        with mock.patch.object(addon, "request", side_effect=answer):
            self.assertEqual(addon.own_copies("script.test", "matrix"), {"omega": True,
                                                                        "piers": False})
            self.assertEqual(addon.own_copies("script.test", "omega"), {"piers": False})
            self.assertEqual(addon.own_copies("script.test", "piers"), {})


class ProviderTests(unittest.TestCase):
    """Provider name and submitter changes against the copy on the branch."""

    def test_changes(self):
        """A changed provider or submitter is a hint, case-blind, and only for an update."""
        meta = {"user": {"login": "newcomer"}}
        last = [{"author": {"login": "someone"}}]
        with mock.patch.object(addon, "request", return_value=last):
            rows = addon.provider_rows(meta, {**ADDON, "provider": "other"}, ADDON,
                                       "script.test", "piers")
        self.assertEqual(rules(rows), ["addon.provider-changed"] * 2)
        with mock.patch.object(addon, "request", return_value=[{"author": {"login": "Newcomer"}}]):
            self.assertEqual(addon.provider_rows(meta, ADDON, ADDON, "script.test", "piers"), [])
        self.assertEqual(addon.provider_rows(meta, ADDON, None, "script.test", "piers"), [])


def kdk_row(rule, severity, message, file="addon.xml"):
    """Return a new row of kdk compare for an add-on."""
    return {"check": "Kodi Repo", "rule": rule, "subject": "", "severity": severity,
            "file": file, "line": 3, "message": message, "muted": False, "reason": "",
            "status": "new", "count": 1}


def facts(**changes):
    """Return add-on check facts with one checked add-on and a title hint, changed as given."""
    po = "resources/language/resource.language.de_de/strings.po"
    rows = [kdk_row("addon.schema", "error", "bad element"),
            kdk_row("lang.orphan-id", "hint", "id 32001 is not in en_gb", po)]
    result = {**ADDON, "folder": "script.test", "release": "omega", "kdk": "3.3.1+abc1234",
              "checker": "0.1.0", "notices": [], "new": False,
              "head": {"errors": 1, "warnings": 0, "hints": 1, "muted": 0},
              "base": {"errors": 0, "warnings": 0, "hints": 0, "muted": 0},
              "base_facts": ADDON, "rows": rows, "existing": [], "all": rows}
    title = addon.bot_row(addon.PULL_CHECK, "pr.title", "hint",
                          "The pull request title is 'Update', not '[script.test] 1.2.0'")
    return {"key": "sha=" + "a" * 40, "branch": "omega", "checked": "`aaaaaaaaaaaa`",
            "pull": [title], "addons": [result], "rules": addon.BOT_RULES, "run": "https://run",
            **changes}


class RenderTests(unittest.TestCase):
    """The add-on check comment and run summary."""

    def test_comment(self):
        """The comment opens with title and verdict and folds hints and pull request findings."""
        text = addon.addon_text(facts())
        self.assertTrue(text.startswith("**Add-on check** · `script.test` 1.2.0 against the "
                                        "version on omega · Kodi 21 (Omega)\n\n"
                                        "1 new error and 2 new hints."))
        self.assertIn("⚪ <b>2 new hints, 2 kinds</b>", text)
        self.assertIn("<b>Pull request</b> · Pull request title is not \\[&lt;id&gt;\\] "
                      "&lt;version&gt; <code>1</code></summary>\n\n`pr.title`\n\n"
                      "- `The pull request title is 'Update', not '[script.test] 1.2.0'`", text)
        self.assertTrue(text.endswith("with addoncheck `0.1.0` · checked `aaaaaaaaaaaa`.</sub>"))

    def test_pull_request_alone(self):
        """Without an add-on checked, the comment carries the pull request's findings alone."""
        target = addon.bot_row(addon.PULL_CHECK, "pr.base-branch", "error",
                               "The pull request targets master, not a Kodi release branch")
        text = addon.addon_text(facts(branch="master", addons=[], pull=[target]))
        self.assertTrue(text.startswith("**Add-on check** · the pull request into master\n\n"
                                        "1 error."))
        self.assertIn("no add-on checked", text)

    def test_not_checked(self):
        """A failed check states its reason."""
        text = addon.addon_text({"key": "run=1", "error": "the pull request targets leia"})
        self.assertEqual(text, "**Add-on check** · not checked: the pull request targets leia.")

    def test_already_there_listed(self):
        """Findings the branch's copy had too get severity sections with places, errors open."""
        old = kdk_row("lang.code-unknown", "error", "no Kodi language is named prs",
                      "resources/language/resource.language.prs")
        new = kdk_row("addon.schema", "error", "bad element")
        result = {**facts()["addons"][0], "rows": [new], "all": [old, new, {**new, "muted": True}]}
        result["there"] = addon.unchanged(result["all"], result["rows"])
        self.assertEqual([r["rule"] for r in result["there"]], ["lang.code-unknown"])
        text = addon.addon_text(facts(addons=[result], pull=[]))
        self.assertIn("1 new error. 1 already there before 1.2.0: 1 error.", text)
        self.assertIn("<details open><summary>🔴 <b>1 error already there, 1 kind</b></summary>",
                      text)
        self.assertIn("| `no Kodi language is named prs` | 1 | "
                      "`resources/language/resource.language.prs` 3 |", text)

    def test_report(self):
        """The report gives each add-on's counts and the pull request's findings."""
        text = addon.report_text(facts())
        self.assertIn("## script.test 1.2.0\n\nKodi 21 (Omega), kdk 3.3.1+abc1234, addoncheck "
                      "0.1.0: 1 error, 0 warnings, 1 hint, 0 muted.", text)
        self.assertIn("### Pull request (1)", text)


def section(path, body="@@ -0,0 +1 @@\n+x\n"):
    """Return one file's section of a git diff."""
    return f"diff --git a/{path} b/{path}\nindex 0..1 100644\n--- a/{path}\n+++ b/{path}\n{body}"


class PrepTests(unittest.TestCase):
    """The AI review's add-on inputs: the filtered diff, the facts and the check's findings."""

    def test_diff_leaves_out_translations_binaries_and_bundled_code(self):
        """PO files, images, binary patches and bundled folders drop out and are listed."""
        diff = "".join([section("script.test/default.py"),
                        section("script.test/resources/language/resource.language.de_de/strings.po"),
                        section("script.test/icon.png"),
                        section("script.test/data.bin", "Binary files a/x and b/x differ\n"),
                        section("script.test/resources/lib/requests/api.py"),
                        section("script.test/resources/lib/main.py")])
        kept, dropped = addon.review_diff(diff, "script.test", ["resources/lib/requests/"])
        self.assertEqual(addon.DIFF_SECTION.findall(kept),
                         [("script.test/default.py", "script.test/default.py"),
                          ("script.test/resources/lib/main.py",
                           "script.test/resources/lib/main.py")])
        self.assertEqual(dropped, ["script.test/data.bin", "script.test/icon.png",
                                   "script.test/resources/language/resource.language.de_de/"
                                   "strings.po", "script.test/resources/lib/requests/api.py"])

    def test_facts_name_target_update_and_left_out_files(self):
        """Facts file names target, Python, replaced version, newer copies and extension points."""
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            (tree / "script.test").mkdir()
            (tree / "script.test" / "addon.xml").write_text(
                '<addon id="script.test" name="Test" version="1.2.0"><requires>'
                '<import addon="xbmc.python" version="3.0.1"/></requires>'
                '<extension point="xbmc.service" library="service.py"/>'
                '<extension point="xbmc.addon.metadata"/></addon>')
            result = {**ADDON, "folder": "script.test", "bundled": [],
                      "base_facts": {"version": "1.1.0"}}
            text = addon.addon_facts_text(result, "omega", tree, ["script.test/icon.png"],
                                          {"piers": False})
        self.assertIn("- Target: omega, Kodi 21 (Omega), Python 3.8", text)
        self.assertIn("- Update of version 1.1.0 on omega", text)
        self.assertIn("- Newer branch piers: has no copy, so its Kodi gets this one", text)
        self.assertIn("- xbmc.service: service.py", text)
        self.assertIn("- xbmc.python 3.0.1", text)
        self.assertIn("Left out of full.diff (1):\n- script.test/icon.png", text)

    def test_checks_list_new_and_old_findings_from_the_repo_root(self):
        """The checks file lists new and already-there findings, errors first, one line each."""
        new = kdk_row("addon.schema", "warning", "bad\nelement")
        old = {**kdk_row("lang.code-unknown", "error", "no language prs", "resources/x"),
               "status": "there"}
        text = addon.checks_text({"folder": "script.test", "rows": [new], "there": [old]})
        self.assertIn("- error lang.code-unknown script.test/resources/x:3: no language prs\n"
                      "- warning addon.schema script.test/addon.xml:3: bad element", text)
        empty = addon.checks_text({"folder": "script.test", "rows": [], "there": []})
        self.assertIn("Nothing: the add-on check found no problems.", empty)


if __name__ == "__main__":
    unittest.main()
