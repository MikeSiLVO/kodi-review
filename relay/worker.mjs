const MENTION = /(?<![\w-])@kodi-review(?![\w-])/i;
const REVIEW_ASK = /^[\s,:.!]*(?:review\b|$)/i;
const FORMAT_ASK = /^[\s,:.!]*(?:clang-format|clang|format)[\s,:.!]*$/i;
const HELP_ASK = /^[\s,:.!]*help[\s,:.!?]*$/i;
const SKIN_ASK = /^[\s,:.!]*skin\b/i;
const AGAIN = /\bagain\b/i;
const QUOTED = /```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`|<!--[\s\S]*?-->|^[ \t]*>[^\n]*/gm;

/** Return a comment's own words, without code, quoted lines or HTML comments. */
export function spoken(body) {
  return body.replace(QUOTED, " ");
}
const STRANGERS = new Set(["NONE", "FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR", "MANNEQUIN"]);
const OPEN_MODES = new Set(["format", "skin", "help"]);
const SKIN_REPO_MODES = new Set(["skin", "help"]);
const SKIN_ACTIONS = new Set(["opened", "reopened", "synchronize"]);
const BOT = "kodi-review[bot]";
const FINDING = /^\*\*(Serious|Moderate|Minor)\b/;
const SUMMARY = /^<!-- kodi-review sha=/;
const SKIN_CHECK = /^<!-- kodi-review skin /;
const OPEN_PROBLEMS = /^### [^\n]* · \d+ problems?\b/m;

/** Verify GitHub's HMAC signature of the raw request body. */
export async function verify(secret, body, signature) {
  const hex = /^sha256=([0-9a-f]{64})$/.exec(signature || "");
  if (!secret || !hex) return false;
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey("raw", encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["verify"]);
  const digest = Uint8Array.from(hex[1].match(/../g), (pair) => parseInt(pair, 16));
  return crypto.subtle.verify("HMAC", key, digest, encoder.encode(body));
}

/** Return a new mention's inputs, or null; mode refuse when repo, page or asker rules it out. */
export function trigger(event, payload, allowed, skinRepos = []) {
  if (payload.action !== "created" || !allowed.includes(payload.repository?.full_name)) return null;
  const comment = payload.comment;
  const words = spoken(comment?.body || "");
  if (!comment || comment.user?.type === "Bot" || !MENTION.test(words)) return null;
  const onIssue = event === "issue_comment" && !payload.issue?.pull_request;
  const pr = event === "issue_comment" ? payload.issue
    : event === "pull_request_review_comment" ? payload.pull_request : null;
  if (!pr) return null;
  const kind = event === "issue_comment" ? "issue" : "review";
  const at = MENTION.exec(words).index;
  const after = words.slice(at + "@kodi-review".length);
  const mode = words.slice(0, at).trim() ? "answer" : FORMAT_ASK.test(after) ? "format"
    : HELP_ASK.test(after) ? "help" : SKIN_ASK.test(after) ? "skin"
    : REVIEW_ASK.test(after) ? "review" : "answer";
  const repo = payload.repository.full_name;
  const skinRepo = skinRepos.includes(repo);
  const again = mode === "skin" && AGAIN.test(after.split("\n", 1)[0]);
  const refused = pr.state !== "open"
    || ((onIssue || skinRepo) && (!SKIN_REPO_MODES.has(mode) || !skinRepo))
    || ((!OPEN_MODES.has(mode) || again) && STRANGERS.has(comment.author_association));
  return { pr: String(pr.number), repo, comment: `${kind}/${comment.id}`,
    mode: refused ? "refuse" : mode, source: onIssue ? "issue" : "mention" };
}

/** Return the bot's newest review summary and newest skin check on a pull request, null if none. */
async function latestChecks(repo, number, get) {
  const found = { summary: null, skin: null };
  for (let page = 1; page <= 5; page++) {
    const items = await get(`/repos/${repo}/issues/${number}/comments?per_page=100&page=${page}`);
    if (!Array.isArray(items)) return found;
    for (const c of items.filter((item) => item.user?.login === BOT)) {
      if (SUMMARY.test(c.body || "")) found.summary = c;
      if (SKIN_CHECK.test(c.body || "")) found.skin = c;
    }
    if (items.length < 100) return found;
  }
  return found;
}

