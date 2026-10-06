"""Offline tests for the review helpers: patch comparison, label rules, Where it stands block."""

import json
import os
import unittest
from unittest import mock

import review

RESULT = {"summary": "Fixes the crash.", "verdict": "Ready to merge", "kodi22": "Yes, for 22.0",
          "kodi22_reason": "Piers has the same bug.", "next_step": "A team member merges it.",
          "findings": []}


def changed(name, patch, sha="0"):
    """Return a file entry shaped like the pull request files API gives it."""
    return {"filename": name, "patch": patch, "sha": sha}


def block(status):
    """Return the Where it stands lines of the rendered summary, list markers stripped."""
    text = review.summary_text(RESULT, status, "f" * 40, [], False)
    lines = text.split("**Where it stands**\n")[1].split("\n\n")[0].splitlines()
    return [line.removeprefix("- ") for line in lines]


class PatchTests(unittest.TestCase):
    """Backport changes compared with the master original."""

    def test_moved_hunk_is_same(self):
        """A hunk that only moved is the same, its new line numbers ignored."""
        ours = [changed("a.cpp", "@@ -10,3 +10,4 @@ void F()\n x\n+y\n z")]
        theirs = [changed("a.cpp", "@@ -52,3 +52,4 @@ void F()\n x\n+y\n z")]
        self.assertEqual(review.differing_files(ours, theirs), [])

    def test_changed_line_differs(self):
        """A file differs when an added line changed."""
        ours = [changed("a.cpp", "@@ -1,2 +1,3 @@\n x\n+y\n z")]
        theirs = [changed("a.cpp", "@@ -1,2 +1,3 @@\n x\n+w\n z")]
        self.assertEqual(review.differing_files(ours, theirs), ["a.cpp"])

    def test_file_on_one_side_differs(self):
        """A file only one of the two touches differs while the shared file matches."""
        ours = [changed("a.cpp", "@@ -1 +1 @@\n-x\n+y")]
        theirs = ours + [changed("b.h", "@@ -1 +1 @@\n-x\n+y")]
        self.assertEqual(review.differing_files(ours, theirs), ["b.h"])

    def test_omitted_patch_compares_blob(self):
        """Without a patch the file compares by its blob sha, differing only when that changes."""
        self.assertEqual(review.differing_files([changed("a.png", None, "1")],
                                                [changed("a.png", None, "1")]), [])
        self.assertEqual(review.differing_files([changed("a.png", None, "1")],
                                                [changed("a.png", None, "2")]), ["a.png"])


class LabelTests(unittest.TestCase):
    """The upstream mergeable rules."""

    def test_ready(self):
        """A milestone and a version label with nothing blocking are ready."""
        self.assertEqual(review.label_problems("Fix", ["v23 Q*", "Type: Fix"], "Q* 23.0"), [])

    def test_infrastructure_counts_as_version(self):
        """An Infrastructure label counts as a version label, leaving no problem to report."""
        self.assertEqual(review.label_problems("Fix", ["Infrastructure"], "Q* 23.0"), [])

    def test_missing_milestone_and_version(self):
        """No milestone and no version label are both reported."""
        self.assertEqual(review.label_problems("Fix", ["Type: Fix"], None),
                         ["no milestone", "no version label"])

    def test_abandoned_milestone(self):
        """An Abandoned milestone does not count."""
        self.assertEqual(review.label_problems("Fix", ["v23 Q*"], "Abandoned"),
                         ["milestone Abandoned"])

    def test_blocking_label_and_title_tag(self):
        """Only an exact blocking label blocks, and a title tag is reported."""
        self.assertEqual(review.label_problems("[WIP] Fix", ["v23 Q*", "WIP", "WIP: later"], "Q*"),
                         ["labeled WIP", "[WIP] in title"])


