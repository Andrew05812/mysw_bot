/**
 * Cloudflare Worker: Telegram Bot API proxy for mysw_bot.
 *
 * Proxies ONLY to https://api.telegram.org (fixed upstream). No open proxy.
 * Routes:
 *   - /bot<TOKEN>/<METHOD>          Bot API
 *   - /file/bot<TOKEN>/<FILE_PATH>  File API (binary pass-through)
 * Auth: X-Worker-Api-Key header must equal the WORKER_API_KEY secret.
 * Long polling: no artificial timeout is applied; the upstream response is
 * streamed back as-is. No secrets are stored in source or returned/logged.
 */
const UPSTREAM = "https://api.telegram.org";
const KEY_HEADER = "X-Worker-Api-Key";
const FILE_PREFIX = "/file/bot";
const BOT_PREFIX = "/bot";

function json(status, obj) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function constantTimeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) {
    return false;
  }
  let diff = 0;
  for (let i = 0; i < a.length; i++) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

function buildUpstreamPath(pathname) {
  if (pathname.startsWith(FILE_PREFIX)) {
    return UPSTREAM + FILE_PREFIX + pathname.slice(FILE_PREFIX.length);
  }
  if (pathname.startsWith(BOT_PREFIX)) {
    return UPSTREAM + BOT_PREFIX + pathname.slice(BOT_PREFIX.length);
  }
  return null;
}

export default {
  async fetch(request, env) {
    const method = request.method;

    if (method === "OPTIONS") {
      return new Response(null, { status: 204 });
    }
    if (method !== "GET" && method !== "POST") {
      return json(405, { ok: false, error_code: 405, description: "method not allowed" });
    }

    const expected = env[KEY_HEADER] || env["WORKER_API_KEY"];
    if (!expected) {
      return json(500, { ok: false, error_code: 500, description: "WORKER_API_KEY not configured" });
    }
    const provided = request.headers.get(KEY_HEADER);
    if (!provided || !constantTimeEqual(provided, expected)) {
      return json(401, { ok: false, error_code: 401, description: "unauthorized" });
    }

    const url = new URL(request.url);
    const upstreamPath = buildUpstreamPath(url.pathname);
    if (upstreamPath === null) {
      return json(404, { ok: false, error_code: 404, description: "not found" });
    }

    // Copy headers except the worker auth header and host.
    const headers = new Headers();
    for (const [k, v] of request.headers.entries()) {
      const lk = k.toLowerCase();
      if (lk === KEY_HEADER.toLowerCase() || lk === "host") continue;
      headers.set(k, v);
    }

    const upstreamUrl = upstreamPath + url.search;
    const upstreamRequest = new Request(upstreamUrl, {
      method,
      headers,
      body: method === "GET" ? undefined : request.body,
      duplex: method === "GET" ? undefined : "half",
      redirect: "manual",
    });

    try {
      const resp = await fetch(upstreamRequest);
      const outHeaders = new Headers();
      for (const [k, v] of resp.headers.entries()) {
        const lk = k.toLowerCase();
        if (lk === "content-length" || lk === "connection" || lk === "transfer-encoding") {
          continue;
        }
        outHeaders.set(k, v);
      }
      return new Response(resp.body, {
        status: resp.status,
        statusText: resp.statusText,
        headers: outHeaders,
      });
    } catch (err) {
      return json(502, { ok: false, error_code: 502, description: "upstream unavailable" });
    }
  },
};
