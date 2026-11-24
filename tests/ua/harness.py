"""User-scenario harness for mysw_bot.

Drives the REAL handlers (bot.py) with Telegram-like doubles and records every
outgoing Telegram API call. No Telegram network is touched: the bot module is
imported with fake env and a mocked docker SDK. fake_host_run() answers
offline for the acceptance suite; sandbox.py adds the read-only guard used only
by the live crawl.

Checks applied to every produced message:
  * length limit (Telegram rejects text longer than 4096 characters)
  * Markdown entity balance (unbalanced asterisk / underscore / backtick
    silently breaks sendMessage)
  * inline button payload limit (64 bytes -> BUTTON_DATA_INVALID)
  * duplicate callback_data on one keyboard (ambiguous button)
  * empty or None message body
"""
import asyncio
import json
import os
import re
import sys
import tempfile
import types

TMP = tempfile.mkdtemp(prefix="ua_")

os.environ["TELEGRAM_BOT_TOKEN"] = "123:FAKE"
os.environ["BOT_ACCESS_PASSWORD"] = "test-password"
os.environ["ADMIN_IDS"] = "1001"
os.environ["WORKER_API_KEY"] = "a" * 64
os.environ.setdefault("SSH_KEY_PATH", "/tmp/none")

fake_docker = types.ModuleType("docker")


class _FakeCollection:
    def list(self, *a, **k):
        return []

    def get(self, *a, **k):
        raise LookupError("not found")


class _FakeClient:
    def __init__(self):
        self.containers = _FakeCollection()
        self.images = _FakeCollection()

    def ping(self):
        return True


fake_docker.from_env = lambda *a, **k: _FakeClient()
sys.modules.setdefault("docker", fake_docker)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
import bot  # noqa: E402

bot.DATA_DIR = TMP
bot.BACKUP_CONFIG_FILE = os.path.join(TMP, "backup_config.json")
bot.AUTOHEAL_CONFIG_FILE = os.path.join(TMP, "autoheal_config.json")
bot.SCHEDULES_FILE = os.path.join(TMP, "schedules.json")
bot.AUDIT_LOG_FILE = os.path.join(TMP, "audit.log")
bot.PLUGIN_CONFIG_FILE = os.path.join(TMP, "plugins.json")
bot.DOCKER_MONITOR_STATE_FILE = os.path.join(TMP, "docker_monitor_state.json")

USER_ID = 1001
ADMIN_IDS = {USER_ID}



class Guarded:
    MUTATORS = {
        "restart", "stop", "start", "remove", "kill", "pause", "unpause",
        "exec_run", "prune", "pull", "push", "commit", "rename", "reload",
        "put_archive", "attach",
    }

    def __init__(self, obj, label="docker"):
        self._obj = obj
        self._label = label

    def __getattr__(self, name):
        value = getattr(self._obj, name)
        if name in Guarded.MUTATORS and callable(value):
            def blocked(*a, **k):
                raise RuntimeError("sandbox: " + self._label + "." + name + " blocked")
            return blocked
        return value

    def __getitem__(self, key):
        return self._obj[key]

    def __repr__(self):
        return "<guarded " + self._label + ">"


class GuardedCollection:
    def __init__(self, coll, label):
        self._coll = coll
        self._label = label

    def __getattr__(self, name):
        return getattr(self._coll, name)

    def list(self, *a, **k):
        return [Guarded(x, self._label + ".item") for x in self._coll.list(*a, **k)]

    def get(self, *a, **k):
        return Guarded(self._coll.get(*a, **k), self._label + ".item")

    def __repr__(self):
        return "<guarded-collection " + self._label + ">"


class GuardedClient:
    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        value = getattr(self._client, name)
        if name in ("containers", "images", "volumes", "networks"):
            return GuardedCollection(value, name)
        return value


if getattr(bot, "docker_client", None) is not None:
    bot.docker_client = GuardedClient(bot.docker_client)


FAKE_ENTRIES = [
    {"name": "etc", "is_dir": True, "size": 0},
    {"name": "passwd", "is_dir": False, "size": 12},
]


