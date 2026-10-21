"""Saved-login switching with refresh coordination, narrow writes, and recovery."""

from __future__ import annotations

import json
import os
import hashlib
import time
import pwd
import re
from functools import wraps
from typing import Any

from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from ccswitch.accounts import (
    Account,
    AccountStore,
    suggest_label,
    validate_label,
)
from ccswitch.jsonio import read_json, write_json_atomic
from ccswitch.keychain import read as keychain_read
from ccswitch.keychain import write as keychain_write
from ccswitch.keychain import delete as keychain_delete
from ccswitch.oauth import (
    extract_oauth_data,
    is_oauth_token_expired,
    refresh_oauth_credentials,
    login_blob,
)
from ccswitch.paths import get_claude_config_path, get_credentials_service
from ccswitch.locking import DirectoryLock, refresh_locks, storage_and_config_locks

CLAUDE_CREDENTIALS_SERVICE = "Claude Code-credentials"
CCSWITCH_KEYCHAIN_SERVICE = "ccswitch"
SETUP_TOKEN_SERVICE = "ccswitch-token"
KEYCHAIN_CACHE_WARNING_SECONDS = 30
RECOVERY_SERVICE = "ccswitch-recovery"


def serialized(method):
    @wraps(method)
    def run(self, *args, **kwargs):
        if self._operation_depth:
            return method(self, *args, **kwargs)
        with DirectoryLock(self._store.path.with_suffix(".lock")):
            self._store.reload()
            self._operation_depth += 1
            try:
                return method(self, *args, **kwargs)
            finally:
                self._operation_depth -= 1
    return run


def require_stored_login():
    overrides = [name for name in (
        "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_PROFILE",
        "ANTHROPIC_FEDERATION_RULE_ID", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_MANTLE", "USE_STAGING_OAUTH", "USE_LOCAL_OAUTH",
    ) if os.environ.get(name)]
    if overrides:
        raise ValueError("Stored-login switching conflicts with: " + ", ".join(overrides) + ". Unset these first.")
    get_credentials_service()  # Validate path spelling before any mutation.
    get_claude_config_path()


def _current_user() -> str:
    try:
        name = os.environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return "claude-code-user"
    return name if re.fullmatch(r"[a-zA-Z0-9._-]+", name) else "claude-code-user"


def _active_identity(claude_config: dict[str, Any] | None) -> tuple[str, str] | None:
    """Return ``(emailAddress, organizationUuid)`` of the active account, or ``None``."""
    if not isinstance(claude_config, dict):
        return None
    oauth = claude_config.get("oauthAccount")
    if not isinstance(oauth, dict):
        return None
    email = oauth.get("emailAddress")
    org_uuid = oauth.get("organizationUuid")
    if not isinstance(email, str) or not isinstance(org_uuid, str):
        return None
    return (email, org_uuid)


def _account_identity(account: Account) -> tuple[str, str] | None:
    oauth = account.oauth_account
    email = oauth.get("emailAddress")
    org_uuid = oauth.get("organizationUuid")
    if not isinstance(email, str) or not isinstance(org_uuid, str):
        return None
    return (email, org_uuid)


