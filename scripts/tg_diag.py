#!/usr/bin/env python3
"""Diagnose Telegram Bot API reachability: worker vs direct. No secrets printed."""
import json
import os
import urllib.request

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
KEY = os.environ["WORKER_API_KEY"]
WORKER = "https://tg-bot-api-proxy.andrey05812andrey05812.workers.dev"
UA = "mysw-bot-diag/1.0"


TARGETS = [
    ("worker", WORKER + "/bot" + TOKEN + "/getMe", {"X-Worker-Api-Key": KEY}),
    ("direct", "https://api.telegram.org/bot" + TOKEN + "/getMe", {}),
    ("worker", WORKER + "/bot" + TOKEN + "/getWebhookInfo", {"X-Worker-Api-Key": KEY}),
    ("direct", "https://api.telegram.org/bot" + TOKEN + "/getWebhookInfo", {}),
]

for name, url, extra in TARGETS:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    for key, value in extra.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = resp.read(600).decode("utf-8", "replace")
            data = json.loads(body)
            method = url.rsplit("/", 1)[-1]
            summary = {"ok": data.get("ok"), "status": resp.status}
            result = data.get("result")
            if isinstance(result, dict):
                summary.update(
                    {
                        "pending": result.get("pending_update_count"),
                        "webhook": bool(result.get("url")),
                        "last_error": result.get("last_error_message"),
                        "username_is_bot": result.get("is_bot"),
                    }
                )
            print(name, method, json.dumps(summary, ensure_ascii=False))
    except Exception as exc:
        print(name, "ERR", type(exc).__name__, str(exc)[:140])
