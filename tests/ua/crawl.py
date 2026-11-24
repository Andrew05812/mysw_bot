"""Crawl every reachable screen and button of mysw_bot; report defects."""
import asyncio
import json
import os
import re
import sys

import harness as H
import bot
import sandbox

TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024
DATA_LIMIT = 64
THRESH = 25.0

SKIP_APIS = {
    "delete", "pin", "answer", "delete_message", "edit_message_reply_markup",
    "send_chat_action", "set_my_commands", "copy", "forward",
}

BT = "\x60"


def markdown_warnings(txt):
    out = []
    if txt.count(BT) % 2:
        out.append("unbalanced code span")
    if txt.count(BT * 3) % 2:
        out.append("unbalanced code fence")
    if txt.count("*") % 2:
        out.append("odd asterisk count")
    if txt.count("[") != txt.count("]"):
        out.append("unbalanced link brackets")
    return out


def inline_rows(markup):
    return getattr(markup, "inline_keyboard", None) or []


def inline_datas(markup):
    found = []
    for row in inline_rows(markup):
        for button in row:
            data = getattr(button, "callback_data", None)
            if data:
                found.append(data)
    return found


def check_calls(calls):
    problems = []
    for call in calls:
        api = call.get("api")
        if api in SKIP_APIS:
            continue
        txt = call.get("text")
        limit = CAPTION_LIMIT if "photo" in api or "document" in api else TEXT_LIMIT
        if not isinstance(txt, str) or not txt.strip():
            problems.append({"kind": "empty", "api": api})
            continue
        if len(txt) > limit:
            problems.append({"kind": "too_long", "api": api, "len": len(txt), "limit": limit})
        mode = call.get("parse_mode") or ""
        if mode.lower() in ("markdown", "markdownv2"):
            for warning in markdown_warnings(txt):
                problems.append({"kind": "markdown", "api": api, "detail": warning})
        markup = call.get("reply_markup")
        datas = []
        for row in inline_rows(markup):
            if not row:
                problems.append({"kind": "keyboard", "api": api, "detail": "empty row"})
            for button in row:
                if button.callback_data:
                    datas.append(button.callback_data)
        for data in datas:
            if len(data.encode("utf-8")) > DATA_LIMIT:
                problems.append({"kind": "button_data", "api": api, "detail": data})
        dupes = [d for d in set(datas) if datas.count(d) > 1]
        if dupes:
            problems.append({"kind": "duplicate_button", "api": api, "detail": dupes})
    return problems


def run_action(action, auth=True):
    if auth:
        bot.AUTHENTICATED_USERS.add(H.USER_ID)
        bot._AUTH_SESSIONS[H.USER_ID] = 10 ** 9
    rec = H.Recorder()
    H.GLOBAL_REC = rec
    ctx = H.FakeContext(rec)
    kind = action["kind"]
    value = action["value"]
    if kind == "text":
        update = H.FakeUpdate(rec, text=value)
        coro = bot.handle_text(update, ctx)
    elif kind == "cmd":
        update = H.FakeUpdate(rec, text="/" + value)
        coro = getattr(bot, action["fn"])(update, ctx)
    else:
        update = H.FakeUpdate(rec, data=value)
        coro = bot.callback(update, ctx)
    error = None
    try:
        asyncio.run(asyncio.wait_for(coro, timeout=THRESH))
    except BaseException as exc:
        error = type(exc).__name__ + ": " + str(exc)[:200]
    return {
        "kind": kind,
        "value": value,
        "error": error,
        "calls": rec.calls,
        "problems": check_calls(rec.calls),
        "buttons": [d for c in rec.calls for d in inline_datas(c.get("reply_markup"))],
        "texts": [c.get("text") for c in rec.calls if isinstance(c.get("text"), str)],
    }


def command_actions():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    src = open(os.path.join(root, "bot.py"), encoding="utf-8").read()
    found = re.findall(r'CommandHandler\("(\w+)", (\w+)\)', src)
    return [{"kind": "cmd", "value": name, "fn": fn} for name, fn in found]


def text_actions():
    labels = []
    for row in bot.main_kb().keyboard:
        for button in row:
            labels.append(button.text)
    labels.append("/start")
    unique = []
    for item in labels:
        if item not in unique:
            unique.append(item)
    return [{"kind": "text", "value": item} for item in unique]


def save(results):
    with open("/out/report.json", "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=1, default=str)


def main():
    sandbox.setup()
    bot.AUTHENTICATED_USERS.add(H.USER_ID)
    bot._AUTH_SESSIONS[H.USER_ID] = 10 ** 9
    queue = text_actions() + command_actions()
    results = []
    seen = set()
    while queue and len(results) < 400:
        action = queue.pop(0)
        key = (action["kind"], action["value"])
        if key in seen:
            continue
        seen.add(key)
        result = run_action(action)
        results.append(result)
        for data in result["buttons"]:
            if ("cb", data) not in seen:
                queue.append({"kind": "cb", "value": data})
    save(results)
    print("ACTIONS", len(results))
    print("BLOCKED", len(sandbox.BLOCKED))
    return results


if __name__ == "__main__":
    res = main()
    bad = [x for x in res if x["error"] or x["problems"]]
    print("FAILING", len(bad))
    for x in bad[:100]:
        print(x["kind"], repr(x["value"])[:50], "|ERR", x["error"], "|PROB", x["problems"][:2])

