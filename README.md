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
