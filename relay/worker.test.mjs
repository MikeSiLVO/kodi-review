import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import test from "node:test";
import worker, { followUp, trigger, verify } from "./worker.mjs";

const REPOS = ["xbmc/xbmc", "MikeSiLVO/xbmc"];
const ENV = { WEBHOOK_SECRET: "s3cret", DISPATCH_TOKEN: "t", BOT_REPO: "MikeSiLVO/kodi-review",
  ALLOWED_REPOS: REPOS.join(" ") };

/** Return an issue_comment payload for a comment on pull request 7. */
function issueComment(changes = {}) {
  return { action: "created", repository: { full_name: "xbmc/xbmc" },
    issue: { number: 7, state: "open", pull_request: {} },
    comment: { id: 99, body: "@kodi-review review", author_association: "MEMBER",
      user: { type: "User" } }, ...changes };
}

/** Sign the body as GitHub does for the x-hub-signature-256 header. */
function sign(body, secret = ENV.WEBHOOK_SECRET) {
  return `sha256=${createHmac("sha256", secret).update(body).digest("hex")}`;
}

test("a mention on an open pull request asks for a review", () => {
  assert.deepEqual(trigger("issue_comment", issueComment(), REPOS),
    { pr: "7", repo: "xbmc/xbmc", comment: "issue/99", mode: "review", source: "mention" });
});

test("review or nothing after the mention asks for a review; other words ask a question", () => {
  const base = issueComment();
  const modes = [["@kodi-review", "review"], ["@kodi-review, Review again", "review"],
    ["@kodi-review reviewing this, why?", "answer"], ["Hey @kodi-review is it safe?", "answer"],
    ["> @kodi-review review\n\n@kodi-review why?", "answer"]];
  for (const [body, mode] of modes) {
    const payload = issueComment({ comment: { ...base.comment, body } });
    assert.equal(trigger("issue_comment", payload, REPOS).mode, mode, body);
  }
});

test("a mention in a line comment names the review comment", () => {
  const payload = { action: "created", repository: { full_name: "xbmc/xbmc" },
    pull_request: { number: 8, state: "open" },
    comment: { id: 5, body: "@kodi-review why?", author_association: "MEMBER",
      user: { type: "User" } } };
  assert.deepEqual(trigger("pull_request_review_comment", payload, REPOS),
    { pr: "8", repo: "xbmc/xbmc", comment: "review/5", mode: "answer", source: "mention" });
});

test("everything else is ignored", () => {
  const base = issueComment();
  const ignored = [
    issueComment({ action: "edited" }),
    issueComment({ repository: { full_name: "someone/xbmc" } }),
    issueComment({ issue: { ...base.issue, pull_request: undefined } }),
    issueComment({ issue: { ...base.issue, state: "closed" } }),
    issueComment({ comment: { ...base.comment, body: "thanks @kodi-reviewer" } }),
    issueComment({ comment: { ...base.comment, body: "ask @kodi-review-bot" } }),
    issueComment({ comment: { ...base.comment, body: "> @kodi-review review\n\nThanks" } }),
    issueComment({ comment: { ...base.comment, body: "Type `@kodi-review review` to ask" } }),
    issueComment({ comment: { ...base.comment, body: "```\n@kodi-review\n```" } }),
    issueComment({ comment: { ...base.comment, body: "<!-- @kodi-review -->" } }),
    issueComment({ comment: { ...base.comment, author_association: "NONE" } }),
    issueComment({ comment: { ...base.comment, user: { type: "Bot" } } }),
  ];
  for (const payload of ignored) assert.equal(trigger("issue_comment", payload, REPOS), null);
});

test("only GitHub's signature with the shared secret passes", async () => {
  const body = JSON.stringify(issueComment());
  assert.equal(await verify("s3cret", body, sign(body)), true);
  assert.equal(await verify("s3cret", body, sign(body, "other")), false);
  assert.equal(await verify("s3cret", body + " ", sign(body)), false);
  assert.equal(await verify("s3cret", body, "sha256=xyz"), false);
  assert.equal(await verify("", body, sign(body, "")), false);
});