class Switcher:
    """Stateful façade over the on-disk store + Keychain + live Claude config."""

    def __init__(
        self,
        store: AccountStore | None = None,
        console: Console | None = None,
    ) -> None:
        self._store = store if store is not None else AccountStore()
        self._console = console if console is not None else Console()
        self._user = _current_user()
        self._operation_depth = 0

    @property
    def _service(self):
        return get_credentials_service()

    @property
    def _recovery_key(self):
        identity = f"{self._service}|{self._user}|{get_claude_config_path().resolve()}"
        return hashlib.sha256(identity.encode()).hexdigest()

    # ---- Prompts (extracted so tests can monkey-patch on the instance) ----

    def _prompt_text(
        self, message: str, default: str | None = None, *, password: bool = False
    ) -> str:
        return Prompt.ask(
            message, default=default, password=password, console=self._console
        )

    def _confirm(self, message: str, *, default: bool = False) -> bool:
        return Confirm.ask(message, default=default, console=self._console)

    # ---- Internal helpers --------------------------------------------------

    def _read_claude_config(self) -> dict[str, Any]:
        """Return ``~/.claude.json`` parsed, raising on missing / unparseable.

        Required for R20: ``use`` aborts cleanly when the live config is gone
        or corrupt rather than rebuilding it from scratch.
        """
        path = get_claude_config_path()
        try:
            data = read_json(path)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Cannot parse Claude config at {path}: {exc.msg}. "
                f"Refusing to overwrite — fix or remove the file first."
            ) from exc
        if data is None:
            raise FileNotFoundError(
                f"Claude config not found at {path}. Run Claude Code at least "
                f"once to sign in before using ccswitch."
            )
        if not isinstance(data, dict):
            raise ValueError("Claude config must be a JSON object.")
        return data

    def _read_active_credentials_blob(self) -> str:
        blob = keychain_read(self._service, self._user)
        if not blob:
            raise ValueError(
                f"No active Claude Code credentials found in Keychain "
                f"(service={self._service!r}, account={self._user!r}). "
                f"Sign in via Claude Code first."
            )
        return blob

    def _read_saved_blob(self, label: str) -> str:
        blob = keychain_read(CCSWITCH_KEYCHAIN_SERVICE, label)
        if not blob:
            raise ValueError(
                f"Saved Keychain entry for label {label!r} is missing. "
                f"Re-run `ccswitch add` to repair it."
            )
        try:
            json.loads(blob)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Saved Keychain entry for label {label!r} is not valid JSON: {exc.msg}."
            ) from exc
        return blob

    def _refresh_if_expired(self, label: str, blob: str) -> str:
        """Refresh the access token in ``blob`` when expired; persist back to Keychain.

        On refresh failure, abort before touching the active login.
        """
        oauth = extract_oauth_data(blob)
        if not oauth or not is_oauth_token_expired(oauth.get("expiresAt")):
            return blob
        refreshed = refresh_oauth_credentials(blob)
        if refreshed is None:
            raise ValueError(f"Refresh failed for '{label}'; active login unchanged. Run /login and add it again.")
        refreshed = login_blob(refreshed)
        if is_oauth_token_expired(extract_oauth_data(refreshed)["expiresAt"]):
            raise ValueError("Refresh returned an unusable expiry; active login unchanged.")
        # Persist refreshed credentials so the next switch/exec doesn't re-refresh.
        keychain_write(CCSWITCH_KEYCHAIN_SERVICE, label, refreshed)
        return refreshed

    def _print_active_marker_table(self, active: tuple[str, str] | None) -> None:
        table = Table(title="ccswitch accounts")
        table.add_column("Label", style="bold")
        table.add_column("Email")
        table.add_column("Organization")
        table.add_column("Active", justify="center")
        for acc in self._store.list():
            marker = ""
            if active is not None and _account_identity(acc) == active:
                marker = "[green]●[/green]"
            table.add_row(acc.label, acc.email, acc.organization_name, marker)
        self._console.print(table)

    # ---- Public commands ---------------------------------------------------

    @serialized
    def add(self, label_override: str | None = None) -> None:
        """Save the currently-active Claude Code account."""
        require_stored_login()
        config = self._read_claude_config()
        oauth = config.get("oauthAccount")
        if not isinstance(oauth, dict) or _active_identity(config) is None:
            raise ValueError(
                "Active Claude Code config has no oauthAccount section. "
                "Sign in via Claude Code first."
            )

        email = str(oauth.get("emailAddress", ""))
        org_name = str(oauth.get("organizationName", ""))
        suggested = suggest_label(email, org_name)

        if label_override is None:
            label = self._prompt_text("Label for this account", default=suggested or None)
        else:
            label = label_override
        validate_label(label)

        existing = self._store.get(label)
        if existing is not None:
            same_identity = _account_identity(existing) == _active_identity(config)
            if same_identity:
                if not self._confirm(
                    f"Label '{label}' already saved for this account. Update credentials in place?",
                    default=True,
                ):
                    self._console.print("[yellow]Cancelled.[/yellow]")
                    return
            else:
                raise ValueError(
                    f"Label {label!r} is already in use by a different account. "
                    f"Pick a different label or remove the existing one first."
                )

        with refresh_locks() as check_refresh, storage_and_config_locks() as check_storage:
            self._require_no_recovery()
            current = self._read_claude_config()
            if _active_identity(current) != _active_identity(config):
                raise ValueError("Active account changed during the prompt; retry.")
            blob = login_blob(self._read_active_credentials_blob())
            check_refresh()
            check_storage()
            self._save_login(label, current["oauthAccount"], blob)
        self._console.print(f"[green]Saved[/green] account '{label}' ({email}).")

    @serialized
    def add_token(self, label: str, token: str | None = None) -> None:
        """Attach a long-lived ``claude setup-token`` to an already-saved account.

        The setup-token is what ``exec`` injects as ``CLAUDE_CODE_OAUTH_TOKEN``;
        unlike the browser-login blob it does not share the interactive login's
        rotating refresh chain, so it keeps working when the same account is
        also live in Claude Code. ``token`` is prompted for (hidden) when not
        supplied. Raises ``ValueError`` if ``label`` is not a saved account or
        the token is empty.
        """
        if self._store.get(label) is None:
            raise ValueError(
                f"No saved account labeled {label!r}. Run `ccswitch add` first to "
                f"save the account, then attach a token with `ccswitch add-token {label}`."
            )
        if token is None:
            token = self._prompt_text(
                f"Paste a `claude setup-token` for '{label}'", password=True
            )
        token = token.strip()
        if not token:
            raise ValueError("No token provided.")
        keychain_write(SETUP_TOKEN_SERVICE, label, token)
        self._console.print(
            f"[green]Saved[/green] exec token for '{label}'. "
            f"`ccswitch exec {label} -- claude …` will now use it."
        )

    def list_accounts(self) -> None:
        """Render saved accounts with a marker on the currently-active one."""
        if len(self._store) == 0:
            self._console.print(
                "[yellow]No saved accounts.[/yellow] Run `ccswitch add` to save the "
                "currently-active Claude Code account."
            )
            return
        try:
            config = self._read_claude_config()
        except (FileNotFoundError, ValueError):
            config = None
        active = _active_identity(config) if config is not None else None
        self._print_active_marker_table(active)

    def current(self) -> None:
        """Print the currently-active Claude account."""
        try:
            config = self._read_claude_config()
        except (FileNotFoundError, ValueError) as exc:
            self._console.print(f"[red]{exc}[/red]")
            return
        oauth = config.get("oauthAccount")
        if not isinstance(oauth, dict):
            self._console.print("[yellow]No active Claude account.[/yellow]")
            return
        email = str(oauth.get("emailAddress", "(unknown email)"))
        org_name = str(oauth.get("organizationName", "(no organization)"))
        identity = _active_identity(config)
        matched_label: str | None = None
        if identity is not None:
            for acc in self._store.list():
                if _account_identity(acc) == identity:
                    matched_label = acc.label
                    break
        if matched_label is None:
            self._console.print(
                f"Active: [bold]{email}[/bold] · {org_name} "
                f"[dim](not saved — run `ccswitch add` to save it)[/dim]"
            )
        else:
            self._console.print(
                f"Active: [bold green]{matched_label}[/bold green] "
                f"({email} · {org_name})"
            )

    def _save_login(self, label, account, blob):
        keychain_write(CCSWITCH_KEYCHAIN_SERVICE, label, login_blob(blob))
        self._store.add(Account(label, account["emailAddress"], account.get("organizationName", ""), account))

    def _require_no_recovery(self):
        if keychain_read(RECOVERY_SERVICE, self._recovery_key):
            raise ValueError("An interrupted switch needs recovery. Run `ccswitch recover` first.")

    def _restore(self, journal, check):
        """Conditional rollback, preserving every field unrelated to the login."""
        current = json.loads(self._read_active_credentials_blob())
        config = self._read_claude_config()
        if (current.get("claudeAiOauth") not in (journal["oldLogin"], journal["newLogin"])
                or config.get("oauthAccount") not in (journal["oldAccount"], journal["newAccount"])):
            raise ValueError("Login changed after the interrupted switch; refusing to overwrite it. "
                             "Run /login to establish the desired account, then `ccswitch recover --keep-current`.")
        current["claudeAiOauth"] = journal["oldLogin"]
        check()
        keychain_write(self._service, self._user, json.dumps(current))
        config["oauthAccount"] = journal["oldAccount"]
        check()
        write_json_atomic(get_claude_config_path(), config)

    @serialized
    def recover(self, keep_current: bool = False):
        """Restore an interrupted transaction, or explicitly keep a repaired login."""
        require_stored_login()
        with refresh_locks() as check_refresh, storage_and_config_locks() as check_storage:
            saved = keychain_read(RECOVERY_SERVICE, self._recovery_key)
            if not saved:
                self._console.print("No interrupted switch to recover.")
                return
            if keep_current:
                # This is an explicit acknowledgement after manual /login. Do not guess identity from a token.
                login_blob(self._read_active_credentials_blob())
                if not _active_identity(self._read_claude_config()):
                    raise ValueError("Current account metadata is missing; complete /login first.")
            else:
                check_refresh()
                check_storage()
                self._restore(json.loads(saved), lambda: (check_refresh(), check_storage()))
            check_refresh()
            check_storage()
            keychain_delete(RECOVERY_SERVICE, self._recovery_key)
        self._console.print("Recovery complete. Run `ccswitch add` to update any profile repaired with /login.")

    @serialized
    def use(self, label: str, wait: bool = False) -> None:
        """Switch the stored login. Live adoption depends on each process's credential source."""
        require_stored_login()
        target = self._store.get(label)
        if target is None:
            raise ValueError(f"No saved account labeled {label!r}.")
        config = self._read_claude_config()
        self._require_no_recovery()
        identity = _active_identity(config)
        if identity is None or _account_identity(target) is None:
            raise ValueError("Missing account identity; complete /login and add the account again.")
        active = next((a for a in self._store.list() if _account_identity(a) == identity), None)
        # Never wait for human input while holding Claude's locks.
        if active is None:
            oauth = config["oauthAccount"]
            suggested = suggest_label(oauth["emailAddress"], oauth.get("organizationName", ""))
            if not self._confirm(f"Save the current account as '{suggested}' before switching?", default=True):
                self._console.print("Aborted; active login unchanged.")
                return
            self.add(label_override=suggested)
            active = self._store.get(suggested)

        with refresh_locks() as check_refresh:
            with storage_and_config_locks() as check_storage:
                self._require_no_recovery()
                config = self._read_claude_config()
                if _active_identity(config) != identity:
                    raise ValueError("Active account changed; retry.")
                live_blob = self._read_active_credentials_blob()
                old_login = json.loads(login_blob(live_blob))["claudeAiOauth"]
                check_refresh()
                check_storage()
                self._save_login(active.label, config["oauthAccount"], live_blob)
                if _account_identity(target) == identity:
                    # Never restore an older snapshot over the currently active account.
                    if target.label != active.label:
                        self._save_login(target.label, config["oauthAccount"], live_blob)
                    self._console.print(f"Already using '{label}'; saved the current login without replacing it.")
                    return
            saved_blob = login_blob(self._read_saved_blob(label))
            # Only the refresh locks are held during HTTP; MCP and config writers may proceed.
            saved_blob = self._refresh_if_expired(label, saved_blob)
            # Migrate legacy full-store snapshots to login-only profiles, even without a refresh.
            keychain_write(CCSWITCH_KEYCHAIN_SERVICE, label, saved_blob)
            new_login = json.loads(saved_blob)["claudeAiOauth"]
            with storage_and_config_locks() as check_storage:
                check_refresh()
                check_storage()
                config = self._read_claude_config()
                live = json.loads(self._read_active_credentials_blob())
                if _active_identity(config) != identity or live.get("claudeAiOauth") != old_login:
                    raise ValueError("Active login changed during preparation; no switch applied. Retry.")
                # Re-read under the locks: retain concurrent MCP/plugin and unrelated config changes.
                journal = {"oldLogin": old_login, "newLogin": new_login,
                           "oldAccount": config["oauthAccount"], "newAccount": target.oauth_account}
                keychain_write(RECOVERY_SERVICE, self._recovery_key, json.dumps(journal))
                try:
                    check_refresh()
                    check_storage()
                    live["claudeAiOauth"] = new_login
                    keychain_write(self._service, self._user, json.dumps(live))
                    check_refresh()
                    check_storage()
                    config["oauthAccount"] = target.oauth_account
                    write_json_atomic(get_claude_config_path(), config)
                    check_refresh()
                    check_storage()
                    verified = json.loads(self._read_active_credentials_blob())
                    if (verified.get("claudeAiOauth") != new_login
                            or self._read_claude_config().get("oauthAccount") != target.oauth_account):
                        raise ValueError("Stored login verification failed.")
                except BaseException:
                    try:
                        check_refresh()
                        check_storage()
                        self._restore(journal, lambda: (check_refresh(), check_storage()))
                        keychain_delete(RECOVERY_SERVICE, self._recovery_key)
                    except Exception:
                        raise ValueError("Switch interrupted and rollback could not finish. "
                                         "Recovery data remains in Keychain; run `ccswitch recover`.") from None
                    raise
                keychain_delete(RECOVERY_SERVICE, self._recovery_key)
        self._console.print(f"Stored login switched to '{label}' ({target.email}).")
        if wait:
            self._console.print("Waiting 30 seconds for normal credential cache expiry; no sessions are restarted.")
            time.sleep(KEYCHAIN_CACHE_WARNING_SECONDS)
        self._console.print("Allow ~30 seconds before continuing idle sessions. In-flight requests keep their old login. "
                            "This verifies stored credentials, not the account used by each running session. "
                            "Environment tokens, API keys and some integrations may require /login or restart.")

    @serialized
    def remove(self, label: str) -> None:
        """Delete a saved account after interactive confirmation."""
        target = self._store.get(label)
        if target is None:
            raise ValueError(f"No saved account labeled {label!r}.")

        try:
            config = self._read_claude_config()
        except (FileNotFoundError, ValueError):
            config = None
        is_active = (
            config is not None
            and _account_identity(target) == _active_identity(config)
        )
        if is_active:
            self._console.print(
                f"[yellow]Warning:[/yellow] '{label}' is currently active in "
                f"`~/.claude.json`. Removing it here does not log Claude Code out — "
                f"it only forgets ccswitch's saved copy."
            )

        if not self._confirm(
            f"Permanently remove saved account '{label}'?", default=False
        ):
            self._console.print("[yellow]Cancelled.[/yellow]")
            return

        keychain_delete(CCSWITCH_KEYCHAIN_SERVICE, label)
        keychain_delete(SETUP_TOKEN_SERVICE, label)
        self._store.remove(label)
        self._console.print(f"[green]Removed[/green] '{label}'.")