def fake_host_run(command, timeout=10):
    cmd = str(command)
    if "os.scandir" in cmd:
        return json.dumps({"ok": True, "path": "/", "total": 2,
                           "entries": FAKE_ENTRIES})
    if "json.dumps" in cmd:
        return json.dumps({"ok": True, "preview": "root:x:0:0\n", "size": 9})
    if "is-active" in cmd:
        return "active"
    if "ActiveEnterTimestamp" in cmd:
        return "Fri 2026-11-20 00:25:18 MSK"
    if "http_code" in cmd:
        return "200"
    if "ufw status" in cmd:
        return "Status: active"
    return "ok"

# ---------------------------------------------------------------- recording
KEEP = ("parse_mode", "reply_markup", "chat_id", "show_alert")


class Recorder:
    def __init__(self):
        self.calls = []

    def add(self, api, text=None, **kw):
        entry = {"api": api, "text": text}
        for key in KEEP:
            if key in kw:
                entry[key] = kw[key]
        self.calls.append(entry)

    def texts(self):
        return [c.get("text") for c in self.calls if c.get("text")]


class FakeUser:
    def __init__(self, uid=USER_ID):
        self.id = uid
        self.first_name = "Admin"
        self.username = "admin"
        self.is_bot = False


class FakeChat:
    def __init__(self, chat_id=USER_ID):
        self.id = chat_id
        self.type = "private"
        self.title = None
        self.username = "admin"

    async def send_message(self, text=None, **kw):
        FakeBot(GLOBAL_REC).add("send_message", text, chat_id=self.id, **kw)
        return FakeMessage(GLOBAL_REC, self.id, text or "")


GLOBAL_REC = None


class FakeBot:
    def __init__(self, rec=None):
        self.rec = rec or GLOBAL_REC

    async def send_message(self, chat_id=None, text=None, **kw):
        self.rec.add("send_message", text, chat_id=chat_id, **kw)
        return FakeMessage(self.rec, chat_id or USER_ID, text or "")

    async def send_photo(self, chat_id=None, photo=None, caption=None, **kw):
        self.rec.add("send_photo", caption, chat_id=chat_id, **kw)
        return FakeMessage(self.rec, chat_id or USER_ID, caption or "")

    async def send_document(self, chat_id=None, document=None, caption=None, **kw):
        self.rec.add("send_document", caption, chat_id=chat_id, **kw)
        return FakeMessage(self.rec, chat_id or USER_ID, caption or "")

    async def edit_message_text(self, text=None, chat_id=None, message_id=None, **kw):
        self.rec.add("edit_message_text", text, chat_id=chat_id, **kw)
        return FakeMessage(self.rec, chat_id or USER_ID, text or "")

    async def edit_message_reply_markup(self, chat_id=None, message_id=None, **kw):
        self.rec.add("edit_message_reply_markup", None, chat_id=chat_id, **kw)
        return True

    async def delete_message(self, chat_id=None, message_id=None, **kw):
        self.rec.add("delete_message", None, chat_id=chat_id, **kw)
        return True

    async def answer_callback_query(self, callback_query_id=None, text=None, **kw):
        self.rec.add("answer_callback_query", text, **kw)
        return True

    async def send_chat_action(self, chat_id=None, action=None, **kw):
        self.rec.add("send_chat_action", None, chat_id=chat_id, **kw)
        return True

    async def set_my_commands(self, commands, **kw):
        self.rec.add("set_my_commands", None, **kw)
        return True

    async def get_me(self, **kw):
        return FakeUser(0)

    async def get_chat(self, chat_id=None, **kw):
        return FakeChat(chat_id)


