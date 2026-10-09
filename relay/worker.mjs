const MENTION = /(?<![\w-])@kodi-review(?![\w-])/i;
const REVIEW_ASK = /^[\s,:.!]*(?:review\b|$)/i;
const QUOTED = /```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`|<!--[\s\S]*?-->|^[ \t]*>[^\n]*/gm;

/** Return a comment's own words, without code, quoted lines or HTML comments. */
export function spoken(body) {
  return body.replace(QUOTED, " ");
}
const STRANGERS = new Set(["NONE", "FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR", "MANNEQUIN"]);
const BOT = "kodi-review[bot]";
const FINDING = /^\*\*(Serious|Moderate|Minor)\b/;
const SUMMARY = /^<!-- kodi-review sha=/;
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

/** Return the review inputs a new mention on an open pull request asks for, or null. */
export function trigger(event, payload, allowed) {
  if (payload.action !== "created" || !allowed.includes(payload.repository?.full_name)) return null;
  const comment = payload.comment;
  const words = spoken(comment?.body || "");
  if (!comment || comment.user?.type === "Bot" || STRANGERS.has(comment.author_association)
      || !MENTION.test(words)) return null;
  const pr = event === "issue_comment" && payload.issue?.pull_request ? payload.issue
    : event === "pull_request_review_comment" ? payload.pull_request : null;
  if (!pr || pr.state !== "open") return null;
  const kind = event === "issue_comment" ? "issue" : "review";
  const after = words.slice(MENTION.exec(words).index + "@kodi-review".length);
  return { pr: String(pr.number), repo: payload.repository.full_name,
    comment: `${kind}/${comment.id}`, mode: REVIEW_ASK.test(after) ? "review" : "answer",
    source: "mention" };
}

/** Return the bot's newest summary comment on a pull request, or null. */
async function latestSummary(repo, number, get) {
  let found = null;
  for (let page = 1; page <= 5; page++) {
    const items = await get(`/repos/${repo}/issues/${number}/comments?per_page=100&page=${page}`);
    if (!Array.isArray(items)) return found;
    for (const c of items) if (c.user?.login === BOT && SUMMARY.test(c.body || "")) found = c;
    if (items.length < 100) return found;
  }
  return found;
}

/** Return the inputs for a push after open findings or a reply in a finding's thread, or null. */
export async function followUp(event, payload, allowed, get) {
  const repo = payload.repository?.full_name;
  const pr = payload.pull_request;
  if (!allowed.includes(repo) || !pr || pr.state !== "open") return null;
  if (event === "pull_request") {
    if (payload.action !== "synchronize") return null;
    const summary = await latestSummary(repo, pr.number, get);
    return summary && OPEN_PROBLEMS.test(summary.body)
      ? { pr: String(pr.number), repo, comment: "", mode: "review", source: "push" } : null;
  }
  const comment = payload.comment;
  if (event !== "pull_request_review_comment" || payload.action !== "created"
      || !comment?.in_reply_to_id || comment.user?.type === "Bot"
      || STRANGERS.has(comment.author_association)) return null;
  const parent = await get(`/repos/${repo}/pulls/comments/${comment.in_reply_to_id}`);
  return parent?.user?.login === BOT && FINDING.test(parent.body || "")
    ? { pr: String(pr.number), repo, comment: `review/${comment.id}`, mode: "answer",
      source: "reply" } : null;
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
  /** Handle a fetch event: dispatch review.yml for a signed mention, reply or push, or decline. */
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
    const inputs = trigger(event, payload, allowed)
      ?? await followUp(event, payload, allowed, (path) => readGitHub(path, env.DISPATCH_TOKEN));
    if (!inputs) return new Response("Ignored", { status: 202 });
    const reply = await fetch(
      `https://api.github.com/repos/${env.BOT_REPO}/actions/workflows/review.yml/dispatches`, {
        method: "POST",
        headers: { Authorization: `Bearer ${env.DISPATCH_TOKEN}`,
          Accept: "application/vnd.github+json", "User-Agent": "kodi-review-relay",
          "X-GitHub-Api-Version": "2022-11-28" },
        body: JSON.stringify({ ref: "main", inputs }),
      });
    return new Response(reply.ok ? "Dispatched" : `Dispatch failed: ${reply.status}`,
      { status: reply.ok ? 202 : 502 });
  },
};