class RenderTests(unittest.TestCase):
    """The rendered summary comment."""

    def test_every_line(self):
        """Each fact gets its line, in order, before the reviewed line."""
        status = {"base": "master", "conflicts": True, "label_problems": ["no milestone"],
                  "reviews": [{"login": "a", "state": "APPROVED", "team": True},
                              {"login": "b", "state": "CHANGES_REQUESTED", "team": False,
                               "author_pushed_since": True, "author_commented_since": False}],
                  "builds": {"count": 2, "failing": ["Jenkins"], "pending": [],
                             "links": {"Jenkins": "https://jenkins.kodi.tv/job/1/"}},
                  "backport": {"backports": []}, "unavailable": []}
        self.assertEqual(block(status), [
            "**Reviews:** approved by a (team); changes requested by b, author pushed since",
            "**Test builds:** failing ([Jenkins](https://jenkins.kodi.tv/job/1/))",
            "**Conflicts:** yes, needs a rebase onto master",
            "**Labels:** no milestone",
            "**Kodi 22:** Yes, for 22.0. Piers has the same bug. Backport: none yet.",
            "**Next:** A team member merges it."])
        text = review.summary_text(RESULT, status, "f" * 40, [], False)
        self.assertTrue(text.startswith("### Ready to merge once the test builds pass and the "
                                        "conflicts are resolved\nFixes the crash.\n\n---\n\n"))
        self.assertIn("**Where it stands**\n- **Reviews:**", text)
        self.assertTrue(text.endswith("\n\n<sub>Reviewed up to ffffffffffff.</sub>"))

    def test_verdict_line_counts(self):
        """The verdict line counts problems by severity, naming a lone one's severity alone."""
        result = dict(RESULT, verdict="Needs more work")
        self.assertEqual(review.verdict_line(dict(result, findings=[{"severity": "Moderate"}]), {}),
                         "Needs more work · 1 problem (Moderate)")
        findings = [{"severity": "Minor"}, {"severity": "Serious"}, {"severity": "Minor"}]
        self.assertEqual(review.verdict_line(dict(result, findings=findings), {}),
                         "Needs more work · 3 problems (1 Serious, 2 Minor)")


    def test_quiet_lines_skipped(self):
        """Reviews, conflicts and labels with nothing to say have their lines skipped."""
        status = {"base": "master", "conflicts": False, "label_problems": [], "reviews": [],
                  "builds": {"count": 1, "failing": [], "pending": []}, "backport": None}
        self.assertEqual(block(status), [
            "**Test builds:** passing",
            "**Kodi 22:** Yes, for 22.0. Piers has the same bug.",
            "**Next:** A team member merges it."])

    def test_unanswered_change_request(self):
        """A change request with no push or comment after it says so."""
        status = {"reviews": [{"login": "b", "state": "CHANGES_REQUESTED", "team": True,
                               "author_pushed_since": False, "author_commented_since": False}]}
        self.assertEqual(block(status)[0],
                         "**Reviews:** changes requested by b (team), no answer yet")

    def test_backport_original(self):
        """A Piers backport names its original, its state and any files that differ."""
        merged = {"original": 29502, "state": "merged", "differs": []}
        self.assertEqual(review.backport_text(merged), " Original PR 29502 merged, same changes.")
        open_ = {"original": 29589, "state": "draft", "differs": ["a.cpp", "b.h"]}
        self.assertEqual(review.backport_text(open_),
                         " Original PR 29589 not merged yet, changes differ in a.cpp, b.h.")
        self.assertEqual(review.backport_text({"original": None}),
                         " Original master PR not found.")

    def test_missing_status(self):
        """Without status facts only the model's lines and the unavailable note remain."""
        self.assertEqual(block({"unavailable": ["pull request"]}), [
            "**Kodi 22:** Yes, for 22.0. Piers has the same bug.",
            "**Next:** A team member merges it.",
            "**Unavailable:** pull request"])


class SettledVerdictTests(unittest.TestCase):
    """The verdict corrected to agree with the findings and the status."""

    def settle(self, verdict, severities=(), status=None):
        """Settle a copy of RESULT rewritten for one test case."""
        result = dict(RESULT, verdict=verdict, findings=[{"severity": s} for s in severities])
        return review.settled_verdict(result, status or {})

    def test_ready_with_findings(self):
        """Ready to merge with Minor findings becomes small fixes, with worse ones more work."""
        self.assertEqual(self.settle("Ready to merge", ["Minor"]), "Merge after small fixes")
        self.assertEqual(self.settle("Ready to merge", ["Minor", "Moderate"]), "Needs more work")

    def test_small_fixes_without_findings(self):
        """Merge after small fixes with no findings becomes Ready to merge."""
        self.assertEqual(self.settle("Merge after small fixes"), "Ready to merge")

    def test_ready_waits_on_builds(self):
        """Ready to merge waits on failing builds; passing ones add nothing."""
        failing = {"builds": {"failing": ["Jenkins"]}}
        self.assertEqual(self.settle("Ready to merge", status=failing),
                         "Ready to merge once the test builds pass")
        self.assertEqual(self.settle("Ready to merge", status={"builds": {"failing": []}}),
                         "Ready to merge")

    def test_other_verdicts_kept(self):
        """Verdicts that already agree with the facts are left alone."""
        self.assertEqual(self.settle("Needs more work", ["Serious"]), "Needs more work")
        self.assertEqual(self.settle("Needs a team decision"), "Needs a team decision")


