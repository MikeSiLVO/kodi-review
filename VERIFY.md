# Verifier

This replaces the task in the review instructions that follow. Two passes already ran on this
pull request: the review, whose findings are in `.kodi-review-run/result.json`, and an input pass,
whose proposed problems are in `.kodi-review-run/candidates.json`. Both are another model's
claims, not facts. Your job is to decide, with a fresh look at the code, which candidates are real
problems the review does not already report. The review instructions still describe the prepared
files, the facts about Kodi, what to report, the Backports rules and the standard of proof; the
verdict and the rest of the result do not apply to you.

For each candidate, read the code along its `chain` at head, and the removed lines in `full.diff`
for what the merge base did. Then keep it or drop it. Drop it only for one of these reasons, with
the evidence named:

- `duplicate`: a review finding or a `prior.json` entry already reports the same root cause
  (unless the entry is `fixed` and `new.diff` brings the problem back), or `discussion.md` answers
  it and the code confirms the answer. Evidence: that finding, entry or answer.
- `contradicted`: a line of code shows a step of the chain or the claimed result is wrong.
  Evidence: the file:line and what it shows.
- `unproven`: a step of the chain cannot be shown from the code, such as behavior inferred from a
  name with no definition in the tree or `external` fact, or a thread claim without the two
  threads and the order of events. Evidence: the step and where the search ended.
- `wanted`: the description or a code comment names this input and says this result is wanted
  for it. Evidence: the quoted words that name the input. Never for a crash, data loss, security
  hole or build break.
- `unreachable`: no real caller, user action, setting or data source reaches the input.
  Evidence: where the search for callers ended.
- `not new`: the merge base gives the same result and the change does not make the path newly
  reachable. Evidence: the base lines.
- `rule`: a "Never report" item or the Backports rules exclude it. Evidence: the rule.

Keep every candidate whose chain holds in your own reading and that none of these reasons drops.
Merge kept candidates that share a root cause into one finding. Write each in the review's finding
format, with your own checked `problem` and `fix` rather than the candidate's. Return `findings`
(empty when none survive) and `dropped`, one entry per dropped candidate with its `title` as
`candidate`, the `reason` (one of the labels above) and the `evidence`.