test("a signed mention dispatches the review workflow", async (t) => {
  const calls = [];
  t.mock.method(globalThis, "fetch", async (url, init) => {
    calls.push({ url, init });
    return new Response(null, { status: 204 });
  });
  const body = JSON.stringify(issueComment());
  const request = new Request("https://relay.example/github", { method: "POST", body,
    headers: { "x-github-event": "issue_comment", "x-hub-signature-256": sign(body) } });
  const reply = await worker.fetch(request, ENV);
  assert.equal(reply.status, 202);
  assert.equal(calls.length, 1);
  assert.match(calls[0].url, /MikeSiLVO\/kodi-review\/actions\/workflows\/review\.yml/);
  assert.deepEqual(JSON.parse(calls[0].init.body),
    { ref: "main", inputs: { pr: "7", repo: "xbmc/xbmc", comment: "issue/99", mode: "review",
      source: "mention" } });
});

test("an unsigned request is refused without dispatching", async (t) => {
  const fetched = t.mock.method(globalThis, "fetch", async () => new Response(null));
  const request = new Request("https://relay.example/github", { method: "POST",
    body: "{}", headers: { "x-github-event": "issue_comment" } });
  assert.equal((await worker.fetch(request, ENV)).status, 401);
  assert.equal(fetched.mock.callCount(), 0);
});

/** Return a summary comment by the bot under the given heading. */
function summary(head) {
  return { user: { login: "kodi-review[bot]" },
    body: `<!-- kodi-review sha=${"a".repeat(40)} -->\n### ${head}\nWhy.` };
}

/** Return a lookup that answers each API path from a table, null for the rest. */
function lookup(table) {
  return async (path) => table[path] ?? null;
}

/** Return a reply to review comment 4 on pull request 8 by a member. */
function threadReply(changes = {}) {
  return { action: "created", repository: { full_name: "xbmc/xbmc" },
    pull_request: { number: 8, state: "open" },
    comment: { id: 6, in_reply_to_id: 4, body: "Agreed, thanks.", author_association: "MEMBER",
      user: { type: "User" } }, ...changes };
}

test("a push after a summary with open problems asks for a recheck", async () => {
  const push = { action: "synchronize", repository: { full_name: "xbmc/xbmc" },
    pull_request: { number: 8, state: "open" } };
  const path = "/repos/xbmc/xbmc/issues/8/comments?per_page=100&page=1";
  const open = lookup({ [path]: [summary("Ready to merge"),
    summary("Merge after small fixes · 1 problem (Moderate)")] });
  assert.deepEqual(await followUp("pull_request", push, REPOS, open),
    { pr: "8", repo: "xbmc/xbmc", comment: "", mode: "review", source: "push" });
  const clean = lookup({ [path]: [summary("Merge after small fixes · 1 problem (Moderate)"),
    summary("Ready to merge")] });
  assert.equal(await followUp("pull_request", push, REPOS, clean), null);
  assert.equal(await followUp("pull_request", push, REPOS, lookup({ [path]: [] })), null);
  assert.equal(await followUp("pull_request", { ...push, action: "opened" }, REPOS, open), null);
});

test("a member's reply in an open finding's thread asks for an answer", async () => {
  const finding = { user: { login: "kodi-review[bot]" }, body: "**Moderate: Leak**\n\nDetail." };
  const get = lookup({ "/repos/xbmc/xbmc/pulls/comments/4": finding });
  assert.deepEqual(await followUp("pull_request_review_comment", threadReply(), REPOS, get),
    { pr: "8", repo: "xbmc/xbmc", comment: "review/6", mode: "answer", source: "reply" });
});

test("replies elsewhere, by strangers or bots, or to settled findings are ignored", async () => {
  const base = threadReply();
  const finding = { user: { login: "kodi-review[bot]" }, body: "**Minor: Typo**" };
  const get = lookup({ "/repos/xbmc/xbmc/pulls/comments/4": finding });
  const ignored = [threadReply({ comment: { ...base.comment, in_reply_to_id: undefined } }),
    threadReply({ comment: { ...base.comment, author_association: "NONE" } }),
    threadReply({ comment: { ...base.comment, user: { type: "Bot" } } }),
    threadReply({ action: "edited" }),
    threadReply({ pull_request: { number: 8, state: "closed" } })];
  for (const payload of ignored) {
    assert.equal(await followUp("pull_request_review_comment", payload, REPOS, get), null);
  }
  for (const parent of [{ user: { login: "coderabbitai[bot]" }, body: "**Minor: x**" },
    { user: { login: "kodi-review[bot]" }, body: "**Fixed in `abc`.**\n\n**Minor: x**" }]) {
    const other = lookup({ "/repos/xbmc/xbmc/pulls/comments/4": parent });
    assert.equal(await followUp("pull_request_review_comment", base, REPOS, other), null);
  }
});