class FailedRunTests(unittest.TestCase):
    """Failed runs, which keep the recorded commit."""

    def post_failure(self, comment):
        """Post a failure through cmd_post with or without a summary comment, return the body."""
        with mock.patch.object(review, "summary_comment", return_value=comment), \
                mock.patch.object(review, "request") as request, \
                mock.patch.dict(os.environ, {"HEAD_SHA": "b" * 40, "FORK_PR": "7", "RESULT": ""}):
            review.cmd_post()
        return request.call_args.args[2]["body"]

    def test_keeps_reviewed_commit(self):
        """The marker keeps the commit the last finished review covered."""
        comment = {"id": 1, "body": f"<!-- kodi-review sha={'a' * 40} -->\nold"}
        body = self.post_failure(comment)
        self.assertTrue(body.startswith(f"<!-- kodi-review sha={'a' * 40} -->"))

    def test_first_failure_leaves_empty_marker(self):
        """A failed first run leaves an empty marker that still matches as no reviewed commit."""
        body = self.post_failure(None)
        self.assertTrue(body.startswith("<!-- kodi-review sha= -->"))
        self.assertIsNotNone(review.MARKER.match(body))
        with mock.patch.object(review, "summary_comment", return_value={"id": 1, "body": body}):
            self.assertEqual(review.reviewed_sha(), "")


class UnlinkedTests(unittest.TestCase):
    """Posted text that pings nobody and links no upstream pull request."""

    def test_mention_outside_code(self):
        """A mention gets a zero-width space; code, emails and paths keep their @."""
        self.assertEqual(review.unlinked("Ask @fuzzard, not `@code`, a@b.com or x/@y"),
                         "Ask @​fuzzard, not `@code`, a@b.com or x/@y")

    def test_upstream_links_and_refs(self):
        """Upstream links and owner/repo references become upstream PR numbers."""
        text = ("See https://github.com/xbmc/xbmc/pull/29589/files, "
                "github.com/xbmc/xbmc/issues/7#issuecomment-12. and xbmc/xbmc#29502.")
        self.assertEqual(review.unlinked(text),
                         "See upstream PR 29589, upstream PR 7. and upstream PR 29502.")

    def test_bare_number(self):
        """A bare number of three or more digits becomes PR N; short ones and entities stay."""
        self.assertEqual(review.unlinked("Since #24720, not #12 or &#123;"),
                         "Since PR 24720, not #12 or &#123;")

    def test_code_block_kept(self):
        """A fenced block keeps its mentions and numbers."""
        text = "```\n@x #123 xbmc/xbmc#5\n```"
        self.assertEqual(review.unlinked(text), text)

    def test_post_unlinks(self):
        """The summary and line comments are posted with mentions and numbers unlinked."""
        finding = {"path": "a.cpp", "line": 2, "severity": "Minor", "title": "Typo",
                   "problem": "Ask @a.", "fix": "See #12345."}
        result = dict(RESULT, summary="Since xbmc/xbmc#24720.", findings=[finding])
        files = [{"filename": "a.cpp", "patch": "@@ -1,2 +1,3 @@\n x\n+y\n z"}]
        with mock.patch.object(review, "summary_comment", return_value=None), \
                mock.patch.object(review, "changed_files", return_value=files), \
                mock.patch.object(review, "request") as request, \
                mock.patch.dict(os.environ, {"HEAD_SHA": "b" * 40, "FORK_PR": "7",
                                             "RESULT": json.dumps(result)}):
            review.cmd_post()
        line_comment = request.call_args_list[0].args[2]["comments"][0]["body"]
        summary = request.call_args_list[1].args[2]["body"]
        self.assertIn("Ask @​a.", line_comment)
        self.assertIn("See PR 12345.", line_comment)
        self.assertIn("Since upstream PR 24720.", summary)

    def test_finding_title(self):
        """A finding opens with its severity and title in bold, then the detail and the fix."""
        finding = {"severity": "Moderate", "title": "3D output breaks", "problem": "Both eyes.",
                   "fix": "Skip the cache."}
        self.assertEqual(review.finding_text(finding),
                         "**Moderate: 3D output breaks**\n\nBoth eyes.\n\n**Fix:** Skip the cache.")


