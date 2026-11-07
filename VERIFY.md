# Verifier

This replaces the task in the review instructions that follow. Two passes already ran on this
pull request: the review, whose findings are in `.kodi-review-run/result.json`, and an input pass,
whose proposed problems are in `.kodi-review-run/candidates.json`. Both are another model's
claims, not facts. Your job is to decide, with a fresh look at the code, which candidates are real
problems the review does not already report. The review instructions still describe the prepared
files, the facts about Kodi, what to report, the Backports rules and the standard of proof; the
verdict and the rest of the result do not apply to you.

For each candidate:

1. Drop it when a review finding or a `prior.json` entry already reports the same root cause,
   unless the entry is `fixed` and `new.diff` brings the problem back; or when `discussion.md`
   answers it and the code confirms the answer.
2. Try to show it is wrong. Read the code along its `chain` at head, and the removed lines in
   `full.diff` for what the merge base did. Check that the input reaches the changed code through
   a real caller, user action or data source; that the result is what the candidate says; that
   the base gave a different result or the change made the path newly reachable; and that neither
   the description nor a code comment says that result is wanted for that input.
3. Keep it only if it survives, and only if the review's "What to report", "Never report",
   "Prove each one" and "Backports" rules allow it.

Merge candidates that share a root cause into one finding. Write each kept one in the review's
finding format, with your own checked `problem` and `fix` rather than the candidate's. Return
`findings`; an empty list is right when none survive.

