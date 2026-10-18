# kodi-review

A pull request reviewer for Kodi ([xbmc/xbmc](https://github.com/xbmc/xbmc)), run with Claude
Code on GitHub Actions. It reports only problems that should block a merge, proves each one from
the code, and adds a status block.

Reviews are posted by the [kodi-review](https://github.com/apps/kodi-review) GitHub App. To use it
on a repo, install the App there with its two permissions: Pull requests (read and write) and
Issues (read and write, for skin checks on issues). Mentions only work in xbmc/xbmc,
xbmc/repo-skins and my test forks of both; repo-skins takes only the skin check.

## How a review runs

1. Someone with write access to the repo comments `@kodi-review` alone on an open pull request
   (`@kodi-review review` works too). The comment gets a 👀 reaction and the review starts. A
   mention inside code, a quote or an HTML comment does not count, and a command counts only
   when the mention opens the comment.

   `review.yml` can also be dispatched by hand with a pull request number. Optional inputs: the
   repository (xbmc/xbmc by default), the effort level (xhigh by default), a dry run that writes
   the review to the run's summary page instead of posting it, and a commit for a blind replay,
   which reviews the pull request as it stood then, without its discussion or status, and also
   posts nothing.
2. Each finding is posted as a line comment, and the summary is posted at the end of the
   conversation, hiding the earlier ones as outdated. A later run reviews only what changed since
   the last review, even after a force push, marks the findings the code has fixed, and does not
   repeat ones still open.
3. While a review has open findings, each push to the pull request starts a re-review on its
   own, unless five reviews ran in the last day. When someone replied under a finding that is now
   fixed, the bot says so in that thread, since it cannot resolve threads.
4. Each commit's changed lines are checked against the base branch's `.clang-format`, as the
   Jenkins build does, but with a newer clang-format. The summary says whether they match and
   folds any patch under the status block.

## Asking questions

Any other comment that mentions `@kodi-review` is a question about the pull request. The answer
goes in the same thread, or quotes the question in the main conversation. A comment that asks
nothing, such as thanks, gets a 👍 instead. In a finding's thread, a reply showing the finding is
wrong gets agreement and marks the finding withdrawn. Questions about anything else, or requests
to approve, merge or label, get a one-line reply saying so.

Under a finding, a team member's reply needs no mention. A question or dispute gets an answer,
and a reply that only agrees or says a fix is coming gets a 👍.

## Help

`@kodi-review help` alone posts the list of commands from `HELP.md`, in the same thread or the main
conversation. Only people with write access can ask.

## Formatting only

`@kodi-review format`, `clang` or `clang-format`, alone after the mention, runs only the
formatting check and posts it as its own comment, hiding the last one. Anyone can ask, since no
model runs. A head that already has its check is not checked again. Dispatched by hand with
`mode` set to `format` and a dry run, it writes the check to the run's summary page instead.

## Skin check

`@kodi-review skin` alone on a pull request checks the skins it changes with
[kdk](https://github.com/MikeSiLVO/kdk), or Estuary when it changes none, against the Kodi
release of its base branch. On a repo-skins submission issue, `@kodi-review skin <link>` checks
the skin at the link: a GitHub repo at a commit, branch or tag, or a zip. A revision may follow a
bare repo link. The comment lists the problems that are new since the base, or for a new skin its
errors and counts per check, and links the run's summary page, which lists every finding. Anyone
can ask, since no model runs, once per head or link. Omega and Piers skins are checked; master
counts as Piers while their xbmc.gui versions match.

## Files

| File | What it is |
| --- | --- |
| `REVIEW.md` | The reviewer's instructions |
| `schema.json` | The result Claude returns |
| `ANSWER.md` | The instructions for answering a question, read before `REVIEW.md` |
| `answer.json` | The answer Claude returns |
| `HELP.md` | The list of commands posted for `@kodi-review help` |
| `review.py` | Prepares the inputs, posts the review, prints each run's token use |
| `test_review.py` | Offline tests: `python3 -m unittest test_review` |
| `skin.py` | Fetches the skin, runs kdk in its sandbox, posts the skin check |
| `test_skin.py` | Offline tests: `python3 -m unittest test_skin` |
| `relay/worker.mjs` | The Cloudflare Worker that turns a mention into a review run |
| `relay/wrangler.toml` | The Worker's address and settings |
| `relay/worker.test.mjs` | Worker tests: `node --test relay/worker.test.mjs` |
| `vendor/` | bubblewrap and socat for Ubuntu 24.04, checked against `SHA256SUMS` before install, and the hash-pinned clang-format wheels |

## Security

The reviewer reads code and text written by strangers, so it gets as little as possible to misuse.

- Read, Grep and Glob only, limited to the workspace. No shell, web or edit tools.
- `/proc`, `/sys`, `~/.claude` and `.git` are denied, which keeps the Claude token and the GitHub
  token out of reach.
- `CLAUDE.md`, `AGENTS.md`, `.claude` and `.mcp.json` are removed from the checkout, so a pull
  request cannot give the reviewer its own instructions, hooks or tools.
- Output that looks like a credential is withheld. Mentions and pull request references are
  unlinked before posting.
- Only people with write access to the reviewed repo can start a review or an answer by
  mentioning the bot or replying under a finding, and only people with write access to this repo
  can dispatch one by hand. A push starts a re-review only after such a review left open findings.
  A formatting request runs no model, so anyone may make it, once per head. None of it works
  outside the repos the relay lists.
- A skin check reads strangers' files, so the job that fetches and checks them holds no App token;
  a separate job posts the result. A download must be https to a public address, under 200 MB,
  and unpack to under 600 MB and 30,000 files with no links or paths outside its folder.
- kdk runs in bubblewrap with no network, no environment, a read-only skin and one writable
  folder for its report, and ignores config files in the skin. If the sandbox cannot start,
  nothing is checked. Text from the skin is posted inside code spans.
