"""Live-host sandbox for the user-scenario crawl.

Mutating shell commands never reach the host; the docker SDK is wrapped so a
crawl cannot stop, restart or remove a container.
"""
import re

import harness

bot = harness.bot
GuardedClient = harness.GuardedClient


# ---------------------------------------------------------------- sandbox
BLOCKED = []

MUTATING = re.compile(
    r"(\brm\b|\brmdir\b|\bmv\b|\bchmod\b|\bchown\b|\btee\b|\bdd\b|"
    r"\breboot\b|\bshutdown\b|\bpoweroff\b|\bhalt\b|\bsuspend\b|"
    r"\bsystemctl\s+(restart|stop|start|enable|disable|reload|daemon-reload|mask|unmask)|"
    r"\bservice\s+\S+\s+(restart|stop|start)|"
    r"\bdocker\s+(restart|stop|start|run|rm|kill|build|pull|push|exec|prune|system|compose|"
    r"image\s+(rm|prune|pull|build)|container\s+(rm|prune))|"
    r"\bapt(-get)?\s|\bdpkg\s|\bpip3?\s+(install|uninstall)|"
    r"\bkill\s|\bcrontab\b|\buseradd\b|\bpasswd\b|\btruncate\b|"
    r">\s*/(?!dev/null)|\|\s*(tee|sh|bash)\b|"
    r"sqlite3\s+.*(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|VACUUM)\b|"
    r"\bINSERT\s+INTO\b|\bDELETE\s+FROM\b|\bUPDATE\s+\w+\s+SET\b|\bcommit\(\)|"
    r"-X\s+POST|--data\b)",
    re.I,
)

_real_host_run = bot.host_run


def sandbox_host_run(command, timeout=10):
    if MUTATING.search(command):
        BLOCKED.append(command)
        return "sandbox: mutating command blocked"
    return _real_host_run(command, timeout=timeout)


bot.host_run = sandbox_host_run


def setup():
    bot.host_run = sandbox_host_run
    if getattr(bot, "docker_client", None) is not None:
        bot.docker_client = GuardedClient(bot.docker_client)
    return BLOCKED
