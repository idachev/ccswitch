"""Typer command-line surface for ccswitch.

Expected validation, filesystem, locking, and Keychain failures produce an
Error message and exit code 1. Unexpected exceptions omit local variable values
from tracebacks to avoid exposing credentials.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console

from ccswitch.exec_runner import run_with_account
from ccswitch.keychain import KeychainError
from ccswitch.locking import LockError
from ccswitch.switcher import Switcher

app = typer.Typer(
    name="ccswitch",
    help="macOS account switcher for Claude Code.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    pretty_exceptions_show_locals=False,
)

_HANDLED_ERRORS = (ValueError, KeychainError, LockError, OSError)


def _run(fn, *args, **kwargs) -> None:
    """Run ``fn`` with the user-error exceptions caught and rendered."""
    try:
        fn(*args, **kwargs)
    except _HANDLED_ERRORS as exc:
        Console(stderr=True).print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from exc


@app.command("add")
def add_cmd(
    label: Annotated[
        str | None,
        typer.Option(
            "--label",
            "-l",
            help="Label to save under. Defaults to a prompt suggesting <email-local>-<org-slug>.",
        ),
    ] = None,
) -> None:
    """Save the currently-active Claude Code account."""
    _run(Switcher().add, label_override=label)


@app.command("add-token")
def add_token_cmd(
    label: Annotated[
        str, typer.Argument(help="Saved account label to attach the setup-token to.")
    ],
) -> None:
    """Attach a long-lived `claude setup-token` to a saved account for `exec`."""
    _run(Switcher().add_token, label)


@app.command("list")
def list_cmd() -> None:
    """List saved accounts with an active marker."""
    _run(Switcher().list_accounts)


@app.command("current")
def current_cmd() -> None:
    """Show the currently-active Claude account."""
    _run(Switcher().current)


@app.command("use")
def use_cmd(
    label: Annotated[str, typer.Argument(help="Saved account label to switch to.")],
    wait: Annotated[bool, typer.Option("--wait", help="Wait 30s for normal credential cache expiry.")] = False,
) -> None:
    """Switch the global Claude Code account to the given label."""
    if wait:
        _run(Switcher().use, label, wait=True)
    else:
        _run(Switcher().use, label)


@app.command("recover")
def recover_cmd(
    keep_current: Annotated[bool, typer.Option("--keep-current", help="Keep a login repaired manually with /login.")] = False,
) -> None:
    """Recover a switch interrupted between Keychain and config writes."""
    _run(Switcher().recover, keep_current=keep_current)


@app.command("remove")
def remove_cmd(
    label: Annotated[str, typer.Argument(help="Saved account label to remove.")],
) -> None:
    """Remove a saved account after confirmation."""
    _run(Switcher().remove, label)


@app.command("rename")
def rename_cmd(
    label: Annotated[str, typer.Argument(help="Current saved account label.")],
    new_label: Annotated[str, typer.Argument(help="New saved account label.")],
) -> None:
    """Rename a saved account and its saved credentials."""
    _run(Switcher().rename, label, new_label)


@app.command(
    "exec",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    help=(
        "Run a command scoped to a saved account via env-var injection. "
        "Example: ccswitch exec work -- claude -p 'hello'"
    ),
)
def exec_cmd(
    ctx: typer.Context,
    label: Annotated[str, typer.Argument(help="Saved account label to scope the command to.")],
) -> None:
    cmd_argv = list(ctx.args)
    try:
        exit_code = run_with_account(label, cmd_argv)
    except _HANDLED_ERRORS as exc:
        Console(stderr=True).print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    raise typer.Exit(code=exit_code)


if __name__ == "__main__":  # pragma: no cover
    app()
