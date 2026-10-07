"""Offline tests for the review helpers: patch comparison, label rules, Where it stands block."""

import json
import os
import sys
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
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
    """Posted text that pings nobody and links no pull request or issue."""

    def test_mention_outside_code(self):
        """A mention gets a zero-width space; code, emails and paths keep their @."""
        self.assertEqual(review.unlinked("Ask @fuzzard, not `@code`, a@b.com or x/@y"),
                         "Ask @​fuzzard, not `@code`, a@b.com or x/@y")

    def test_upstream_links_and_refs(self):
        """Upstream links and owner/repo references become upstream PR and issue numbers."""
        text = ("See https://github.com/xbmc/xbmc/pull/29589/files, "
                "github.com/XBMC/xbmc/issues/7#issuecomment-12. and xbmc/xbmc#29502.")
        self.assertEqual(review.unlinked(text),
                         "See upstream PR 29589, upstream issue 7. and upstream PR 29502.")

    def test_other_repo_refs(self):
        """Links and references to any other repository are unlinked too, named in full."""
        text = "Like xbmc/repo-scripts#12 and https://github.com/Foo/bar/issues/3."
        self.assertEqual(review.unlinked(text),
                         "Like xbmc/repo-scripts PR 12 and Foo/bar issue 3.")

    def test_bare_number(self):
        """A #N of 3+ digits becomes PR N, a PR prefix kept once; short ones and entities stay."""
        self.assertEqual(review.unlinked("Since #24720, not #12 or &#123;"),
                         "Since PR 24720, not #12 or &#123;")
        self.assertEqual(review.unlinked("Like PR #24720 and pr #24721"),
                         "Like PR 24720 and PR 24721")

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
    """Earlier findings: which stay open, which are settled and which get marked fixed."""

    def thread(self, tid, logins, resolved=False, body="**Minor: Found**"):
        """Return a review thread shaped like the GraphQL reviewThreads nodes."""
        bots = {"kodi-review", "coderabbitai"}
        return {"id": tid, "isResolved": resolved, "path": "a.cpp", "line": 3,
                "comments": {"nodes": [
                    {"databaseId": 11, "body": body if login == "kodi-review" else f"by {login}",
                     "author": {"login": login, "__typename": "Bot" if login in bots else "User"}}
                    for login in logins]}}

    def test_earlier_findings(self):
        """The bot's findings come back with their status, replies or not; other threads do not."""
        threads = [self.thread("T1", ["kodi-review"]),
                   self.thread("T2", ["kodi-review"], resolved=True),
                   self.thread("T3", ["kodi-review", "MikeSiLVO"]),
                   self.thread("T4", ["someone"]),
                   self.thread("T5", ["coderabbitai"]),
                   self.thread("T6", ["kodi-review"],
                               body=review.FIXED_NOTE.format("a" * 12) + "**Minor: Found**"),
                   self.thread("T7", ["kodi-review"],
                               body=review.ACCEPTED_NOTE.format("dev", "Merging anyway.")
                               + "**Minor: Found**\n\nDetail.")]
        found = review.earlier_findings(threads)
        self.assertEqual([(f["id"], f["status"]) for f in found],
                         [("T1", "open"), ("T2", "resolved"), ("T3", "open"), ("T6", "fixed"),
                          ("T7", "accepted")])
        self.assertEqual({f["finding"] for f in found}, {"**Minor: Found**"})
        self.assertEqual(found[0]["comment"], 11)

    def test_post_marks_listed_only(self):
        """Only listed fixed ids get the note, once each; open ones still count in the verdict."""
        result = dict(RESULT, fixed=["T1", "T9", "T1"])
        path = f"/repos/{review.REPO}/pulls/comments/11"
        with mock.patch.object(review, "summary_comment", return_value=None), \
                mock.patch.object(review, "changed_files", return_value=[]), \
                mock.patch.object(review, "load_prior", return_value=[
                    {"id": "T1", "comment": 11, "finding": "**Minor: A**"},
                    {"id": "T2", "comment": 12, "finding": "**Moderate:** B"}]), \
                mock.patch.object(review, "request",
                                  return_value={"body": "**Minor: A**"}) as request, \
                mock.patch.dict(os.environ, {"HEAD_SHA": "b" * 40, "FORK_PR": "7",
                                             "RESULT": json.dumps(result)}):
            review.cmd_post()
        edits = [c.args for c in request.call_args_list if c.args[0] == "PATCH"]
        self.assertEqual(edits, [("PATCH", path, {"body": "**Fixed in `bbbbbbbbbbbb`.**\n\n"
                                                          "**Minor: A**"})])
        body = request.call_args.args[2]["body"]
        self.assertIn("1 earlier finding fixed. 1 earlier finding still open.", body)
        self.assertIn("### Needs more work · 1 problem (Moderate)", body)


