"""Offline tests for the review helpers: patch comparison, label rules, Where it stands block."""

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
    """Return the Where it stands lines of the rendered summary."""
    text = review.summary_text(RESULT, status, "f" * 40, 0, [], False)
    return text.split("**Where it stands**\n")[1].split("\n\n")[0].splitlines()


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
    """The Where it stands block."""

    def test_every_line(self):
        """Each fact gets its line, in order, before the reviewed line."""
        status = {"base": "master", "conflicts": True, "label_problems": ["no milestone"],
                  "reviews": [{"login": "a", "state": "APPROVED", "team": True},
                              {"login": "b", "state": "CHANGES_REQUESTED", "team": False,
                               "author_pushed_since": True, "author_commented_since": False}],
                  "builds": {"count": 2, "failing": ["Jenkins"], "pending": []},
                  "backport": {"backports": []}, "unavailable": []}
        self.assertEqual(block(status), [
            "Reviews: approved by a (team); changes requested by b, author pushed since",
            "Test builds: failing (Jenkins)",
            "Conflicts: yes, needs a rebase onto master",
            "Labels: no milestone",
            "Kodi 22: **Yes, for 22.0**. Piers has the same bug. Backport: none yet.",
            "Next: A team member merges it."])
        text = review.summary_text(RESULT, status, "f" * 40, 0, [], False)
        self.assertTrue(text.endswith("\n\n<sub>Reviewed up to ffffffffffff.</sub>"))

    def test_quiet_lines_skipped(self):
        """Reviews, conflicts and labels with nothing to say have their lines skipped."""
        status = {"base": "master", "conflicts": False, "label_problems": [], "reviews": [],
                  "builds": {"count": 1, "failing": [], "pending": []}, "backport": None}
        self.assertEqual(block(status), [
            "Test builds: passing",
            "Kodi 22: **Yes, for 22.0**. Piers has the same bug.",
            "Next: A team member merges it."])

    def test_unanswered_change_request(self):
        """A change request with no push or comment after it says so."""
        status = {"reviews": [{"login": "b", "state": "CHANGES_REQUESTED", "team": True,
                               "author_pushed_since": False, "author_commented_since": False}]}
        self.assertEqual(block(status)[0],
                         "Reviews: changes requested by b (team), no answer yet")

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
            "Kodi 22: **Yes, for 22.0**. Piers has the same bug.",
            "Next: A team member merges it.",
            "Unavailable: pull request"])


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


if __name__ == "__main__":
    unittest.main()
