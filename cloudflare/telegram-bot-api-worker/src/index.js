/**
 * Cloudflare Worker: Telegram Bot API proxy for mysw_bot.
 * Proxies ONLY to https://api.telegram.org (fixed upstream). No open proxy.
 * Routes: /bot<TOKEN>/<METHOD> and /file/bot<TOKEN>/<FILE_PATH>.
 * Auth: X-Worker-Api-Key header equals the WORKER_API_KEY secret.
 * Redirects: worker never follows; a redirect to a location outside
 * api.telegram.org is rejected with a controlled 502 and no external Location
 * is returned (a URL containing the token cannot redirect to an external host).
 * Long polling: no artificial timeout; the upstream response is streamed back.
 */
const UPSTREAM = "https://api.telegram.org";
const KEY_HEADER = "X-Worker-Api-Key";
const FILE_PREFIX = "/file/bot";
const BOT_PREFIX = "/bot";
const HOP_BY_HOP = new Set([
  "connection", "keep-alive", "proxy-connection", "proxy-authenticate",
  "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade",
]);

function json(status, obj) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

// Length-checked XOR comparison. Best-effort timing mitigation, not a formal
// cryptographic constant-time primitive.
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

// Drop hop-by-hop headers AND any header named in the Connection value, e.g.
// "Connection: keep-alive, X-Internal-Header" also drops X-Internal-Header.
function stripHopByHop(headers) {
  const out = new Headers();
  const conn = headers.get("connection");
  const listed = new Set();
  if (conn) {
    for (const part of conn.split(",")) {
      const t = part.trim().toLowerCase();
      if (t) listed.add(t);
    }
  }
  for (const [k, v] of headers.entries()) {
    const lk = k.toLowerCase();
    if (lk === "connection") continue;
    if (HOP_BY_HOP.has(lk)) continue;
    if (listed.has(lk)) continue;
    out.set(k, v);
  }
  return out;
}

// Allow only a Location that re-enters this worker (relative) or stays on the
// fixed upstream origin.
function isAllowedRedirectLocation(loc) {
  if (!loc) return false;
  if (loc.startsWith("/")) return true;
  return loc.startsWith("https://api.telegram.org") ||
         loc.startsWith("http://api.telegram.org");
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

    const headers = stripHopByHop(request.headers);
    headers.delete(KEY_HEADER);

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
      if (resp.status >= 300 && resp.status < 400) {
        const loc = resp.headers.get("location");
        if (loc && !isAllowedRedirectLocation(loc)) {
          return json(502, { ok: false, error_code: 502,
                             description: "upstream redirect to unsupported location" });
        }
      }
      const outHeaders = stripHopByHop(resp.headers);
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
