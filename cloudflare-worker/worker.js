/**
 * BTC123 Telegram test proxy.
 *
 * Holds TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID as Worker secrets (env-bound,
 * never sent to the browser). The public page POSTs here with no
 * credentials at all; this Worker is the only thing that ever talks to the
 * Telegram API. A lightweight cooldown (Cache API, 20s, shared across all
 * callers) stops the public endpoint from being hammered into a message
 * flood -- it does not stop someone from calling it once every 20s, but
 * that's a minor nuisance ceiling, not a token leak.
 */

// Both mirrors serve the same dashboard and both need to call this Worker.
// A CORS allow-list must echo back the CALLER's own origin (never a fixed
// default) -- returning a fixed origin that doesn't match the requester is
// exactly what silently broke the button on the second mirror before this
// fix: the Worker's HTTP call and the Telegram send both succeeded, but the
// browser discarded the response because Access-Control-Allow-Origin didn't
// match window.location.origin.
const ALLOWED_ORIGINS = new Set([
  "https://wahgor2050.github.io",
  "https://btc123-site.pages.dev",
]);
const COOLDOWN_SECONDS = 20;
const COOLDOWN_KEY = "https://btc123.internal/cooldown";

function corsHeaders(origin) {
  const headers = { "Access-Control-Allow-Methods": "POST, OPTIONS", "Access-Control-Allow-Headers": "Content-Type" };
  if (ALLOWED_ORIGINS.has(origin)) headers["Access-Control-Allow-Origin"] = origin;
  return headers;
}

async function sendTelegram(env, text) {
  const url = `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`;
  const body = new URLSearchParams({ chat_id: env.TELEGRAM_CHAT_ID, text });
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  const j = await r.json().catch(() => ({}));
  return { ok: r.ok && j.ok === true, description: j.description };
}

// Diagnostic route (POST ?diag=1): never returns secret values, only their
// lengths and the bot's own public identity via getMe -- safe to leave in
// for future troubleshooting if a secret ever gets re-entered wrong.
async function diagnose(env) {
  const tokenLen = (env.TELEGRAM_BOT_TOKEN || "").length;
  const chatLen = (env.TELEGRAM_CHAT_ID || "").length;
  let getMe = null;
  try {
    const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/getMe`);
    getMe = await r.json();
  } catch (e) {
    getMe = { error: String(e) };
  }
  return { token_length: tokenLen, chat_id_length: chatLen, getMe };
}

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin") || "";
    const headers = corsHeaders(origin);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers });
    }
    if (request.method !== "POST") {
      return new Response(JSON.stringify({ ok: false, error: "POST only" }), {
        status: 405,
        headers: { ...headers, "Content-Type": "application/json" },
      });
    }
    const u = new URL(request.url);
    if (u.searchParams.get("diag") === "1") {
      const d = await diagnose(env);
      return new Response(JSON.stringify(d), {
        headers: { ...headers, "Content-Type": "application/json" },
      });
    }
    if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) {
      return new Response(
        JSON.stringify({ ok: false, error: "Worker secrets not configured" }),
        { status: 500, headers: { ...headers, "Content-Type": "application/json" } }
      );
    }

    // Global cooldown via the Cache API -- cheap abuse guard, no external state needed.
    const cache = caches.default;
    const cooldownReq = new Request(COOLDOWN_KEY);
    const cached = await cache.match(cooldownReq);
    if (cached) {
      return new Response(
        JSON.stringify({ ok: false, error: `cooldown active, wait ${COOLDOWN_SECONDS}s between tests` }),
        { status: 429, headers: { ...headers, "Content-Type": "application/json" } }
      );
    }
    await cache.put(
      cooldownReq,
      new Response("1", { headers: { "Cache-Control": `max-age=${COOLDOWN_SECONDS}` } })
    );

    const now = new Date().toISOString().replace("T", " ").slice(0, 16) + " UTC";
    const text =
      `\u{1F9EA} BTC123 connectivity test (via Cloudflare Worker) -- ${now}\n` +
      `If you see this, the one-click browser button works.\n` +
      `Paper trading only, no broker, no real orders.`;

    try {
      const result = await sendTelegram(env, text);
      return new Response(JSON.stringify(result), {
        status: result.ok ? 200 : 502,
        headers: { ...headers, "Content-Type": "application/json" },
      });
    } catch (e) {
      return new Response(JSON.stringify({ ok: false, error: "request failed" }), {
        status: 502,
        headers: { ...headers, "Content-Type": "application/json" },
      });
    }
  },
};