class DiscussionTests(unittest.TestCase):
    """The upstream conversation written for the reviewer."""

    def comment(self, cid, user, kind, when, body, path=None, reply_to=None):
        """Return a comment shaped like the issue and review comments APIs give it."""
        return {"id": cid, "user": {"login": user, "type": kind}, "created_at": when,
                "body": body, "path": path, "line": 7 if path else None,
                "in_reply_to_id": reply_to}

    def test_people_and_answered_bot_comments(self):
        """People's comments stay, newest first, with a bot comment only when a person answered."""
        comments = [
            self.comment(1, "bot", "Bot", "2026-01-01T00:00:00Z", "Throttle this.", "a.cpp"),
            self.comment(2, "dev", "User", "2026-01-02T00:00:00Z", "Not needed.", "a.cpp", 1),
            self.comment(3, "bot", "Bot", "2026-01-03T00:00:00Z", "Unanswered.", "b.cpp"),
            self.comment(4, "author", "User", "2026-01-04T00:00:00Z", "Intentional."),
        ]
        self.assertEqual(review.discussion_text(comments),
                         "author, 2026-01-04\nIntentional.\n\n"
                         "dev, 2026-01-02, a.cpp:7\nNot needed.\n\n"
                         "bot, 2026-01-01, a.cpp:7\nThrottle this.\n")

    def test_cap(self):
        """Older comments past the size cap are left out."""
        comments = [self.comment(i, "dev", "User", f"2026-01-{i + 1:02}T00:00:00Z", "x" * 9000)
                    for i in range(5)]
        self.assertEqual(review.discussion_text(comments).count("dev, "), 2)


class PriorTests(unittest.TestCase):
    """Earlier findings: which stay open and which get resolved."""

    def thread(self, tid, logins, resolved=False):
        """Return a review thread shaped like the GraphQL reviewThreads nodes."""
        return {"id": tid, "isResolved": resolved, "path": "a.cpp", "line": 3,
                "comments": {"nodes": [{"author": {"login": login}, "body": f"by {login}"}
                                       for login in logins]}}

    def test_open_findings(self):
        """Only the bot's unresolved threads without a reply from anyone else count."""
        threads = [self.thread("T1", ["github-actions"]),
                   self.thread("T2", ["github-actions"], resolved=True),
                   self.thread("T3", ["github-actions", "MikeSiLVO"]),
                   self.thread("T4", ["someone"])]
        self.assertEqual([f["id"] for f in review.open_findings(threads)], ["T1"])

    def test_post_resolves_listed_only(self):
        """Only listed fixed ids are resolved, once each; open ones still count in the verdict."""
        result = dict(RESULT, fixed=["T1", "T9", "T1"])
        with mock.patch.object(review, "summary_comment", return_value=None), \
                mock.patch.object(review, "changed_files", return_value=[]), \
                mock.patch.object(review, "load_prior", return_value=[
                    {"id": "T1", "finding": "**Minor: A**"},
                    {"id": "T2", "finding": "**Moderate:** B"}]), \
                mock.patch.object(review, "request") as request, \
                mock.patch.object(review, "graphql") as graphql, \
                mock.patch.dict(os.environ, {"HEAD_SHA": "b" * 40, "FORK_PR": "7",
                                             "RESULT": json.dumps(result)}):
            review.cmd_post()
        graphql.assert_called_once_with(review.RESOLVE_MUTATION, id="T1")
        body = request.call_args.args[2]["body"]
        self.assertIn("Resolved 1 earlier finding. 1 earlier finding still open.", body)
        self.assertIn("### Needs more work · 1 problem (Moderate)", body)


