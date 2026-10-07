# kodi-review

A pull request reviewer for Kodi ([xbmc/xbmc](https://github.com/xbmc/xbmc)), run with Claude
Code on GitHub Actions. It reports only problems that should block a merge, proves each one from
the code, and adds a status block.

Reviews run from my xbmc fork for now and post to mirror pull requests there. This repo becomes the
bot's home once a GitHub App can post its reviews on xbmc/xbmc.

## How a review runs

1. Dispatch `review.yml` with a pull request number. Optional inputs: the repository (xbmc/xbmc by
   default), the effort level (high by default), a dry run that writes the review to the run's
   summary page instead of posting it, and a commit for a blind replay, which reviews the pull
   request as it stood then, without its discussion or status, and also posts nothing.
2. Each finding is posted as a line comment, and one summary comment is kept up to date. A later
   run reviews only new commits, resolves findings the code has fixed, and does not repeat ones
   still open.

## Files

| File | What it is |
| --- | --- |
| `REVIEW.md` | The reviewer's instructions |
| `schema.json` | The result Claude returns |
| `review.py` | Prepares the inputs, posts the review, prints each run's token use |
| `test_review.py` | Offline tests: `python3 -m unittest test_review` |

## Security

The reviewer reads code and text written by strangers, so it gets as little as possible to misuse.

- Read, Grep and Glob only, limited to the workspace. No shell, web or edit tools.
- `/proc`, `/sys`, `~/.claude` and `.git` are denied, which keeps the Claude token and the GitHub
  token out of reach.
- `CLAUDE.md`, `AGENTS.md`, `.claude` and `.mcp.json` are removed from the checkout, so a pull
  request cannot give the reviewer its own instructions, hooks or tools.
- Output that looks like a credential is withheld. Mentions and pull request references are
  unlinked before posting.
- Only people with write access to this repo can start a review.
