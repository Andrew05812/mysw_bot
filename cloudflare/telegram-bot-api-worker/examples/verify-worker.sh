#!/usr/bin/env bash
# Safe verification for the deployed Worker. Reads secrets only from the
# protected env file; never echoes the token, key, full URL or headers.
# Usage: ./examples/verify-worker.sh [/path/to/mybot.env]
set -u
ENVF="${1:-/home/adelf/.config/mybot/mybot.env}"
BASE="https://tg-bot-api-proxy.andrey05812andrey05812.workers.dev"

if [ ! -r "$ENVF" ]; then echo "env file not readable: $ENVF"; exit 2; fi
set -a; . "$ENVF"; set +a

key_len="${#WORKER_API_KEY}"
tok_len="${#TELEGRAM_BOT_TOKEN}"
echo "key_len=$key_len tok_len=$tok_len"

no_key=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/bot${TELEGRAM_BOT_TOKEN}/getMe")
echo "no-key status: $no_key"

me=$(curl -s -H "X-Worker-Api-Key: ${WORKER_API_KEY}" "$BASE/bot${TELEGRAM_BOT_TOKEN}/getMe")
echo "getMe ok: $(echo "$me" | grep -o '"ok":true' | head -1)"

hook=$(curl -s -H "X-Worker-Api-Key: ${WORKER_API_KEY}" "$BASE/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo")
echo "getWebhookInfo ok: $(echo "$hook" | grep -o '"ok":true' | head -1)"

echo "do NOT run getUpdates here while a production poller is active."
