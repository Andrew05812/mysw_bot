"""Shared pytest fixtures. Never touches production config, Docker, SSH or Telegram.

Importing bot.py runs module-level side effects (env checks, docker.from_env,
load_runtime_configs). We set fake env, mock the docker module, and redirect the
data dir to a per-test temp dir before/after import so nothing touches /app/data.
"""
import os
import sys
import types
import tempfile

import pytest

# ---- fake env (must be set before importing bot) ----
os.environ["TELEGRAM_BOT_TOKEN"] = os.environ.get("TELEGRAM_BOT_TOKEN", "123:FAKE")
os.environ["BOT_ACCESS_PASSWORD"] = os.environ.get("BOT_ACCESS_PASSWORD", "test-password")
os.environ.setdefault("ADMIN_IDS", "")
os.environ.setdefault("WORKER_API_KEY", "a" * 64)
os.environ.setdefault("SSH_KEY_PATH", "/tmp/test_ssh_key")

# ---- mock the docker module so `import docker` / docker.from_env() are inert ----
_fake_docker = types.ModuleType("docker")

class _FakeClient:
    def ping(self):
        return True
    def containers(self, *a, **k):
        return []
    def images(self, *a, **k):
        return []

_fake_docker.from_env = lambda *a, **k: _FakeClient()
sys.modules.setdefault("docker", _fake_docker)

# ---- import bot (deps telegram/httpx/psutil must be installed) ----
import bot  # noqa: E402

# ---- redirect data dir + file paths to a temp dir for this session ----
_TMP_DATA = tempfile.mkdtemp(prefix="mybot_test_")
bot.DATA_DIR = _TMP_DATA
bot.BACKUP_CONFIG_FILE = os.path.join(_TMP_DATA, "backup_config.json")
bot.AUTOHEAL_CONFIG_FILE = os.path.join(_TMP_DATA, "autoheal_config.json")
bot.SCHEDULES_FILE = os.path.join(_TMP_DATA, "schedules.json")
bot.AUDIT_LOG_FILE = os.path.join(_TMP_DATA, "audit.log")
bot.PLUGIN_CONFIG_FILE = os.path.join(_TMP_DATA, "plugins.json")
bot.DOCKER_MONITOR_STATE_FILE = os.path.join(_TMP_DATA, "docker_monitor_state.json")
os.makedirs(_TMP_DATA, exist_ok=True)


@pytest.fixture
def tmp_data_dir():
    """A per-test temp dir; also points bot config files there."""
    d = tempfile.mkdtemp(prefix="mybot_case_")
    os.environ["MYBOT_TEST_DIR"] = d
    yield d