/** Return the runs for a pull request opening or push, or a finding's reply. */
export async function followUp(event, payload, allowed, get, skinRepos = []) {
  const repo = payload.repository?.full_name;
  const pr = payload.pull_request;
  if (!allowed.includes(repo) || !pr || pr.state !== "open") return null;
  if (event === "pull_request") {
    const run = (mode) => ({ pr: String(pr.number), repo, comment: "", mode, source: "push" });
    if (skinRepos.includes(repo)) return SKIN_ACTIONS.has(payload.action) ? [run("skin")] : null;
    if (payload.action !== "synchronize") return null;
    const { summary, skin } = await latestChecks(repo, pr.number, get);
    const runs = [];
    if (summary && OPEN_PROBLEMS.test(summary.body)) runs.push("review");
    if (skin) runs.push("skin");
    return runs.length ? runs.map(run) : null;
  }
  const comment = payload.comment;
  if (event !== "pull_request_review_comment" || payload.action !== "created"
      || !comment?.in_reply_to_id || comment.user?.type === "Bot"
      || STRANGERS.has(comment.author_association)) return null;
  const parent = await get(`/repos/${repo}/pulls/comments/${comment.in_reply_to_id}`);
  return parent?.user?.login === BOT && FINDING.test(parent.body || "")
    ? [{ pr: String(pr.number), repo, comment: `review/${comment.id}`, mode: "answer",
      source: "reply" }] : null;
}

/** Read a public GitHub API path as JSON, without the token if it is refused; null on failure. */
async function readGitHub(path, token) {
  const headers = { Accept: "application/vnd.github+json", "User-Agent": "kodi-review-relay",
    "X-GitHub-Api-Version": "2022-11-28" };
  let reply = await fetch(`https://api.github.com${path}`,
    { headers: { ...headers, Authorization: `Bearer ${token}` } });
  if ([401, 403, 404].includes(reply.status)) {
    reply = await fetch(`https://api.github.com${path}`, { headers });
  }
  return reply.ok ? reply.json() : null;
}

export default {
  /** Handle a fetch: dispatch runs for a signed mention, reply, opening or push, or decline. */
  async fetch(request, env) {
    if (request.method !== "POST" || new URL(request.url).pathname !== "/github") {
      return new Response("Not found", { status: 404 });
    }
    const body = await request.text();
    if (!(await verify(env.WEBHOOK_SECRET, body, request.headers.get("x-hub-signature-256")))) {
      return new Response("Bad signature", { status: 401 });
    }
    const event = request.headers.get("x-github-event");
    const payload = JSON.parse(body);
    const allowed = env.ALLOWED_REPOS.split(/\s+/);
    const skinRepos = (env.SKIN_REPOS || "").split(/\s+/);
    const asked = trigger(event, payload, allowed, skinRepos);
    const runs = asked ? [asked] : await followUp(event, payload, allowed,
      (path) => readGitHub(path, env.DISPATCH_TOKEN), skinRepos);
    if (!runs) return new Response("Ignored", { status: 202 });
    const replies = await Promise.all(runs.map((inputs) => fetch(
      `https://api.github.com/repos/${env.BOT_REPO}/actions/workflows/review.yml/dispatches`, {
        method: "POST",
        headers: { Authorization: `Bearer ${env.DISPATCH_TOKEN}`,
          Accept: "application/vnd.github+json", "User-Agent": "kodi-review-relay",
          "X-GitHub-Api-Version": "2022-11-28" },
        body: JSON.stringify({ ref: "main", inputs }),
      })));
    const failed = replies.find((reply) => !reply.ok);
    return new Response(failed ? `Dispatch failed: ${failed.status}` : "Dispatched",
      { status: failed ? 502 : 202 });
  },
};
