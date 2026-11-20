"""Security: audit/user text must not leak env secrets on normal admin actions."""
import os

import bot


def test_audit_normal_actions_do_not_leak_env_secrets(tmp_data_dir, monkeypatch):
    p = os.path.join(tmp_data_dir, "audit.log")
    monkeypatch.setattr(bot, "AUDIT_LOG_FILE", p)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    password = os.environ.get("BOT_ACCESS_PASSWORD", "")
    # realistic admin actions (the bot does not pass token/password into details)
    bot.append_audit(1, "start", status="ok", details="authenticated")
    bot.append_audit(1, "tgvideo_status", status="ok", details="ok")
    rows = bot.read_audit_tail()
    text = "\n".join(str(r) for r in rows)
    assert token not in text
    assert password not in text


def test_audit_details_stored_verbatim_and_truncated(tmp_data_dir, monkeypatch):
    # Documented behavior: append_audit stores details as-is (truncated to 4000),
    # so callers must never pass secrets in details.
    p = os.path.join(tmp_data_dir, "audit.log")
    monkeypatch.setattr(bot, "AUDIT_LOG_FILE", p)
    bot.append_audit(1, "run", details="x" * 5000)
    rows = bot.read_audit_tail()
    assert len(rows[0]["details"]) == 4000


def test_export_audit_text_contains_no_access_password(tmp_data_dir, monkeypatch):
    p = os.path.join(tmp_data_dir, "audit.log")
    monkeypatch.setattr(bot, "AUDIT_LOG_FILE", p)
    bot.append_audit(1, "login", status="ok", details="ok")
    text = bot.export_audit_text()
    assert "ACCESS_PASSWORD" not in text
    assert os.environ.get("BOT_ACCESS_PASSWORD", "") not in text


def test_sanitize_and_truncate_keep_output_tame():
    out = bot.sanitize_codeblock("a ``` b")
    assert "```" not in out
    long = "y" * 10000
    cut = bot.truncate_long_output(long, max_total=3500, head_chars=1500, tail_chars=1700)
    assert len(cut) < 3600
