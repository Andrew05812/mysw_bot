# Safe verification curls (placeholder values — do not put real tokens/keys here)

BASE="https://tg-bot-api-proxy.andrey05812andrey05812.workers.dev"
# Use a real key only from the protected env, without printing it:
#   set -a; . /home/adelf/.config/mybot/mybot.env; set +a
#   KEY="$WORKER_API_KEY"; TOK="$TELEGRAM_BOT_TOKEN"

# 1. No key -> expect 401
# curl -s -o /dev/null -w "%{http_code}\n" "$BASE/bot<TOKEN>/getMe"

# 2. getMe with key -> expect {"ok":true,...,"username":"..."}
# curl -s -H "X-Worker-Api-Key: $KEY" "$BASE/bot$TOK/getMe"

# 3. getWebhookInfo
# curl -s -H "X-Worker-Api-Key: $KEY" "$BASE/bot$TOK/getWebhookInfo"

# 4. POST sendMessage to owner (expect delivered)
# curl -s -H "X-Worker-Api-Key: $KEY" -H "Content-Type: application/json" \
#   -d '{"chat_id":<OWNER_ID>,"text":"worker check"}' "$BASE/bot$TOK/sendMessage"

# 5. Bad path -> expect 404
# curl -s -H "X-Worker-Api-Key: $KEY" "$BASE/random"

# Do NOT run getUpdates manually while a production poller is active.
