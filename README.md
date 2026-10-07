# kodi-review

A pull request reviewer for Kodi ([xbmc/xbmc](https://github.com/xbmc/xbmc)), run with Claude
Code on GitHub Actions. It reports only problems that should block a merge, proves each one from
the code, and adds a status block.

Reviews are posted by the [kodi-review](https://github.com/apps/kodi-review) GitHub App. To use it
on a repo, install the App there with its two permissions: Pull requests (read and write) and
Issues (read). Mentions only work in xbmc/xbmc and my test fork.

## How a review runs

1. Someone with write access to the repo comments `@kodi-review review`, or the mention alone, on
   an open pull request. The comment gets a 👀 reaction and the review starts. A mention inside
   code, a quote or an HTML comment does not count.

   `review.yml` can also be dispatched by hand with a pull request number. Optional inputs: the
   repository (xbmc/xbmc by default), the effort level (high by default), a dry run that writes
   the review to the run's summary page instead of posting it, and a commit for a blind replay,
   which reviews the pull request as it stood then, without its discussion or status, and also
   posts nothing.
2. Each finding is posted as a line comment, and one summary comment is kept up to date. A later
   run reviews only new commits, marks the findings the code has fixed, and does not repeat ones
   still open.

## Asking questions

Any other words after `@kodi-review` are a question about the pull request. The answer goes in the
same thread, or quotes the question in the main conversation. In a finding's thread, a reply
showing the finding is wrong gets agreement and marks the finding withdrawn. Questions about
anything else, or requests to approve, merge or label, get a one-line reply saying so.

## Files

| File | What it is |
| --- | --- |
| `REVIEW.md` | The reviewer's instructions |
| `schema.json` | The result Claude returns |
| `ANSWER.md` | The instructions for answering a question, read before `REVIEW.md` |
| `answer.json` | The answer Claude returns |
| `review.py` | Prepares the inputs, posts the review, prints each run's token use |
| `test_review.py` | Offline tests: `python3 -m unittest test_review` |
| `relay/worker.mjs` | The Cloudflare Worker that turns a mention into a review run |
| `relay/wrangler.toml` | The Worker's address and settings |
| `relay/worker.test.mjs` | Worker tests: `node --test relay/worker.test.mjs` |
| `vendor/` | bubblewrap and socat for Ubuntu 24.04, checked against `SHA256SUMS` before install |

## Security

The reviewer reads code and text written by strangers, so it gets as little as possible to misuse.

- Read, Grep and Glob only, limited to the workspace. No shell, web or edit tools.
- `/proc`, `/sys`, `~/.claude` and `.git` are denied, which keeps the Claude token and the GitHub
  token out of reach.
- `CLAUDE.md`, `AGENTS.md`, `.claude` and `.mcp.json` are removed from the checkout, so a pull
  request cannot give the reviewer its own instructions, hooks or tools.
- Output that looks like a credential is withheld. Mentions and pull request references are
  unlinked before posting.
- Only people with write access to the reviewed repo can start a review by mentioning it, and
  only people with write access to this repo can dispatch one by hand. A mention starts a review
  only in the repos the relay lists.
