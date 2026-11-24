"""Unit tests for polling self-healing and message safety helpers."""
import json
import os

import bot
import harness as H


class _Task:
    def __init__(self, done):
        self._done = done

    def done(self):
        return self._done


class _Updater:
    def __init__(self, running, task_done=None):
        self.running = running
        if task_done is not None:
            self._Updater__polling_task = _Task(task_done)


class _App:
    def __init__(self, updater):
        self.updater = updater


def test_healthy_requires_updater():
    assert bot.polling_is_healthy(_App(None)) is False


def test_healthy_requires_running():
    assert bot.polling_is_healthy(_App(_Updater(False))) is False


def test_healthy_detects_finished_task():
    assert bot.polling_is_healthy(_App(_Updater(True, True))) is False


def test_healthy_when_task_alive():
    assert bot.polling_is_healthy(_App(_Updater(True, False))) is True


def test_fit_message_truncates_long_text():
    text = "a" * 9000
    fitted = bot.fit_message(text)
    assert len(fitted) <= 4096


def test_fit_message_balances_fence():
    fence = "`" * 3
    fitted = bot.fit_message(fence + ("x" * 8000))
    assert fitted.count(fence) % 2 == 0


class _FakeBotAPI:
    def __init__(self):
        self.pings = 0

    async def get_me(self):
        self.pings += 1
        return object()


class _FakeUpdater:
    def __init__(self, running, task_done):
        self.running = running
        self._Updater__polling_task = _Task(task_done)
        self.stops = 0
        self.starts = []

    async def stop(self):
        self.stops += 1
        self.running = False

    async def start_polling(self, **kwargs):
        self.starts.append(kwargs)
        self.running = True
        self._Updater__polling_task = _Task(False)


class _Ctx:
    def __init__(self, app):
        self.application = app


def test_watchdog_restarts_dead_polling(tmp_data_dir, monkeypatch):
    import asyncio
    import types

    hb = os.path.join(tmp_data_dir, "polling_heartbeat.json")
    monkeypatch.setattr(bot, "HEARTBEAT_FILE", hb)
    monkeypatch.setattr(bot, "_poll_restart_lock", None)
    monkeypatch.setattr(bot, "_poll_state",
                        {"failures": 3, "restarts": 0,
                         "last_ok": None, "last_error": None})
    updater = _FakeUpdater(False, True)
    api = _FakeBotAPI()
    app = types.SimpleNamespace(updater=updater, bot=api)
    asyncio.run(bot.poll_watchdog(_Ctx(app)))
    assert updater.starts
    assert updater.starts[0]["drop_pending_updates"] is False
    data = json.load(open(hb, encoding="utf-8"))
    assert data["polling"] is True
    assert data["restarts"] == 1


def test_watchdog_keeps_healthy_polling_unrestarted(tmp_data_dir, monkeypatch):
    import asyncio
    import types

    hb = os.path.join(tmp_data_dir, "polling_heartbeat.json")
    monkeypatch.setattr(bot, "HEARTBEAT_FILE", hb)
    monkeypatch.setattr(bot, "_poll_restart_lock", None)
    updater = _FakeUpdater(True, False)
    api = _FakeBotAPI()
    app = types.SimpleNamespace(updater=updater, bot=api)
    asyncio.run(bot.poll_watchdog(_Ctx(app)))
    assert updater.starts == []
    assert updater.stops == 0

def test_write_heartbeat(tmp_data_dir, monkeypatch):
    target = os.path.join(tmp_data_dir, "polling_heartbeat.json")
    monkeypatch.setattr(bot, "HEARTBEAT_FILE", target)
    monkeypatch.setattr(bot, "_poll_state", {"failures": 0, "restarts": 2,
                                             "last_ok": None, "last_error": None})
    bot.write_heartbeat(True, "unit")
    data = json.load(open(target, encoding="utf-8"))
    assert data["polling"] is True
    assert data["restarts"] == 2
    assert not os.path.exists(target + ".tmp")


def test_transient_errors_are_classified():
    assert bot.is_transient_telegram_error(Exception("Invalid server response"))
    assert bot.is_transient_telegram_error(Exception("502 Bad Gateway"))
    assert bot.is_transient_telegram_error(Exception("Connection reset by peer"))
    assert not bot.is_transient_telegram_error(Exception("Bad Request: chat not found"))
    assert not bot.is_transient_telegram_error(Exception("Message is too long"))


async def _no_sleep(_seconds):
    return None


def test_screen_is_retried_after_gateway_error(monkeypatch):
    """Two Cloudflare 502s in a row must not cost the user the screen."""
    import asyncio
    import types

    from telegram.error import TelegramError

    monkeypatch.setattr(bot, "AUTHENTICATED_USERS", {H.USER_ID})
    monkeypatch.setattr(bot, "_AUTH_SESSIONS", {H.USER_ID: 10 ** 9})
    monkeypatch.setattr(bot.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(bot, "get_system_stats", lambda: "📊 status screen")
    rec = H.Recorder()

    class FlakyQuery(H.FakeCallbackQuery):
        attempts = 0

        async def edit_message_text(self, text=None, **kw):
            type(self).attempts += 1
            if type(self).attempts < 3:
                raise TelegramError("Invalid server response")
            rec.add("edit_message_text", text, **kw)
            return self.message

    query = FlakyQuery(rec, "status")
    asyncio.run(
        bot.callback(
            types.SimpleNamespace(callback_query=query),
            H.FakeContext(rec),
        )
    )
    screens = [call for call in rec.calls if call["api"] == "edit_message_text"]
    assert FlakyQuery.attempts == 3
    assert screens, rec.calls
    assert screens[-1]["text"]
