"""Tests for ccswitch.jsonio."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from ccswitch import jsonio
from ccswitch.jsonio import read_json, write_json_atomic


def test_read_json_returns_parsed_dict(tmp_path: Path) -> None:
    p = tmp_path / "data.json"
    p.write_text('{"k": "v"}')
    assert read_json(p) == {"k": "v"}


def test_read_json_missing_returns_none(tmp_path: Path) -> None:
    assert read_json(tmp_path / "absent.json") is None


def test_read_json_malformed_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{not valid")
    with pytest.raises(json.JSONDecodeError):
        read_json(p)


def test_write_json_atomic_writes_and_sets_mode(tmp_path: Path) -> None:
    p = tmp_path / "out.json"
    write_json_atomic(p, {"a": 1})
    assert json.loads(p.read_text()) == {"a": 1}
    mode = stat.S_IMODE(p.stat().st_mode)
    assert mode == 0o600


def test_write_json_atomic_creates_parent_dirs(tmp_path: Path) -> None:
    p = tmp_path / "nested" / "deep" / "out.json"
    write_json_atomic(p, {"a": 1})
    assert p.exists()
    assert json.loads(p.read_text()) == {"a": 1}


def test_write_json_atomic_leaves_original_intact_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "out.json"
    p.write_text('{"original": true}')

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(jsonio.os, "replace", boom)

    with pytest.raises(OSError, match="replace failed"):
        write_json_atomic(p, {"new": True})

    # Original survives.
    assert json.loads(p.read_text()) == {"original": True}
    # Temp file cleaned up.
    leftovers = [child for child in tmp_path.iterdir() if child.name.endswith(".tmp")]
    assert leftovers == []


def test_write_json_atomic_custom_mode(tmp_path: Path) -> None:
    p = tmp_path / "out.json"
    write_json_atomic(p, {"a": 1}, mode=0o644)
    assert stat.S_IMODE(p.stat().st_mode) == 0o644


def test_write_json_atomic_overwrites_existing(tmp_path: Path) -> None:
    p = tmp_path / "out.json"
    p.write_text('{"old": true}')
    os.chmod(p, 0o600)
    write_json_atomic(p, {"new": True})
    assert json.loads(p.read_text()) == {"new": True}


def test_temp_file_is_private_before_replace(tmp_path, monkeypatch):
    original = jsonio.os.replace
    def replace(source, target):
        assert stat.S_IMODE(source.stat().st_mode) == 0o600
        original(source, target)
    monkeypatch.setattr(jsonio.os, "replace", replace)
    write_json_atomic(tmp_path / "private.json", {"data": "synthetic"})


def test_symlinked_config_is_updated_without_replacing_link(tmp_path):
    target = tmp_path / "target.json"
    target.write_text('{}')
    link = tmp_path / "link.json"
    link.symlink_to(target)
    write_json_atomic(link, {"updated": True})
    assert link.is_symlink()
    assert json.loads(target.read_text()) == {"updated": True}
