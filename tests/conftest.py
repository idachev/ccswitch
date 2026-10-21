"""Shared pytest fixtures: isolated HOME / XDG_CONFIG_HOME / CLAUDE_CONFIG_DIR."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HOME at ``tmp_path`` and clear env vars that override config paths."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
    return tmp_path


@pytest.fixture
def tmp_ccswitch_dir(tmp_home: Path) -> Path:
    """Return ``tmp_home/.config/ccswitch`` (the default location), pre-created."""
    d = tmp_home / ".config" / "ccswitch"
    d.mkdir(parents=True, exist_ok=True)
    return d
