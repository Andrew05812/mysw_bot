"""Unit tests for pure helper functions (no Telegram/Docker/SSH)."""
import json
import os
import tempfile

import bot


def test_progress_bar_basic():
    out = bot.progress_bar(50, length=10)
    assert out.startswith("[")
    assert "50.0%" in out
    assert "█" in out and "░" in out


def test_progress_bar_clamps_and_invalid():
    assert "500.0%" in bot.progress_bar(500, length=10)
    assert "-5.0%" in bot.progress_bar(-5, length=10)
    assert "0.0%" in bot.progress_bar("not-a-number", length=10)


def test_task_kind_label():
    assert bot.task_kind_label({"kind": "shell"}) == "cmd"
    assert bot.task_kind_label({"kind": "backup"}) == "backup"
    assert bot.task_kind_label({"kind": "service"}) == "service"
    assert bot.task_kind_label({"kind": "apt"}) == "apt"
    assert bot.task_kind_label({"kind": "custom"}) == "custom"
    assert bot.task_kind_label({}) == "cmd"  # default shell


def test_compute_next_run_interval_min_30():
    base = 1_700_000_000
    r = bot.compute_next_run({"schedule_type": "interval", "interval_sec": 30}, base)
    assert r == base + 30
    r = bot.compute_next_run({"schedule_type": "interval", "interval_sec": 5}, base)
    assert r == base + 30  # min clamped to 30


def test_compute_next_run_daily():
    import datetime
    base = datetime.datetime(2026, 5, 12, 10, 0, 0).timestamp()
    # daily 03:00 after 10:00 -> next day 03:00
    r = bot.compute_next_run({"schedule_type": "daily", "daily_time": "03:00"}, int(base))
    expected = datetime.datetime(2026, 5, 13, 3, 0, 0).timestamp()
    assert r == int(expected)


def test_compute_next_run_daily_invalid_falls_back_03():
    import datetime
    base = datetime.datetime(2026, 5, 12, 1, 0, 0).timestamp()
    r = bot.compute_next_run({"schedule_type": "daily", "daily_time": "99:99"}, int(base))
    expected = datetime.datetime(2026, 5, 12, 3, 0, 0).timestamp()
    assert r == int(expected)


def test_compute_next_run_once():
    base = 1_700_000_000
    r = bot.compute_next_run({"schedule_type": "once", "run_at_ts": base + 60}, base)
    assert r == base + 60


def test_human_dt():
    assert bot.human_dt(None) == "—"
    assert bot.human_dt(0) == "—"
    assert bot.human_dt("not-a-ts") == "—"
    import re as _re
    assert _re.search(r"\d{2}\.\d{2} \d{2}:\d{2}", bot.human_dt(1736592000))


def test_make_task_id_unique():
    a = bot.make_task_id()
    b = bot.make_task_id()
    assert len(a) == 10 and a != b


def test_load_json_file_missing_returns_default(tmp_data_dir):
    p = os.path.join(tmp_data_dir, "nope.json")
    assert bot.load_json_file(p, []) == []


def test_save_and_load_json_roundtrip(tmp_data_dir):
    p = os.path.join(tmp_data_dir, "x.json")
    bot.save_json_file(p, {"a": 1, "b": "текст"})
    assert bot.load_json_file(p, {}) == {"a": 1, "b": "текст"}


def test_sanitize_codeblock_neutralizes_triple_backticks(tmp_data_dir):
    out = bot.sanitize_codeblock("code ``` here")
    assert "```" not in out
    assert "'''" in out



def test_truncate_long_output_short_passthrough():
    s = "x" * 100
    out = bot.truncate_long_output(s, max_total=1000, head_chars=500, tail_chars=500)
    assert out == s


def test_truncate_long_output_cuts():
    s = "a" * 5000
    out = bot.truncate_long_output(s, max_total=3500, head_chars=1500, tail_chars=1700)
    assert len(out) < 3600