class MarkFixedTests(unittest.TestCase):
    """Marking earlier findings fixed when GitHub refuses."""

    def test_refused_edit_not_counted(self):
        """A refused edit is logged, the step goes on, and the footer claims none."""
        def refuse(method, path, body=None, raw=False):
            """Refuse every request for a review comment."""
            if "/pulls/comments/" in path:
                raise urllib.error.URLError("forbidden")
        result = dict(RESULT, fixed=["T1"])
        with mock.patch.object(review, "summary_comment", return_value=None), \
                mock.patch.object(review, "changed_files", return_value=[]), \
                mock.patch.object(review, "load_prior", return_value=[
                    {"id": "T1", "comment": 11, "finding": "**Minor: A**"}]), \
                mock.patch.object(review, "request", side_effect=refuse) as request, \
                mock.patch.dict(os.environ, {"HEAD_SHA": "b" * 40, "FORK_PR": "7",
                                             "RESULT": json.dumps(result)}), \
                mock.patch("builtins.print"):
            review.cmd_post()
        self.assertNotIn("fixed.", request.call_args.args[2]["body"])


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

    def test_settled_spot_holds_without_counting(self):
        """An accepted finding blocks a repeat at its spot, uncounted; a fixed one does neither."""
        prior = [{"id": "T1", "path": "a.cpp", "line": 2, "status": "accepted",
                  "finding": "**Moderate: Old**"},
                 {"id": "T2", "path": "a.cpp", "line": 3, "status": "fixed",
                  "finding": "**Moderate: Gone**"}]
        repeat = {"path": "a.cpp", "line": 2, "severity": "Moderate", "title": "Again",
                  "problem": "Same.", "fix": "Same."}
        new = dict(repeat, line=3, title="New")
        result = dict(RESULT, verdict="Needs more work", findings=[repeat, new])
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
        self.assertIn("· 1 problem (Moderate)", summary)
        self.assertNotIn("still open", summary)

    def test_worse_finding_kept(self):
        """A new finding more severe than the open one at its spot is kept and posted."""
        prior = [{"id": "T1", "path": "a.cpp", "line": 2, "finding": "**Moderate: Old**"}]
        worse = {"path": "a.cpp", "line": 2, "severity": "Serious", "title": "Crash",
                 "problem": "Worse.", "fix": "Fix."}
        result = dict(RESULT, verdict="Needs more work", findings=[worse])
        files = [{"filename": "a.cpp", "patch": "@@ -1,2 +1,3 @@\n x\n+y\n+z"}]
        with mock.patch.object(review, "summary_comment", return_value=None), \
                mock.patch.object(review, "changed_files", return_value=files), \
                mock.patch.object(review, "load_prior", return_value=prior), \
                mock.patch.object(review, "request") as request, \
                mock.patch.dict(os.environ, {"HEAD_SHA": "b" * 40, "FORK_PR": "7",
                                             "RESULT": json.dumps(result)}):
            review.cmd_post()
        posted = request.call_args_list[0].args[2]["comments"]
        self.assertEqual([c["line"] for c in posted], [2])
        summary = request.call_args_list[1].args[2]["body"]
        self.assertIn("2 problems (1 Serious, 1 Moderate)", summary)


