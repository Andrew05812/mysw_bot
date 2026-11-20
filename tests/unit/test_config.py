"""Tests for JSON/runtime config persistence and audit log."""
import os

import bot


def test_load_runtime_configs_defaults(tmp_data_dir, monkeypatch):
    monkeypatch.setattr(bot, "BACKUP_CONFIG_FILE", os.path.join(tmp_data_dir, "backup_config.json"))
    monkeypatch.setattr(bot, "AUTOHEAL_CONFIG_FILE", os.path.join(tmp_data_dir, "autoheal_config.json"))
    monkeypatch.setattr(bot, "SCHEDULES_FILE", os.path.join(tmp_data_dir, "schedules.json"))
    backup, autoheal, schedules = bot.load_runtime_configs()
    assert backup["retention_days"] == 14
    assert autoheal["enabled"] is True
    assert "docker" in autoheal["services"]
    assert isinstance(schedules, list)


def test_load_runtime_configs_invalid_json(tmp_data_dir, monkeypatch):
    p = os.path.join(tmp_data_dir, "schedules.json")
    with open(p, "w") as f:
        f.write("{not json")
    monkeypatch.setattr(bot, "SCHEDULES_FILE", p)
    monkeypatch.setattr(bot, "BACKUP_CONFIG_FILE", os.path.join(tmp_data_dir, "b.json"))
    monkeypatch.setattr(bot, "AUTOHEAL_CONFIG_FILE", os.path.join(tmp_data_dir, "a.json"))
    _, _, schedules = bot.load_runtime_configs()
    assert schedules == []


def test_append_and_read_audit(tmp_data_dir, monkeypatch):
    p = os.path.join(tmp_data_dir, "audit.log")
    monkeypatch.setattr(bot, "AUDIT_LOG_FILE", p)
    bot.append_audit(1, "test_action", status="ok", details="hello")
    bot.append_audit(None, "anon", status="error", details="boom")
    rows = bot.read_audit_tail(max_lines=10)
    assert len(rows) == 2
    assert rows[0]["action"] == "test_action"


def test_audit_details_truncated_to_4000(tmp_data_dir, monkeypatch):
    p = os.path.join(tmp_data_dir, "audit.log")
    monkeypatch.setattr(bot, "AUDIT_LOG_FILE", p)
    bot.append_audit(1, "run", details="x" * 5000)
    rows = bot.read_audit_tail()
    assert len(rows[0]["details"]) == 4000
