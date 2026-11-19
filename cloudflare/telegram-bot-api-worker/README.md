# tg-bot-api-proxy — Telegram Bot API Cloudflare Worker

A minimal, hardened proxy from `tg-bot-api-proxy.andrey05812andrey05812.workers.dev`
to the Telegram Bot API (`https://api.telegram.org`). It is the only Telegram
Bot API path for `mysw_bot` (direct `api.telegram.org` is blocked on the server).

## Contract (compatible with mysw_bot)

- Bot API: `POST/GET {base}/bot<TOKEN>/<METHOD>`
- File API: `{base}/file/bot<TOKEN>/<FILE_PATH>`
- Auth: header `X-Worker-Api-Key: <secret>` (equals the `WORKER_API_KEY` secret)
- `python-telegram-bot` config uses `base_url = .../bot`, `base_file_url = .../file/bot`,
  and sends `X-Worker-Api-Key` on both the normal request client and the
  `get_updates_request` client (httpx default headers).

## Features

- Fixed upstream `https://api.telegram.org`; never an open proxy.
- Only `GET` and `POST`; `OPTIONS` returns 204.
- Query params, POST body, headers and binary File API bodies pass through.
- Auth is checked with a length-checked XOR comparison (best-effort timing
  mitigation, not a formal cryptographic constant-time primitive); the auth
  header is **not** forwarded to Telegram.
- Long polling: no artificial Worker fetch timeout; the upstream response is
  streamed back as-is (Telegram `getUpdates?timeout=N` is preserved).
- On upstream failure a controlled JSON `502` is returned; no secrets leak.

## Files

- `src/index.js` — Worker implementation.
- `src/index.test.mjs` — unit tests with a mocked `fetch` (no network/token).
- `wrangler.toml` — project metadata (no secrets).
- `examples/curl.md` — safe curl checks with placeholder values.

## Local verification

```bash
node --check src/index.js
node --test src/index.test.mjs
```

## Deploy (Cloudflare Dashboard)

1. Open Cloudflare Dashboard -> Workers & Pages -> Worker `tg-bot-api-proxy`.
2. (If available) save a copy of the current code as a backup version.
3. Replace the code with the contents of `src/index.js`.
4. Settings -> Variables and Secrets -> ensure a **secret** `WORKER_API_KEY`
   exists with the same value the bot already uses (do NOT send it in chat;
   paste it from the existing protected location, e.g. the mybot env file).
5. Keep the worker name and public URL unchanged.
6. Deploy.

Then verify (see `examples/curl.md`):

- no key -> `401`
- `getMe` with key -> `{"ok":true,...,"username":"..."}`
- `getWebhookInfo` with key -> ok
- a `sendMessage` to the owner -> delivered
- a File API download -> binary OK
- one `mybot` instance starts long polling without 502/Conflict/Unauthorized

After the worker returns 200 for `getMe`, restart mybot:
`docker restart mybot` and test `/tgvideo`, Apps, Очередь, История, Мониторинг.

## Long polling and platform limits

This worker applies **no artificial timeout**: `getUpdates?timeout=N` is
forwarded to Telegram unchanged and the response is streamed back as-is.

Cloudflare Workers does not impose a hard wall-clock limit on an incoming HTTP
request while the client stays connected (see
https://developers.cloudflare.com/workers/platform/limits/). The Free plan
caps *CPU time* (10 ms) but waiting on a network `fetch` is not active CPU
execution, and a response body that continues streaming keeps the worker alive.
So long polling works on the Free plan for typical Telegram `getUpdates` waits;
no paid plan is required for this.

The Free plan also limits the worker to **100,000 incoming requests/day**. A bot
that polls constantly (one `getUpdates` every few seconds, plus every message
and command) can approach this; keep it in mind for capacity planning, but it
is a per-day request budget, not a per-request wall-clock limit.

Remaining real constraints are on the client and network, not the worker:
- the bot's HTTP read timeout must exceed Telegram's requested `getUpdates`
  `timeout` (so the client does not close the connection first);
- Telegram Bot API and any intermediary network components have their own
  idle/timeout behaviour.

No claim is made that connections run forever or that every client tolerates
arbitrarily long responses.
