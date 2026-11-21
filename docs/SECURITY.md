# Security Model — mysw_bot

## Access model

The bot grants administration only to users who have BOTH:

1. **Authenticated** — the user entered the correct `BOT_ACCESS_PASSWORD`
   (added to the in-memory `AUTHENTICATED_USERS` set for the process lifetime).
2. **Authorized as admin** — when `ADMIN_IDS` is non-empty, the user id must be
   in that list. When `ADMIN_IDS` is empty, any authenticated user is treated as
   admin (documented policy).

`user_is_admin(user_id)` enforces: `None` is never admin; a user not in
`AUTHENTICATED_USERS` is never admin; with `ADMIN_IDS` set, the id must be in
that list. Every command and callback handler re-checks this; menu visibility is
not a substitute.

## Trust boundaries

- **Owner/operator** -> bot: via Telegram, gated by password + ADMIN_IDS.
- **Bot** -> GMKtec host: via SSH (`host_run`, `subprocess` -> `ssh`), using the
  mounted private key `SSH_KEY_PATH`, host `adelf@172.17.0.1:2222`. Command
  strings are shell commands; only trusted admin callers reach `host_run`.
- **Bot** -> Docker: via the mounted `/var/run/docker.sock` and the `docker` SDK.
- **Bot** -> TG-VIDEO / Cloudflare Worker: read-only HTTP status; the Worker
  requires `X-Worker-Api-Key` (secret) and never forwards it upstream.

## Admin action protection

- Never trust a callback `callback_data` alone to authorize an operation.
- Reject `None` / unauthenticated / non-admin users with an answer and do NOT
  run the action.
- Batch/command inputs are validated; arbitrary free-text is not interpolated
  into shell as trusted code.
- File/JSON writes go to the runtime `DATA_DIR` (`/app/data`) which is a mounted
  volume, never to the image.

## Secret handling

- `TELEGRAM_BOT_TOKEN`, `BOT_ACCESS_PASSWORD`, `WORKER_API_KEY`, `SSH_KEY_PATH`
  live in a protected env file / mounted secrets; never in Git, logs, chat,
  audit output or diagnostics.
- `append_audit` stores `details` **verbatim** (truncated to 4000 chars); callers
  must never pass secrets as `details`.
- `sanitize_codeblock` neutralizes triple backticks; `truncate_long_output`
  bounds long output before it reaches the chat.

## Shell / SSH / Docker risks

- `host_run` runs a single command string over SSH; if an admin action
  interpolates user input into that string, it must be validated/allow-listed.
- Docker controls are invoked only by admin-authorized handlers.
- Prefer explicit argument lists (not `shell=True`) for subprocesses where the
  library allows; commands over SSH are inherently a shell, so authorization
  and input validation are the controls.

## Response to suspicious activity

1. Check `read_audit_tail()` / the audit log for unexpected actions.
2. Review recent container/service state via the bot's own monitoring.
3. If a secret is suspected exposed: rotate the token/key in BotFather /
   Cloudflare, update the protected env, restart only `mybot`, and re-verify
   `getMe` / a test command.
4. Do not echo secrets in any report, issue or commit.


## Login hardening (3.3.8)

- Password login is rate limited per Telegram user id: 5 failed attempts then a
  15-minute block (AUTH_MAX_ATTEMPTS, AUTH_BLOCK_SECONDS).
- Authenticated sessions expire after AUTH_SESSION_TTL_SECONDS (default 8h);
  user_is_admin drops an expired session. Re-login restores it; logout clears
  session, attempts and block state.
- The password message is best-effort deleted; if deletion fails the user is
  told to remove it manually. The password is never logged or echoed.
- ADMIN_IDS (when non-empty) restricts admin access; when empty, any
  authenticated user is treated as admin (documented legacy policy).

## Dangerous command confirmation

- classify_dangerous() flags potentially destructive terminal commands
  (recursive deletes, filesystem formatting/writes, host power control, service
  stops, container removal, compose teardown). Flagged commands are routed to
  password confirmation (risk_pending action shell_dangerous); they are not
  executed until the operator re-enters the access password.
- Confirmation is bound to the pending command; a forged callback cannot bypass
  it, and an incorrect password cancels it.

## Shell execution and secret safety

- run_shell_command executes on the GMKtec host over SSH and NEVER falls back
  to running the command inside the container. On SSH error, timeout or
  permission denial it returns a safe diagnostic without local execution.
- redact_sensitive_data() is applied centrally in append_audit and when showing
  commands, removing bot tokens, passwords, API keys, Authorization / Bearer /
  X-Worker-Api-Key values, /bot<TOKEN>/ URLs and password=/token= arguments
  before persistence or display.
- md_inline() sanitizes command text for Telegram Markdown inline spans
  (backticks/newlines removed, length-capped); command output is shown in a
  code block with triple backticks neutralized by sanitize_codeblock.
