"""Filesystem path resolution for ccswitch and the Claude Code config it manages.

Mirrors Claude Code's own resolution so ccswitch reads and writes the same files
Claude Code does:

- Config home: ``CLAUDE_CONFIG_DIR`` if set, else ``~/.claude``.
- Global config: ``<config_home>/.config.json`` if it exists (legacy),
  otherwise ``(CLAUDE_CONFIG_DIR or $HOME)/.claude.json``. The asymmetry is
  intentional — ``.claude.json`` sits at the home directory by default, not
  inside ``.claude/``.

ccswitch's own metadata follows the XDG Base Directory Specification:
``$XDG_CONFIG_HOME/ccswitch`` when ``XDG_CONFIG_HOME`` is set to an absolute
path, otherwise ``~/.config/ccswitch``.
"""

from __future__ import annotations

import os
import hashlib
import unicodedata
from pathlib import Path


def _claude_config_home() -> Path:
    env = _config_override()
    if env:
        return Path(env)
    return Path.home() / ".claude"


def get_claude_config_path() -> Path:
    """Return the path to Claude Code's global config file.

    Returns the legacy ``<config_home>/.config.json`` if it exists, else
    ``(CLAUDE_CONFIG_DIR or $HOME)/.claude.json``.
    """
    legacy = _claude_config_home() / ".config.json"
    if legacy.exists():
        return legacy
    env = _config_override()
    base = Path(env) if env else Path.home()
    return base / ".claude.json"


def _config_override() -> str:
    raw = unicodedata.normalize("NFC", os.environ.get("CLAUDE_CONFIG_DIR", ""))
    if raw and not Path(raw).is_absolute():
        raise ValueError("CLAUDE_CONFIG_DIR must be an absolute path.")
    return raw


def get_ccswitch_config_dir() -> Path:
    """Return the ccswitch config directory.

    ``$XDG_CONFIG_HOME/ccswitch`` when ``XDG_CONFIG_HOME`` is set to an
    absolute path (per the XDG spec), otherwise ``~/.config/ccswitch``.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    if xdg:
        xdg_path = Path(xdg)
        if xdg_path.is_absolute():
            return xdg_path / "ccswitch"
    return Path.home() / ".config" / "ccswitch"


def get_accounts_file() -> Path:
    """Return the path to ``accounts.json`` inside the ccswitch config dir."""
    return get_ccswitch_config_dir() / "accounts.json"


def _explicit_storage_path() -> str:
    raw = os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR")
    if raw is None:
        raw = os.environ.get("CLAUDE_CONFIG_DIR", "")
    raw = unicodedata.normalize("NFC", raw)
    if raw and not Path(raw).is_absolute():
        raise ValueError("CLAUDE_CONFIG_DIR and CLAUDE_SECURESTORAGE_CONFIG_DIR must be absolute paths.")
    return raw


def get_secure_storage_dir() -> Path:
    raw = _explicit_storage_path()
    return Path(raw) if raw else Path.home() / ".claude"


def get_credentials_service() -> str:
    raw = _explicit_storage_path()
    suffix = "-" + hashlib.sha256(raw.encode()).hexdigest()[:8] if raw else ""
    return "Claude Code-credentials" + suffix
