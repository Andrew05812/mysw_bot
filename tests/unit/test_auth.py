"""Security tests for admin authorization logic."""
import pytest
import bot

@pytest.fixture(autouse=True)
def _auth_state(monkeypatch):
    monkeypatch.setattr(bot, "AUTHENTICATED_USERS", set())
    monkeypatch.setattr(bot, "ADMIN_IDS", set())
    yield

def test_none_user_not_admin(monkeypatch):
    monkeypatch.setattr(bot, "AUTHENTICATED_USERS", {1, 2})
    assert bot.user_is_admin(None) is False

def test_unauthenticated_user_not_admin(monkeypatch):
    monkeypatch.setattr(bot, "AUTHENTICATED_USERS", {1, 2})
    assert bot.user_is_admin(99) is False

def test_admin_ids_empty_allows_authenticated(monkeypatch):
    monkeypatch.setattr(bot, "AUTHENTICATED_USERS", {1, 2})
    monkeypatch.setattr(bot, "ADMIN_IDS", set())
    assert bot.user_is_admin(1) is True
    assert bot.user_is_admin(2) is True

def test_admin_ids_restrict_to_list(monkeypatch):
    monkeypatch.setattr(bot, "AUTHENTICATED_USERS", {1, 2, 3})
    monkeypatch.setattr(bot, "ADMIN_IDS", {1})
    assert bot.user_is_admin(1) is True
    assert bot.user_is_admin(2) is False

def test_must_be_authenticated_even_if_in_admin_ids(monkeypatch):
    monkeypatch.setattr(bot, "AUTHENTICATED_USERS", {1})
    monkeypatch.setattr(bot, "ADMIN_IDS", {1, 999})
    assert bot.user_is_admin(999) is False

def test_admin_ids_parse_digit_only():
    raw = " 100, 200 ,abc,300 "
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    assert ids == {100, 200, 300}
