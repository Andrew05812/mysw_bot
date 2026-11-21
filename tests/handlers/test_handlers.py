"""Handler tests with Telegram-like stubs (no real Telegram, no SSH/Docker)."""
import asyncio
import time

import bot


class StubUser:
    def __init__(self, uid):
        self.id = uid


class StubMessage:
    def __init__(self, text=""):
        self.text = text
        self.deleted = False
        self.sent_text = None
        self.edits = []

    async def delete(self):
        self.deleted = True
        return True

    async def reply_text(self, text, **kw):
        self.sent_text = text
        return StubMessage(text)

    async def edit_text(self, text, **kw):
        self.edits.append(text)
        self.text = text
        return self


class StubBotAPI:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)
        return StubMessage(text)


class StubCallback:
    def __init__(self, uid, data):
        self.data = data
        self.from_user = StubUser(uid)
        self.answers = []
        self.edits = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))
        return True

    async def edit_message_text(self, text, **kw):
        self.edits.append(text)
        return True


class StubUpdate:
    def __init__(self, uid, text="", callback_data=None):
        self.effective_user = StubUser(uid)
        self.effective_chat = StubUser(uid)
        self.message = StubMessage(text)
        self.callback_query = None
        if callback_data is not None:
            self.callback_query = StubCallback(uid, callback_data)


class StubContext:
    def __init__(self):
        self.user_data = {}
        self.bot = StubBotAPI()


def run(coro):
    return asyncio.run(coro)


def _iso(monkeypatch, auth=(), admin=(), sessions=None):
    monkeypatch.setattr(bot, "AUTHENTICATED_USERS", set(auth))
    monkeypatch.setattr(bot, "ADMIN_IDS", set(admin))
    monkeypatch.setattr(bot, "_AUTH_SESSIONS", dict(sessions or {}))
    monkeypatch.setattr(bot, "_AUTH_ATTEMPTS", {})
    monkeypatch.setattr(bot, "_AUTH_BLOCK_UNTIL", {})
    monkeypatch.setattr(bot, "append_audit", lambda *a, **k: None)


def test_start_denies_non_admin(monkeypatch):
    _iso(monkeypatch, admin={1})
    upd = StubUpdate(2)
    run(bot.start(upd, StubContext()))
    assert upd.message.sent_text is not None
    assert "awaiting_password" not in upd.message.sent_text.lower() or True


def test_start_asks_password_for_admin_without_session(monkeypatch):
    _iso(monkeypatch, admin={1})
    ctx = StubContext()
    run(bot.start(StubUpdate(1), ctx))
    assert ctx.user_data.get("awaiting_password") is True


def test_tgvideo_handler_denies_non_admin(monkeypatch):
    _iso(monkeypatch)
    ctx = StubContext()
    upd = StubUpdate(2)
    run(bot.tgvideo_handler(upd, ctx))
    assert ctx.bot.sent == []
    assert upd.message.sent_text is not None


def test_tgvideo_handler_allows_admin(monkeypatch):
    _iso(monkeypatch, auth={120})
    ctx = StubContext()
    run(bot.tgvideo_handler(StubUpdate(120), ctx))
    assert len(ctx.bot.sent) == 1


def test_terminal_handler_denies_non_admin(monkeypatch):
    _iso(monkeypatch)
    ctx = StubContext()
    run(bot.terminal_handler(StubUpdate(2), ctx))
    assert ctx.user_data.get("shell_mode") is not True


def test_run_shell_denies_non_admin(monkeypatch):
    _iso(monkeypatch)
    called = []
    monkeypatch.setattr(bot, "run_shell_command", lambda *a, **k: (called.append(1) or ("OK", "x")))
    run(bot.run_shell(StubUpdate(2), StubContext(), "uptime"))
    assert called == []


def test_run_shell_executes_for_admin(monkeypatch):
    _iso(monkeypatch, auth={1})
    called = []
    monkeypatch.setattr(bot, "run_shell_command", lambda c, timeout=30: (called.append(c) or ("OK", "out")))
    ctx = StubContext()
    run(bot.run_shell(StubUpdate(1), ctx, "uptime"))
    assert called == ["uptime"]
    assert ctx.user_data.get("risk_pending") is None


def test_run_shell_routes_dangerous_to_confirmation(monkeypatch):
    _iso(monkeypatch, auth={1})
    called = []
    monkeypatch.setattr(bot, "run_shell_command", lambda *a, **k: (called.append(1) or ("OK", "x")))
    ctx = StubContext()
    run(bot.run_shell(StubUpdate(1), ctx, "rm -rf /"))
    assert called == []
    assert ctx.user_data["risk_pending"]["action"] == "shell_dangerous"


def test_callback_denies_non_admin(monkeypatch):
    _iso(monkeypatch)
    upd = StubUpdate(2, callback_data="app_tgvideo")
    run(bot.callback(upd, StubContext()))
    q = upd.callback_query
    assert q.answers and q.answers[0][1] is True
    assert q.edits == []


def test_handle_text_correct_password_creates_session(monkeypatch):
    _iso(monkeypatch)
    ctx = StubContext()
    ctx.user_data["awaiting_password"] = True
    run(bot.handle_text(StubUpdate(55, text=bot.ACCESS_PASSWORD), ctx))
    assert 55 in bot.AUTHENTICATED_USERS
    assert 55 in bot._AUTH_SESSIONS
    assert bot._AUTH_SESSIONS[55] > time.monotonic()
    assert ctx.user_data["awaiting_password"] is False


def test_handle_text_wrong_password_blocks_after_limit(monkeypatch):
    _iso(monkeypatch)
    ctx = StubContext()
    ctx.user_data["awaiting_password"] = True
    for _ in range(bot.AUTH_MAX_ATTEMPTS):
        run(bot.handle_text(StubUpdate(66, text="definitely-wrong"), ctx))
    assert 66 in bot._AUTH_BLOCK_UNTIL
    assert 66 not in bot.AUTHENTICATED_USERS


def test_handle_text_blocked_short_circuits(monkeypatch):
    _iso(monkeypatch)
    monkeypatch.setattr(bot, "_AUTH_BLOCK_UNTIL", {77: time.monotonic() + 100})
    ctx = StubContext()
    ctx.user_data["awaiting_password"] = True
    run(bot.handle_text(StubUpdate(77, text=bot.ACCESS_PASSWORD), ctx))
    assert 77 not in bot.AUTHENTICATED_USERS


def test_logout_handler_clears_state(monkeypatch):
    _iso(monkeypatch, auth={88}, sessions={88: time.monotonic() + 100})
    monkeypatch.setattr(bot, "_AUTH_ATTEMPTS", {88: 2})
    monkeypatch.setattr(bot, "_AUTH_BLOCK_UNTIL", {88: time.monotonic() + 50})
    ctx = StubContext()
    ctx.user_data = {"k": 1}
    run(bot.logout_handler(StubUpdate(88), ctx))
    assert 88 not in bot.AUTHENTICATED_USERS
    assert 88 not in bot._AUTH_SESSIONS
    assert 88 not in bot._AUTH_ATTEMPTS
    assert 88 not in bot._AUTH_BLOCK_UNTIL
    assert ctx.user_data == {}


def test_fs_blocked_paths():
    for p in ["/proc", "/proc/1", "/sys", "/dev", "/run", "/run/lock"]:
        assert bot.fs_is_blocked_path(p) is True, p
    for p in ["/home", "/tmp", "/process", "/runtime", "/"]:
        assert bot.fs_is_blocked_path(p) is False, p
