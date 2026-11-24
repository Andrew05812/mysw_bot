# mybot

Telegram bot for monitoring and managing a Linux host.

## Configuration

- `TELEGRAM_BOT_TOKEN` — Telegram bot token.
- `BOT_ACCESS_PASSWORD` — bot access password.
- `ADMIN_IDS` — comma-separated administrator IDs.
- `WORKER_API_KEY` — Cloudflare Worker API key.
- `SSH_KEY_PATH` — SSH private-key path inside the container.
- `HOST_INTERVAL` — host metrics collection interval.

Never commit real credentials or private SSH keys. The SSH key is mounted read-only at runtime and is not copied into the Docker image.

## Architecture

Single `bot.py` process (python-telegram-bot 22.7). It runs in the `mybot`
container with `network_mode: host`. Admin access to GMKtec goes over SSH
(`host_run`, key from `SSH_KEY_PATH`); Docker is controlled via the mounted
docker socket. TG-VIDEO status comes from its local FastAPI; the Telegram Bot
API is reached through the `tg-bot-api-proxy` Cloudflare Worker
(`cloudflare/telegram-bot-api-worker/`).

## Authorization

- A user must enter `BOT_ACCESS_PASSWORD` (stored in the in-memory
  `AUTHENTICATED_USERS` set) AND, when `ADMIN_IDS` is non-empty, be in that
  list. Every command and callback re-checks `user_is_admin`.
- See `docs/SECURITY.md` for the full model and secret-handling rules.

## Tests

The test suite never touches production config, Docker, SSH or Telegram. It
imports `bot.py` with fake env and a mocked `docker` module, and redirects the
data dir to a temp dir.

Run locally in an ephemeral container (no production changes):

```bash
./scripts/test.sh                    # uses the mybot image; installs pytest
```

Run Python tests directly (requires deps + pytest):

```bash
pip install -r tests/requirements.txt
PYTHONPATH=. python -m pytest tests -q
```

Run the Cloudflare Worker tests (Node):

```bash
cd cloudflare/telegram-bot-api-worker
node --check src/index.js
node --test src/index.test.mjs
```

CI runs all of the above on push/PR (`.github/workflows/ci.yml`).

## Security (3.3.8)

- Password login is rate limited (5 attempts / 15 min block) and sessions expire
  after `AUTH_SESSION_TTL_SECONDS` (default 8 h).
- `run_shell_command` runs host commands over SSH and never falls back to the
  container; SSH errors return a safe diagnostic.
- `redact_sensitive_data()` removes tokens/passwords/API keys from the audit
  log and user-visible text; `classify_dangerous()` routes destructive commands
  through password confirmation.
- See `docs/SECURITY.md`.

## Reliability (3.3.9)

- Polling watchdog (`poll_watchdog`) runs every 60 s. The updater is treated as
  dead when `Updater.running` is false or the internal polling task is done, and
  it is verified with `getMe` through the Worker. After a dead poll the watchdog
  stops the updater and restarts long polling (5 attempts, backoff 1..30 s). If
  no attempt succeeds the process exits with code 1 and Docker restarts the
  container instead of leaving a deaf bot alive.
- `DATA_DIR/polling_heartbeat.json` is refreshed by the watchdog. A daemon thread
  checks its mtime and exits with code 2 when the file is older than 600 s, which
  covers a stuck event loop. The file holds `polling`, `failures`, `restarts`,
  `note` and `iso`.
- Boot uses `bootstrap_retries=-1`: a Telegram/Worker outage at startup no longer
  kills the process. Pending updates are dropped on the first start only and kept
  on watchdog restarts.
- `fit_message()` caps a screen at 4096 characters and closes an opened code
  fence. If Telegram still rejects the Markdown entity, the callback `edit()`
  reposts the same text with no `parse_mode`, so the screen is never lost.
- A callback unknown to the current build (old message after redeploy) now answers
  with a hint and menu buttons instead of doing nothing.
- File manager: symlinks to directories are listed as folders and open as folders.
- Diagnostics: check labels contain no underscore, so Markdown cannot swallow the
  report.

## Test suite layout

- `tests/unit`, `tests/security`, `tests/handlers` are fast and offline.
- `tests/ua` user-scenario acceptance tests press every reply-keyboard button, every
  command and every reachable inline button through the real handlers. The suite
  fails on silence, duplicate or oversized button payloads, messages over 4096
  characters, broken Markdown entities left by dynamic content, and on leaked host
  diagnostics shown to the user.
- `tests/ua/crawl.py` repeats the crawl against the live host and writes
  `/out/report.json`; run it only in a throwaway container.
