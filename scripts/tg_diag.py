#!/usr/bin/env python3
"""Read-only Telegram/Worker connectivity probe. Prints no secrets."""
import json
import os
import urllib.request

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
KEY = os.environ["WORKER_API_KEY"]
WORKER = "https://tg-bot-api-proxy.andrey05812andrey05812.workers.dev"
UA = "mysw-bot-diag/1.0"


def call(base, method, headers=None):
    url = base + "/bot" + TOKEN + "/" + method
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.status, json.loads(resp.read(4000).decode("utf-8", "replace"))


def report(label, method, status, data):
    result = data.get("result")
    summary = {"ok": data.get("ok"), "status": status}
    if method == "getMe" and isinstance(result, dict):
        summary["is_bot"] = result.get("is_bot")
        summary["can_join_groups"] = result.get("can_join_groups")
    if method == "getWebhookInfo" and isinstance(result, dict):
        summary["pending"] = result.get("pending_update_count")
        summary["webhook"] = bool(result.get("url"))
        summary["last_error"] = result.get("last_error_message")
    if method == "getMyCommands":
        summary["commands"] = len(result or [])
        summary["names"] = [item.get("command") for item in (result or [])]
    print(label, method, json.dumps(summary, ensure_ascii=False))


for method in ("getMe", "getWebhookInfo", "getMyCommands"):
    try:
        status, data = call(WORKER, method, {"X-Worker-Api-Key": KEY})
        report("worker", method, status, data)
    except Exception as exc:
        print("worker", method, "ERR", type(exc).__name__, str(exc)[:120])