class RepeatTests(unittest.TestCase):
    """New findings at a spot an earlier finding still holds."""

    def test_repeat_dropped_and_counted_once(self):
        """A repeat at an open spot is not posted, and duplicate open threads count once."""
        prior = [{"id": "T1", "path": "a.cpp", "line": 2, "finding": "**Serious: Old**"},
                 {"id": "T2", "path": "a.cpp", "line": 2, "finding": "**Serious:** Older"}]
        repeat = {"path": "a.cpp", "line": 2, "severity": "Serious", "title": "Again",
                  "problem": "Same.", "fix": "Same."}
        other = dict(repeat, line=3, severity="Moderate", title="New")
        result = dict(RESULT, verdict="Needs more work", findings=[repeat, other])
        files = [{"filename": "a.cpp", "patch": "@@ -1,2 +1,3 @@\n x\n+y\n+z"}]
        with mock.patch.object(review, "summary_comment", return_value=None), \
                mock.patch.object(review, "changed_files", return_value=files), \
                mock.patch.object(review, "load_prior", return_value=prior), \
                mock.patch.object(review, "request") as request, \
                mock.patch.dict(os.environ, {"HEAD_SHA": "b" * 40, "FORK_PR": "7",
                                             "RESULT": json.dumps(result)}):
            review.cmd_post()
        posted = request.call_args_list[0].args[2]["comments"]
        self.assertEqual([c["line"] for c in posted], [3])
        summary = request.call_args_list[1].args[2]["body"]
        self.assertIn("### Needs more work · 2 problems (1 Serious, 1 Moderate)", summary)
        self.assertIn("1 earlier finding still open.", summary)


class UsageTests(unittest.TestCase):
    """The token report printed from the execution file."""

    def test_usage_lines(self):
        """Each tool call shows its target and result size, never the result itself."""
        messages = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "u1", "name": "Read",
                 "input": {"file_path": "a.cpp", "offset": 10, "limit": 50}},
                {"type": "tool_use", "id": "u2", "name": "Grep", "input": {"pattern": "Foo"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "u1", "content": "secret body"},
                {"type": "tool_result", "tool_use_id": "u2", "content": [{"text": "x"}]}]}},
            {"type": "result", "usage": {"output_tokens": 5}, "num_turns": 2,
             "total_cost_usd": 0.1}]
        lines = review.usage_lines(messages)
        self.assertEqual(lines[0], "      11 chars  Read a.cpp 10+50")
        self.assertTrue(lines[1].endswith("chars  Grep Foo"))
        self.assertEqual(lines[2], 'tokens {"output_tokens": 5} turns 2 cost 0.1')
        self.assertNotIn("secret", "\n".join(lines))


class WithheldTests(unittest.TestCase):
    """Review output that looks like it carries a credential."""

    def test_credential_is_withheld(self):
        """Only the withheld note is posted, under the old commit, and the command exits with 1."""
        result = dict(RESULT, summary="Found ghp_" + "a" * 36 + " in the log.")
        comment = {"id": 1, "body": f"<!-- kodi-review sha={'a' * 40} -->\nold"}
        with mock.patch.object(review, "summary_comment", return_value=comment), \
                mock.patch.object(review, "changed_files", return_value=[]), \
                mock.patch.object(review, "request") as request, \
                mock.patch.dict(os.environ, {"HEAD_SHA": "b" * 40, "FORK_PR": "7",
                                             "RESULT": json.dumps(result)}), \
                self.assertRaises(SystemExit) as stop:
            review.cmd_post()
        self.assertEqual(stop.exception.code, 1)
        request.assert_called_once()
        self.assertEqual(request.call_args.args[2]["body"],
                         f"<!-- kodi-review sha={'a' * 40} -->\n{review.WITHHELD}")

    def test_patterns(self):
        """Anthropic keys, GitHub tokens and fine-grained PATs match; look-alikes do not."""
        for text in ("sk-ant-api03-x", "gho_" + "A1" * 10, "github_pat_11ABC"):
            self.assertRegex(text, review.CREDENTIAL)
        for text in ("ghp_short", "sk-other", "github pat"):
            self.assertNotRegex(text, review.CREDENTIAL)


if __name__ == "__main__":
    unittest.main()
