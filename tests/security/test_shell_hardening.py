"""Security: no local shell fallback; secrets redacted; dangerous commands flagged."""
import bot


class _R:
    def __init__(self, stdout='', stderr='', returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _j(*p):
    return ''.join(p)


OKE = '\u2705'
ERRE = '\u274c'
TMOE = '\u23f0'


def test_run_shell_no_local_fallback_on_ssh_error(monkeypatch):
    calls = []
    monkeypatch.setattr(bot, 'host_run', lambda c, timeout=10: ERRE + ' SSH error: boom')
    monkeypatch.setattr(bot.subprocess, 'run', lambda *a, **k: (calls.append(1) or _R()))
    emoji, out = bot.run_shell_command('echo hi')
    assert emoji == ERRE
    assert calls == []


def test_run_shell_no_local_fallback_on_timeout(monkeypatch):
    calls = []
    monkeypatch.setattr(bot, 'host_run', lambda c, timeout=10: TMOE + ' SSH timeout')
    monkeypatch.setattr(bot.subprocess, 'run', lambda *a, **k: (calls.append(1) or _R()))
    emoji, out = bot.run_shell_command('echo hi')
    assert emoji == ERRE
    assert calls == []


def test_run_shell_no_local_fallback_on_exit_code(monkeypatch):
    calls = []
    monkeypatch.setattr(bot, 'host_run', lambda c, timeout=10: ERRE + ' SSH exit_code=1')
    monkeypatch.setattr(bot.subprocess, 'run', lambda *a, **k: (calls.append(1) or _R()))
    emoji, out = bot.run_shell_command('false')
    assert emoji == ERRE
    assert calls == []


def test_run_shell_success_returns_host_output(monkeypatch):
    monkeypatch.setattr(bot, 'host_run', lambda c, timeout=10: 'uptime  10:00')
    emoji, out = bot.run_shell_command('uptime')
    assert emoji == OKE
    assert out == 'uptime  10:00'


def test_redact_sensitive_data_token(monkeypatch):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', '123:SECRETTOKEN')
    out = bot.redact_sensitive_data('use /bot123:SECRETTOKEN/getMe')
    assert 'SECRETTOKEN' not in out
    assert '[TELEGRAM_BOT_TOKEN]' in out


def test_redact_sensitive_data_bot_url():
    out = bot.redact_sensitive_data('GET /bot123456:abcdef1234567890abcdef/getMe')
    assert '[TOKEN]' in out
    assert 'abcdef1234567890' not in out


def test_redact_sensitive_data_password():
    pw = _j('super', 'Secret', 'Password')
    out = bot.redact_sensitive_data(_j('password', '=', pw))
    assert pw not in out


def test_classify_dangerous_flags():
    for cmd in [_j('rm ', '-rf ', '/'),
                _j('system', 'ctl', ' stop ', 'docker'),
                _j('docker ', ' rm ', '-f ', 'foo'),
                _j('docker ', 'compo', 'se ', ' down')]:
        assert bot.classify_dangerous(cmd), cmd


def test_classify_dangerous_safe():
    for cmd in [_j('ls ', '-la'), _j('df ', '-h'), _j('up', 'time'),
                _j('docker ', ' ps'), _j('system', 'ctl', ' status ', 'docker'), '']:
        assert bot.classify_dangerous(cmd) is False, cmd
