# Input pass

This replaces the task in the review instructions that follow. A separate review of this pull
request runs on its own; you do not write it. Your one job is to find inputs that make the
changed code behave wrongly, by listing them and tracing each one. The review instructions still
describe the prepared files, the facts about Kodi and the standard of proof for each step of a
trace, except that a verifier, not you, settles whether a real caller reaches the input. Their
"What to report" section, the verdict and the rest of the result do not apply to you.

## List the units

From `full.diff` and `pr.json`, write down every unit below. On a re-review, take units only from
the changes in `new.diff` and use `full.diff` for context; when `new.diff` is empty, return no
candidates. For a backport with `original.diff`, take units only from the hunks that differ from
that change, in the files `backport.differs` in `status.json` lists, and from what it relies on
that Piers lacks.

- condition the change adds or alters: an if, a loop bound, an early return, a match, filter or
  regex rule, a comparison;
- if/else-if chain, switch or first-match list it edits;
- behavior the title, description or commit messages promise;
- piece of state it writes that existing code reads (`touches.txt`), and new trigger it adds for
  existing handling (a reset, cleanup, error or fallback path, a handled message);
- `check:` line in `knowledge.md` whose `at:` names a file or function the change touches.

## List the inputs

For each unit, write down concrete inputs from every class that applies before you trace any:

- the case the change targets;
- just outside it: one more, one less, the boundary itself;
- a look-alike that matches the condition but is not the target;
- the same value on its other paths: other callers and entry points (JSON-RPC, Python, skins,
  UPnP, add-ons);
- another mode, setting, layout, platform, caller or item type, including ones the description
  never mentions;
- a mode or setting switched while the new state is live;
- data that is empty, does not divide evenly, or holds a sentinel (zero, -1, none, not found);
- what an earlier run, a failed attempt or another path left behind;
- for a knowledge `check:` line, the input it names.

Do not stop at the first input that comes out right; a unit is done when every class that applies
has been traced.

## Trace

Trace each input through the changed code to what the user or caller sees, and through the merge
base (the removed lines in `full.diff`) to what it saw before. Read the code each trace passes
through. Record each input whose new result is wrong, or
differs from the base where neither the description nor a code comment says the new result is
wanted for that input.

## Output

Return `candidates`, one per root cause:

- `path`, `line`, `severity`, `title`, `problem`, `fix`: as the review's finding rules define them.
- `unit`: the unit from the list above that it tests, in one line.
- `input`: the concrete input, or each one when several share the root cause.
- `base`: what the merge base does with it.
- `new`: what this change does with it.
- `chain`: the call path the trace followed, with file:line.

Report behavior only: no style, comments, docs, spelling or text that disagrees with the code;
the review covers those. Report a candidate whose trace you finished even when you are unsure a
real caller reaches it. Leave out any trace you did not finish. An
empty list is right when no input breaks the change.

