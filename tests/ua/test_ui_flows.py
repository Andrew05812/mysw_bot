"""User-scenario acceptance tests for the mysw_bot Telegram interface.

Every test drives the real handlers with Telegram-like doubles (see
harness.py) and an offline host stub. Nothing here touches production:
no Telegram API, no SSH to the host, no docker socket.
"""
import os
import re

import pytest

import bot
import harness as H
from crawl import command_actions, run_action, text_actions

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BT = "\x60"


@pytest.fixture(autouse=True)
def ui(monkeypatch):
    monkeypatch.setattr(bot, "host_run", H.fake_host_run)
    monkeypatch.setattr(bot, "append_audit", lambda *a, **k: None)
    monkeypatch.setattr(bot, "docker_client", None)
    monkeypatch.setattr(bot, "AUTHENTICATED_USERS", {H.USER_ID})
    monkeypatch.setattr(bot, "_AUTH_SESSIONS", {H.USER_ID: 10 ** 9})


def screen_text(result):
    return [t for t in result["texts"] if t and t.strip()]


def test_every_menu_button_answers(ui):
    silent = []
    for action in text_actions():
        result = run_action(action)
        assert result["error"] is None, (action, result["error"])
        if not screen_text(result):
            silent.append(action["value"])
    assert silent == []


def test_every_command_answers(ui):
    silent = []
    for action in command_actions():
        result = run_action(action)
        assert result["error"] is None, (action, result["error"])
        if not screen_text(result):
            silent.append("/" + action["value"])
    assert silent == []


def test_error_placeholder_never_leaks(ui):
    bad = []
    for action in text_actions() + command_actions():
        result = run_action(action)
        for text in screen_text(result):
            if "Invalid JSON from host" in text or "object has no attribute" in text:
                bad.append((action["value"], text[:80]))
    assert bad == []


def test_no_dead_inline_buttons(ui):
    seen = set()
    queue = []
    for action in text_actions():
        for data in run_action(action)["buttons"]:
            if data not in seen:
                queue.append(data)
    dead = []
    while queue and len(seen) < 600:
        data = queue.pop(0)
        if data in seen:
            continue
        seen.add(data)
        result = run_action({"kind": "cb", "value": data})
        text = " ".join(screen_text(result))
        if result["error"] or not (text or result["buttons"]):
            dead.append((data, result["error"] or "no response"))
        for nxt in result["buttons"]:
            if nxt not in seen:
                queue.append(nxt)
    assert dead == []


FENCE_RE = re.compile(BT * 3 + ".*?" + BT * 3, re.S)
INLINE_RE = re.compile(BT + "+[^" + BT + "]+" + BT + "+")


def unbalanced_markdown(text):
    body = FENCE_RE.sub("", text)
    body = INLINE_RE.sub("", body)
    problems = []
    if body.count("*") % 2:
        problems.append("odd asterisk")
    if body.count("_") % 2:
        problems.append("odd underscore")
    # Legacy Markdown only treats [] as an entity inside a [text](url) link.
    if "](" in body and body.count("[") != body.count("]"):
        problems.append("unbalanced link brackets")
    return problems

def test_screens_have_valid_markdown(ui):
    broken = []
    for action in text_actions() + command_actions():
        result = run_action(action)
        for call in result["calls"]:
            text = call.get("text")
            if not isinstance(text, str):
                continue
            mode = str(call.get("parse_mode") or "")
            if "Markdown" not in mode:
                continue
            problems = unbalanced_markdown(text)
            if problems:
                broken.append((action["value"], problems, text[:60]))
    assert broken == []

def test_buttons_fit_callback_limit(ui):
    too_long = []
    for action in text_actions() + command_actions():
        for data in run_action(action)["buttons"]:
            if len(data.encode("utf-8")) > 64:
                too_long.append((action["value"], data))
    assert too_long == []


def test_messages_fit_telegram_limit(ui):
    too_long = []
    for action in text_actions() + command_actions():
        for text in screen_text(run_action(action)):
            if len(text) > 4096:
                too_long.append((action["value"], len(text)))
    assert too_long == []


def test_unknown_callback_gets_friendly_reply(ui):
    result = run_action({"kind": "cb", "value": "zzz_stale_button_42"})
    assert result["error"] is None
    text = " ".join(screen_text(result)).lower()
    assert "устарел" in text


def test_broken_markdown_screen_is_reposted_as_plain_text(ui):
    """A screen whose Markdown Telegram rejects must still be delivered."""
    import asyncio
    import types

    rec = H.Recorder()

    class BrokenQuery(H.FakeCallbackQuery):
        async def edit_message_text(self, text=None, **kw):
            rec.add("edit_message_text", text, **kw)
            raise RuntimeError("Can't parse entities: character '+' is reserved")

    query = BrokenQuery(rec, "fs_menu")
    update = types.SimpleNamespace(callback_query=query)
    asyncio.run(bot.callback(update, H.FakeContext(rec)))
    replies = [call for call in rec.calls if call["api"] == "reply_text"]
    assert replies, rec.calls
    assert replies[-1]["text"]
    assert "parse_mode" not in replies[-1]


def test_non_admin_sees_only_denial(ui, monkeypatch):
    monkeypatch.setattr(bot, "AUTHENTICATED_USERS", set())
    monkeypatch.setattr(bot, "_AUTH_SESSIONS", {})
    leaked = []
    for action in command_actions():
        result = run_action(action, auth=False)
        if result["error"] or result["buttons"]:
            leaked.append(action["value"])
    assert leaked == []
