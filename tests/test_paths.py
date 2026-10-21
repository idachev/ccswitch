"""Tests for ccswitch.paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from ccswitch.paths import (
    get_accounts_file,
    get_ccswitch_config_dir,
    get_claude_config_path,
)


def test_claude_config_path_defaults_to_home_dot_claude_json(tmp_home: Path) -> None:
    assert get_claude_config_path() == tmp_home / ".claude.json"


def test_claude_config_path_respects_claude_config_dir(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_home / "alt-config"
    override.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(override))
    assert get_claude_config_path() == override / ".claude.json"


def test_claude_config_path_returns_legacy_dot_config_json_when_present(
    tmp_home: Path,
) -> None:
    legacy_dir = tmp_home / ".claude"
    legacy_dir.mkdir()
    legacy = legacy_dir / ".config.json"
    legacy.write_text("{}")
    assert get_claude_config_path() == legacy


def test_claude_config_path_legacy_under_claude_config_dir(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_home / "alt-config"
    override.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(override))
    legacy = override / ".config.json"
    legacy.write_text("{}")
    assert get_claude_config_path() == legacy


def test_ccswitch_config_dir_defaults_to_home_dot_config_ccswitch(tmp_home: Path) -> None:
    assert get_ccswitch_config_dir() == tmp_home / ".config" / "ccswitch"


def test_ccswitch_config_dir_honors_absolute_xdg_config_home(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg = tmp_home / "xdg"
    xdg.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    assert get_ccswitch_config_dir() == xdg / "ccswitch"


def test_ccswitch_config_dir_ignores_relative_xdg_config_home(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/path")
    assert get_ccswitch_config_dir() == tmp_home / ".config" / "ccswitch"


def test_ccswitch_config_dir_ignores_empty_xdg_config_home(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    assert get_ccswitch_config_dir() == tmp_home / ".config" / "ccswitch"


def test_accounts_file_lives_inside_ccswitch_config_dir(tmp_home: Path) -> None:
    assert get_accounts_file() == tmp_home / ".config" / "ccswitch" / "accounts.json"


def test_credentials_service_tracks_config_dir(tmp_home, monkeypatch):
    import hashlib
    from ccswitch.paths import get_credentials_service
    value = str(tmp_home / "work")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", value)
    assert get_credentials_service() == "Claude Code-credentials-" + hashlib.sha256(value.encode()).hexdigest()[:8]


def test_secure_storage_override_including_explicit_default(tmp_home, monkeypatch):
    from ccswitch.paths import get_credentials_service, get_secure_storage_dir
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_home / "work"))
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "")
    assert get_credentials_service() == "Claude Code-credentials"
    assert get_secure_storage_dir() == tmp_home / ".claude"


def test_relative_storage_path_refused(monkeypatch):
    from ccswitch.paths import get_credentials_service
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "relative")
    with pytest.raises(ValueError, match="absolute"):
        get_credentials_service()


def test_relative_config_refused_even_with_absolute_storage(tmp_home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "relative")
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", str(tmp_home / "secure"))
    with pytest.raises(ValueError, match="absolute"):
        get_claude_config_path()


def test_config_and_storage_use_nfc_paths(tmp_home, monkeypatch):
    import unicodedata
    from ccswitch.paths import get_secure_storage_dir
    raw = str(tmp_home / "cafe\u0301")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", raw)
    normalized = Path(unicodedata.normalize("NFC", raw))
    assert get_secure_storage_dir() == normalized
    assert get_claude_config_path() == normalized / ".claude.json"
