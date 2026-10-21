"""Disposable exec must never consume the interactive OAuth refresh chain."""
import json
import subprocess
from pathlib import Path

import pytest

from ccswitch import exec_runner as mod
from ccswitch.accounts import Account, AccountStore
from ccswitch.paths import get_accounts_file


@pytest.fixture
def account(tmp_home, monkeypatch):
    store = AccountStore(get_accounts_file())
    store.add(Account("work", "work@example.invalid", "Work", {"emailAddress": "work@example.invalid"}))
    entries = {(mod.SETUP_TOKEN_SERVICE, "work"): "synthetic-setup"}
    monkeypatch.setattr(mod, "keychain_read", lambda service, name: entries.get((service, name)))
    return entries


def test_setup_exec_is_scoped_and_cleans_scratch(account, monkeypatch):
    scratch = []
    def run(argv, env):
        assert argv == ["fake-command", "arg"]
        root = Path(env["CLAUDE_CONFIG_DIR"])
        scratch.append(root)
        assert json.loads((root / ".claude.json").read_text())["oauthAccount"]["emailAddress"] == "work@example.invalid"
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "synthetic-setup"
        assert "CLAUDE_CODE_OAUTH_REFRESH_TOKEN" not in env
        assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" not in env
        assert "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR" not in env
        return subprocess.CompletedProcess(argv, 7)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "must-not-inherit")
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "/must-not-use")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR", "42")
    monkeypatch.setattr(mod.subprocess, "run", run)
    assert mod.run_with_account("work", ["fake-command", "arg"]) == 7
    assert not scratch[0].exists()


def test_browser_login_fallback_is_refused(account):
    account.clear()
    account[mod.CCSWITCH_KEYCHAIN_SERVICE, "work"] = '{"claudeAiOauth": {"refreshToken": "must-not-use"}}'
    with pytest.raises(ValueError, match="setup-token"):
        mod.run_with_account("work", ["never-start"])


def test_exec_failure_cleans_scratch(account, monkeypatch):
    scratch = []
    def run(argv, env):
        scratch.append(Path(env["CLAUDE_CONFIG_DIR"]))
        raise OSError("simulated spawn failure")
    monkeypatch.setattr(mod.subprocess, "run", run)
    with pytest.raises(OSError):
        mod.run_with_account("work", ["fake"])
    assert not scratch[0].exists()


@pytest.mark.parametrize("key", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"])
def test_exec_rejects_higher_priority_auth(account, monkeypatch, key):
    monkeypatch.setenv(key, "synthetic")
    with pytest.raises(ValueError, match=key):
        mod.run_with_account("work", ["never-start"])


def test_unknown_label(account):
    with pytest.raises(ValueError, match="No saved account"):
        mod.run_with_account("missing", ["never-start"])


def test_missing_command(account):
    with pytest.raises(ValueError, match="command"):
        mod.run_with_account("work", [])