class UsageTests(unittest.TestCase):
    """The token report printed from the execution file."""

    def test_usage_lines(self):
        """Each call shows its target and result size, never the result; turns count calls."""
        messages = [
            {"type": "assistant", "message": {"id": "m1", "usage": {
                "cache_creation_input_tokens": 900, "cache_read_input_tokens": 8000}, "content": [
                {"type": "tool_use", "id": "u1", "name": "Read",
                 "input": {"file_path": "a.cpp", "offset": 10, "limit": 50}}]}},
            {"type": "assistant", "message": {"id": "m1", "content": [
                {"type": "tool_use", "id": "u2", "name": "Grep", "input": {"pattern": "Foo"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "u1", "content": "secret body"},
                {"type": "tool_result", "tool_use_id": "u2", "content": [{"text": "x"}]}]}},
            {"type": "assistant", "message": {"id": "m2", "content": [
                {"type": "tool_use", "id": "u3", "name": "Glob", "input": {"pattern": "*.h"}}]}},
            {"type": "result", "usage": {"output_tokens": 5}, "num_turns": 2,
             "total_cost_usd": 0.1}]
        lines = review.usage_lines(messages)
        self.assertEqual(lines[0], "      11 chars  Read a.cpp 10+50")
        self.assertTrue(lines[1].endswith("chars  Grep Foo"))
        self.assertEqual(lines[2], "calls per turn 2 1")
        self.assertEqual(lines[3], "first turn cache wrote 900 read 8000")
        self.assertEqual(lines[4], 'tokens {"output_tokens": 5} turns 2 cost 0.1')
        self.assertNotIn("secret", "\n".join(lines))


class TrimTests(unittest.TestCase):
    """Inputs trimmed before the reviewer reads them."""

    def test_trimmed_body(self):
        """Template comments, unticked boxes and CodeRabbit notes go; text and ticked boxes stay."""
        body = ("Fixes a crash.\n<!--- describe it -->\n\n\n\n- [ ] **Clean up**\n"
                "- [x] **Bug fix**\n<!-- This is an auto-generated comment: release notes by "
                "coderabbit.ai -->\nSummary\n<!-- end of auto-generated comment: release notes by "
                "coderabbit.ai -->")
        self.assertEqual(review.trimmed_body(body), "Fixes a crash.\n\n- [x] **Bug fix**")

    def test_prior_finding_first_line(self):
        """An earlier finding keeps only its first line."""
        thread = {"id": "T1", "isResolved": False, "path": "a.cpp", "line": 2,
                  "comments": {"nodes": [{"author": {"login": "kodi-review", "__typename": "Bot"},
                                          "body": "**Serious: Breaks**\n\nLong detail."}]}}
        self.assertEqual(review.earlier_findings([thread])[0]["finding"], "**Serious: Breaks**")


class TriggerTests(unittest.TestCase):
    """Deciding whether a comment may start a run, and whether it asks for a review or answer."""

    def run_trigger(self, comment, access="write", asker="issue/5"):
        """Run the trigger on fake replies; store its mode, return the requests and exit code."""
        calls, refusals = [], []
        self.mode = ""

        def reply(method, path, body=None, raw=False):
            """Reply like GitHub for the comment and the commenter's access."""
            calls.append((method, path))
            if path.endswith("/permission"):
                if access is None:
                    refusals.append(urllib.error.HTTPError(path, 404, "Not Found", Message(), None))
                    raise refusals[-1]
                return {"permission": access}
            if method == "GET":
                return comment
            return None
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(review, "request", side_effect=reply), \
                mock.patch.dict(os.environ, {"COMMENT": asker, "FORK_PR": "7",
                                             "GITHUB_OUTPUT": str(Path(tmp, "out"))}):
            try:
                review.cmd_trigger()
            except SystemExit as stop:
                return calls, stop.code
            finally:
                for refusal in refusals:
                    refusal.close()
                out = Path(tmp, "out")
                self.mode = out.read_text() if out.exists() else ""
        return calls, None

    def comment(self, body="@kodi-review review", url="https://api.github.com/repos/o/r/issues/7"):
        """Return a comment by a person on pull request 7."""
        return {"body": body, "issue_url": url, "user": {"login": "dev"}}

    def test_writer_mention_starts_and_is_acknowledged(self):
        """A writer's mention on this pull request starts and is acknowledged with a reaction."""
        calls, code = self.run_trigger(self.comment())
        self.assertIsNone(code)
        self.assertEqual(calls[-1], ("POST", f"/repos/{review.REPO}/issues/comments/5/reactions"))
        self.assertEqual(self.mode, "mode=review\n")

    def test_anything_but_review_is_a_question(self):
        """Review or nothing after the mention asks for a review; other words ask a question."""
        for body, mode in (("@kodi-review: Review again", "review"),
                           ("@kodi-review reviewed this?", "answer"),
                           ("Hey @kodi-review, is the lock needed?", "answer"),
                           ("@kodi-review", "review"),
                           ("@kodi-review!", "review")):
            self.run_trigger(self.comment(body=body))
            self.assertEqual(self.mode, f"mode={mode}\n", body)

    def test_line_comment_is_read_from_pulls(self):
        """A line comment is read from the pull request comments and passes."""
        comment = {"body": "@KODI-REVIEW why?", "user": {"login": "dev"},
                   "pull_request_url": "https://api.github.com/repos/o/r/pulls/7"}
        calls, code = self.run_trigger(comment, asker="review/6")
        self.assertIsNone(code)
        self.assertEqual(calls[0], ("GET", f"/repos/{review.REPO}/pulls/comments/6"))

    def test_refused(self):
        """No mention, a quoted or code one, another PR, low access or a bad id are refused."""
        cases = [(self.comment(body="thanks @kodi-reviewer"), "write", "issue/5"),
                 (self.comment(body="> @kodi-review review\n\nAgreed."), "write", "issue/5"),
                 (self.comment(body="Type `@kodi-review review`."), "write", "issue/5"),
                 (self.comment(body="```\n@kodi-review\n```"), "write", "issue/5"),
                 (self.comment(url="https://api.github.com/repos/o/r/issues/17"), "write",
                  "issue/5"),
                 (self.comment(), "read", "issue/5"),
                 (self.comment(), None, "issue/5"),
                 (self.comment(), "write", "issue/5; rm")]
        for comment, access, asker in cases:
            calls, code = self.run_trigger(comment, access, asker)
            self.assertTrue(code, (access, asker))
            self.assertNotIn("POST", [method for method, _ in calls])


class AnswerTests(unittest.TestCase):
    """Preparing a question and posting its answer."""

    def setUp(self):
        """Run each test in an empty directory."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, cwd)

    def line_comment(self, cid, login, body, kind="User", reply_to=None):
        """Return a review comment shaped like the REST pulls comments."""
        return {"id": cid, "in_reply_to_id": reply_to, "body": body, "path": "a.cpp", "line": 4,
                "created_at": f"2026-10-07T10:00:{cid:02d}Z",
                "user": {"login": login, "type": kind}}

    def test_question_in_finding_thread(self):
        """A question in the bot's open finding thread names that finding and carries the thread."""
        finding = self.line_comment(1, "kodi-review[bot]", "**Minor: Leak**\n\nDetail.", "Bot")
        asked = self.line_comment(3, "dev", "@kodi-review is this intended?", reply_to=1)
        other = self.line_comment(2, "author", "It is freed later.", reply_to=1)
        with mock.patch.object(review, "request", return_value=asked), \
                mock.patch.object(review, "paged", return_value=[asked, finding, other]), \
                mock.patch.dict(os.environ, {"COMMENT": "review/3", "FORK_PR": "7"}):
            review.cmd_question()
        saved = json.loads(Path(".kodi-review-run/question.json").read_text())
        self.assertEqual(saved, {"kind": "review", "comment": 3, "finding": 1, "asker": "dev",
                                 "quote": "@kodi-review is this intended?"})
        text = Path(".kodi-review-run/question.md").read_text()
        self.assertIn("Its first comment is your finding", text)
        self.assertLess(text.index("**Minor: Leak**"), text.index("It is freed later."))

    def test_question_in_conversation_has_no_finding(self):
        """A question in the main conversation can settle nothing."""
        asked = {"id": 9, "body": "@kodi-review summarize it", "user": {"login": "dev"}}
        with mock.patch.object(review, "request", return_value=asked), \
                mock.patch.dict(os.environ, {"COMMENT": "issue/9", "FORK_PR": "7"}):
            review.cmd_question()
        self.assertIsNone(json.loads(Path(".kodi-review-run/question.json").read_text())["finding"])
        self.assertIn("leave withdraw and accept empty",
                      Path(".kodi-review-run/question.md").read_text())

    def run_reply(self, asked, result):
        """Run the reply command for a saved question; return the requests and exit code."""
        Path(".kodi-review-run").mkdir()
        Path(".kodi-review-run/question.json").write_text(json.dumps(asked))
        code = None
        with mock.patch.object(review, "request",
                               return_value={"body": "**Minor: Leak**"}) as request, \
                mock.patch.dict(os.environ, {"FORK_PR": "7", "RESULT": json.dumps(result)}):
            try:
                review.cmd_reply()
            except SystemExit as stop:
                code = stop.code
        return [c.args for c in request.call_args_list], code

    def test_thread_reply_and_withdraw(self):
        """A conceded finding gets the answer in its thread and the withdrawn note on top."""
        asked = {"kind": "review", "comment": 3, "finding": 1, "quote": "q"}
        calls, code = self.run_reply(asked, {"reply": "Agreed, @dev frees it.",
                                             "withdraw": "Freed later."})
        repo = review.REPO
        self.assertIsNone(code)
        self.assertEqual(calls[0], ("POST", f"/repos/{repo}/pulls/7/comments/3/replies",
                                    {"body": "Agreed, @\u200bdev frees it."}))
        self.assertEqual(calls[-1], ("PATCH", f"/repos/{repo}/pulls/comments/1",
                                     {"body": "**Withdrawn:** Freed later.\n\n**Minor: Leak**"}))

    def test_accepted_finding_names_the_asker(self):
        """An accepted finding keeps its text under a note naming who accepted it."""
        asked = {"kind": "review", "comment": 3, "finding": 1, "asker": "dev", "quote": "q"}
        calls, _ = self.run_reply(asked, {"reply": "Recorded. The test still fails locally.",
                                          "withdraw": "", "accept": "Merging with the failure."})
        self.assertEqual(calls[-1], ("PATCH", f"/repos/{review.REPO}/pulls/comments/1",
                                     {"body": "**Accepted by dev:** Merging with the failure."
                                              "\n\n**Minor: Leak**"}))

    def test_conversation_reply_quotes_the_question(self):
        """An answer in the main conversation quotes the question, and withdraws nothing."""
        calls, _ = self.run_reply({"kind": "issue", "comment": 9, "finding": None,
                                   "quote": "@kodi-review summarize it"},
                                  {"reply": "The lock is the problem.", "withdraw": "Wrong."})
        self.assertEqual(calls, [("POST", f"/repos/{review.REPO}/issues/7/comments",
                                  {"body": "> @\u200bkodi-review summarize it\n\n"
                                           "The lock is the problem."})])

    def test_failed_or_leaking_answer(self):
        """No answer says so; an answer carrying a credential is withheld and fails the step."""
        asked = {"kind": "issue", "comment": 9, "finding": None, "quote": ""}
        calls, code = self.run_reply(asked, None)
        self.assertEqual(calls[0][2]["body"], review.NO_ANSWER)
        self.assertIsNone(code)
        Path(".kodi-review-run/question.json").unlink()
        Path(".kodi-review-run").rmdir()
        calls, code = self.run_reply(asked, {"reply": "ghp_" + "a" * 36, "withdraw": ""})
        self.assertEqual(calls[0][2]["body"], review.WITHHELD)
        self.assertEqual(code, 1)


class PreloadTests(unittest.TestCase):
    """Small prepared files carried in the prompt."""

    def test_small_files_load_and_large_ones_wait(self):
        """Small files load between this run's markers; a diff over the limit is left to read."""
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp, ".kodi-review-run")
            run.mkdir()
            (run / "pr.json").write_text('{"title": "Fix"}\n')
            (run / "discussion.md").write_text("\n")
            (run / "full.diff").write_text("x" * (review.PRELOAD_LIMIT + 1))
            with mock.patch.object(review, "RUN_DIR", run), \
                    mock.patch("builtins.print") as printed:
                review.cmd_preload()
        text = printed.call_args.args[0]
        mark = text.split("<<<pr.json ", 1)[1].split(">>>", 1)[0]
        self.assertIn(f'<<<pr.json {mark}>>>\n{{"title": "Fix"}}\n<<<end {mark}>>>', text)
        self.assertNotIn("discussion.md " + mark, text)
        self.assertNotIn("full.diff " + mark, text)

    def test_nothing_to_load(self):
        """Without prepared files nothing is printed."""
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(review, "RUN_DIR", Path(tmp)), \
                mock.patch("builtins.print") as printed:
            review.cmd_preload()
        printed.assert_not_called()


