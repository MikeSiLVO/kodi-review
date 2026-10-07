const MENTION = /(?<![\w-])@kodi-review(?![\w-])/i;
const REVIEW_ASK = /^[\s,:.!]*(?:review\b|$)/i;
const QUOTED = /```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`|<!--[\s\S]*?-->|^[ \t]*>[^\n]*/gm;

/** Return a comment's own words, without code, quoted lines or HTML comments. */
export function spoken(body) {
  return body.replace(QUOTED, " ");
}
const STRANGERS = new Set(["NONE", "FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR", "MANNEQUIN"]);

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
    comment: `${kind}/${comment.id}`, mode: REVIEW_ASK.test(after) ? "review" : "answer" };
}

export default {
  /** Handle a fetch event: dispatch review.yml for a signed mention, refuse or ignore the rest. */
  async fetch(request, env) {
    if (request.method !== "POST" || new URL(request.url).pathname !== "/github") {
      return new Response("Not found", { status: 404 });
    }
    const body = await request.text();
    if (!(await verify(env.WEBHOOK_SECRET, body, request.headers.get("x-hub-signature-256")))) {
      return new Response("Bad signature", { status: 401 });
    }
    const inputs = trigger(request.headers.get("x-github-event"), JSON.parse(body),
      env.ALLOWED_REPOS.split(/\s+/));
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