class FakeMessage:
    def __init__(self, rec, chat_id=USER_ID, text="", message_id=1):
        self.rec = rec
        self.chat_id = chat_id
        self.chat = FakeChat(chat_id)
        self.text = text
        self.caption = None
        self.message_id = message_id
        self.from_user = FakeUser(USER_ID)
        self.date = None

    async def reply_text(self, text=None, **kw):
        self.rec.add("reply_text", text, chat_id=self.chat_id, **kw)
        return FakeMessage(self.rec, self.chat_id, text or "")

    async def edit_text(self, text=None, **kw):
        self.rec.add("edit_text", text, chat_id=self.chat_id, **kw)
        return self

    async def edit_message_text(self, text=None, **kw):
        self.rec.add("edit_message_text", text, chat_id=self.chat_id, **kw)
        return self

    reply_markdown = reply_text
    reply_html = reply_text
    reply_markdown_v2 = reply_text

    async def reply(self, text=None, **kw):
        return await self.reply_text(text, **kw)

    async def reply_photo(self, photo=None, caption=None, **kw):
        self.rec.add("reply_photo", caption, chat_id=self.chat_id, **kw)
        return FakeMessage(self.rec, self.chat_id, caption or "")

    async def reply_document(self, document=None, caption=None, **kw):
        self.rec.add("reply_document", caption, chat_id=self.chat_id, **kw)
        return FakeMessage(self.rec, self.chat_id, caption or "")

    async def reply_voice(self, voice=None, caption=None, **kw):
        self.rec.add("reply_voice", caption, chat_id=self.chat_id, **kw)
        return FakeMessage(self.rec, self.chat_id, caption or "")

    async def reply_video(self, video=None, caption=None, **kw):
        self.rec.add("reply_video", caption, chat_id=self.chat_id, **kw)
        return FakeMessage(self.rec, self.chat_id, caption or "")

    async def reply_audio(self, audio=None, caption=None, **kw):
        self.rec.add("reply_audio", caption, chat_id=self.chat_id, **kw)
        return FakeMessage(self.rec, self.chat_id, caption or "")

    async def reply_location(self, latitude=None, longitude=None, **kw):
        self.rec.add("reply_location", None, chat_id=self.chat_id, **kw)
        return FakeMessage(self.rec, self.chat_id, "")

    async def reply_sticker(self, sticker=None, **kw):
        self.rec.add("reply_sticker", None, chat_id=self.chat_id, **kw)
        return FakeMessage(self.rec, self.chat_id, "")

    async def delete(self):
        self.rec.add("delete", None, chat_id=self.chat_id)
        return True

    async def pin(self, **kw):
        self.rec.add("pin", None, chat_id=self.chat_id, **kw)
        return True

    async def copy(self, chat_id=None, **kw):
        self.rec.add("copy", self.text, chat_id=chat_id, **kw)
        return FakeMessage(self.rec, chat_id or self.chat_id, self.text)

    async def forward(self, chat_id=None, **kw):
        self.rec.add("forward", self.text, chat_id=chat_id, **kw)
        return FakeMessage(self.rec, chat_id or self.chat_id, self.text)

    async def answer(self, text=None, **kw):
        self.rec.add("answer", text, chat_id=self.chat_id, **kw)


class FakeCallbackQuery:
    def __init__(self, rec, data, user_id=USER_ID):
        self.rec = rec
        self.data = data
        self.id = "cbq:" + data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage(rec, user_id)

    async def answer(self, text=None, show_alert=False, **kw):
        self.rec.add("answer", text, show_alert=show_alert)
        return True

    async def edit_message_text(self, text=None, **kw):
        self.rec.add("edit_message_text", text, **kw)
        return self.message

    async def edit_message_reply_markup(self, **kw):
        self.rec.add("edit_message_reply_markup", None, **kw)
        return self.message

    async def delete_message(self, **kw):
        self.rec.add("delete_message", None, **kw)
        return True


class FakeJob:
    def schedule_removal(self):
        return None


class FakeJobQueue:
    def run_repeating(self, *a, **kw):
        return FakeJob()

    def run_once(self, *a, **kw):
        return FakeJob()

    def run_daily(self, *a, **kw):
        return FakeJob()

    def get_jobs_by_name(self, *a, **kw):
        return []


class FakeUpdate:
    def __init__(self, rec, text=None, data=None, user_id=USER_ID):
        self.rec = rec
        self.update_id = abs(hash((text, data))) % 1000000
        self.effective_user = FakeUser(user_id)
        self.effective_chat = FakeChat(user_id)
        self.message = None
        self.callback_query = None
        if data is not None:
            self.callback_query = FakeCallbackQuery(rec, data, user_id)
            self.effective_message = self.callback_query.message
        elif text is not None:
            self.message = FakeMessage(rec, user_id, text, message_id=2)
            self.effective_message = self.message


class FakeContext:
    def __init__(self, rec, user_data=None):
        self.rec = rec
        self.bot = FakeBot(rec)
        self.user_data = {} if user_data is None else dict(user_data)
        self.chat_data = {}
        self.bot_data = {}
        self.args = []
        self.error = None
        self.job_queue = FakeJobQueue()
        self.application = types.SimpleNamespace(
            bot=self.bot, job_queue=self.job_queue
        )
