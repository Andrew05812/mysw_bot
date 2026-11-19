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
- Auth is checked with a constant-time comparison; the auth header is **not**
  forwarded to Telegram.
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

Cloudflare Workers free plan caps request wall-clock duration (approx. 30 s),
so a `getUpdates?timeout=` larger than that would be cut by the platform, not by
this worker. On the paid plan (15 min) long polling is fully supported. Keep the
bot `getUpdates` timeout consistent with the plan; do not raise it above the
platform limit.
