"""Tests for ccswitch.cli — uses typer.testing.CliRunner against fakes."""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from ccswitch import cli as cli_mod
from ccswitch.keychain import KeychainError


runner = CliRunner()


@pytest.fixture
def fake_switcher(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace ccswitch.cli.Switcher with a recording fake."""
    calls: dict[str, Any] = {"created": 0, "calls": []}

    class FakeSwitcher:
        def __init__(self) -> None:
            calls["created"] += 1

        def add(self, label_override: str | None = None) -> None:
            calls["calls"].append(("add", {"label_override": label_override}))

        def list_accounts(self) -> None:
            calls["calls"].append(("list", {}))

        def current(self) -> None:
            calls["calls"].append(("current", {}))

        def use(self, label: str, wait: bool = False) -> None:
            calls["calls"].append(("use", {"label": label, "wait": wait}))

        def recover(self, keep_current: bool = False) -> None:
            calls["calls"].append(("recover", {"keep_current": keep_current}))

        def add_token(self, label: str) -> None:
            calls["calls"].append(("add-token", {"label": label}))

        def remove(self, label: str) -> None:
            calls["calls"].append(("remove", {"label": label}))

    monkeypatch.setattr(cli_mod, "Switcher", FakeSwitcher)
    return calls


@pytest.fixture
def fake_run_with_account(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"calls": [], "return_code": 0, "raises": None}

    def fake(label: str, cmd_argv: list[str]) -> int:
        state["calls"].append({"label": label, "cmd_argv": cmd_argv})
        if state["raises"] is not None:
            raise state["raises"]
        return state["return_code"]

    monkeypatch.setattr(cli_mod, "run_with_account", fake)
    return state


# ---- help ---------------------------------------------------------------


def test_help_shows_all_subcommands() -> None:
    result = runner.invoke(cli_mod.app, ["--help"])
    assert result.exit_code == 0
    for name in ("add", "add-token", "list", "current", "use", "recover", "remove", "exec"):
        assert name in result.stdout


# ---- list / current / add / use / remove --------------------------------


def test_list_invokes_list_accounts(fake_switcher: dict) -> None:
    result = runner.invoke(cli_mod.app, ["list"])
    assert result.exit_code == 0
    assert ("list", {}) in fake_switcher["calls"]


def test_current_invokes_current(fake_switcher: dict) -> None:
    result = runner.invoke(cli_mod.app, ["current"])
    assert result.exit_code == 0
    assert ("current", {}) in fake_switcher["calls"]


def test_use_forwards_label(fake_switcher: dict) -> None:
    result = runner.invoke(cli_mod.app, ["use", "work"])
    assert result.exit_code == 0
    assert ("use", {"label": "work", "wait": False}) in fake_switcher["calls"]


def test_remove_forwards_label(fake_switcher: dict) -> None:
    result = runner.invoke(cli_mod.app, ["remove", "work"])
    assert result.exit_code == 0
    assert ("remove", {"label": "work"}) in fake_switcher["calls"]


def test_add_with_label_forwards_override(fake_switcher: dict) -> None:
    result = runner.invoke(cli_mod.app, ["add", "--label", "personal"])
    assert result.exit_code == 0
    assert ("add", {"label_override": "personal"}) in fake_switcher["calls"]


def test_add_short_label_flag(fake_switcher: dict) -> None:
    result = runner.invoke(cli_mod.app, ["add", "-l", "work"])
    assert result.exit_code == 0
    assert ("add", {"label_override": "work"}) in fake_switcher["calls"]


def test_add_without_label_passes_none(fake_switcher: dict) -> None:
    result = runner.invoke(cli_mod.app, ["add"])
    assert result.exit_code == 0
    assert ("add", {"label_override": None}) in fake_switcher["calls"]


def test_use_without_label_is_usage_error(fake_switcher: dict) -> None:
    result = runner.invoke(cli_mod.app, ["use"])
    assert result.exit_code != 0


# ---- exec ---------------------------------------------------------------


def test_exec_forwards_argv_after_double_dash(fake_run_with_account: dict) -> None:
    result = runner.invoke(cli_mod.app, ["exec", "work", "--", "echo", "hi"])
    assert result.exit_code == 0
    assert fake_run_with_account["calls"] == [{"label": "work", "cmd_argv": ["echo", "hi"]}]


def test_exec_forwards_multi_arg_command(fake_run_with_account: dict) -> None:
    result = runner.invoke(
        cli_mod.app, ["exec", "work", "--", "python", "-c", "print(1)"]
    )
    assert result.exit_code == 0
    assert fake_run_with_account["calls"] == [
        {"label": "work", "cmd_argv": ["python", "-c", "print(1)"]}
    ]


def test_exec_propagates_child_exit_code(fake_run_with_account: dict) -> None:
    fake_run_with_account["return_code"] = 42
    result = runner.invoke(cli_mod.app, ["exec", "work", "--", "false"])
    assert result.exit_code == 42


def test_exec_handles_value_error(fake_run_with_account: dict) -> None:
    fake_run_with_account["raises"] = ValueError("nope")
    result = runner.invoke(cli_mod.app, ["exec", "ghost", "--", "true"])
    assert result.exit_code == 1
    assert "nope" in result.stderr or "nope" in result.output


# ---- error rendering ----------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("bad input"),
        KeychainError("kc down"),
        FileNotFoundError("missing file"),
    ],
)
def test_handled_errors_render_red_and_exit_1(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    class ExplodingSwitcher:
        def use(self, _label: str) -> None:
            raise exc

    monkeypatch.setattr(cli_mod, "Switcher", lambda: ExplodingSwitcher())
    result = runner.invoke(cli_mod.app, ["use", "work"])
    assert result.exit_code == 1
    combined = (result.stdout or "") + (getattr(result, "stderr", "") or "")
    assert str(exc) in combined
    # No traceback in the rendered output.
    assert "Traceback" not in combined


def test_unhandled_exception_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    class ExplodingSwitcher:
        def use(self, _label: str) -> None:
            raise RuntimeError("unexpected")

    monkeypatch.setattr(cli_mod, "Switcher", lambda: ExplodingSwitcher())
    result = runner.invoke(cli_mod.app, ["use", "work"])
    # RuntimeError is not in _HANDLED_ERRORS — the CliRunner should surface it.
    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)


def test_use_wait_flag(fake_switcher):
    result = runner.invoke(cli_mod.app, ["use", "work", "--wait"])
    assert result.exit_code == 0
    assert ("use", {"label": "work", "wait": True}) in fake_switcher["calls"]


@pytest.mark.parametrize("keep", [False, True])
def test_recovery_modes(fake_switcher, keep):
    result = runner.invoke(cli_mod.app, ["recover"] + (["--keep-current"] if keep else []))
    assert result.exit_code == 0
    assert ("recover", {"keep_current": keep}) in fake_switcher["calls"]


def test_add_token_rejects_secret_in_arguments(fake_switcher):
    result = runner.invoke(cli_mod.app, ["add-token", "work", "--token", "synthetic-secret"])
    assert result.exit_code == 2
    assert not fake_switcher["calls"]


def test_add_token_uses_prompt_path(fake_switcher):
    result = runner.invoke(cli_mod.app, ["add-token", "work"])
    assert result.exit_code == 0
    assert ("add-token", {"label": "work"}) in fake_switcher["calls"]