class RenderCommandTests(unittest.TestCase):
    """Rendering a review without posting it."""

    def render(self, status, result):
        """Run the render command on the given files and return what it printed."""
        with mock.patch.object(sys, "argv", ["review.py", "render", status, result]), \
                mock.patch("builtins.print") as printed:
            review.cmd_render()
        return printed.call_args.args[0]

    def test_unfinished_review(self):
        """A missing or empty result renders as an unfinished review."""
        self.assertEqual(self.render("/nonexistent/status.json", "/nonexistent/result.json"),
                         "The review did not finish.")
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp, "result.json")
            empty.write_text("")
            self.assertEqual(self.render(str(Path(tmp, "status.json")), str(empty)),
                             "The review did not finish.")

    def test_without_status(self):
        """A blind replay has no status file and still renders its findings."""
        with tempfile.TemporaryDirectory() as tmp:
            result = Path(tmp, "result.json")
            finding = {"path": "a.cpp", "line": 2, "severity": "Minor", "title": "Typo",
                       "problem": "Misspelled.", "fix": "Spell it."}
            result.write_text(json.dumps(dict(RESULT, verdict="Needs more work",
                                              findings=[finding])))
            text = self.render(str(Path(tmp, "status.json")), str(result))
        self.assertTrue(text.startswith("### Needs more work · 1 problem (Minor)"))
        self.assertIn("**Minor: Typo**", text)


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
        for text in ("sk-ant-api03-x", "gho_" + "A1" * 10, "ghs_1234567_eyJhbGciOiJSUzI1NiJ9.e30.x",
                     "github_pat_11ABC"):
            self.assertRegex(text, review.CREDENTIAL)
        for text in ("ghp_short", "sk-other", "github pat"):
            self.assertNotRegex(text, review.CREDENTIAL)


if __name__ == "__main__":
    unittest.main()
