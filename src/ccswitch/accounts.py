"""Saved-account metadata: dataclass, on-disk store, label helpers.

Stored at ``~/.config/ccswitch/accounts.json``:

.. code-block:: json

    {
      "accounts": {
        "work": {
          "email": "user@example.com",
          "organizationName": "Example Org",
          "oauthAccount": { ... full subtree from ~/.claude.json ... }
        }
      }
    }

The full credential blob (access + refresh + scopes) lives in the Keychain
under service ``ccswitch``, account ``<label>`` — never in this file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ccswitch.jsonio import read_json, write_json_atomic
from ccswitch.paths import get_accounts_file

LABEL_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
LABEL_MAX_LENGTH = 64


@dataclass(frozen=True)
class Account:
    label: str
    email: str
    organization_name: str
    oauth_account: dict[str, Any] = field(default_factory=dict)


def validate_label(label: str) -> None:
    """Raise ``ValueError`` if ``label`` is not a safe identifier.

    Labels appear on command lines and as Keychain account names; restricting
    them to ``[a-zA-Z0-9._-]`` (starting alphanumeric) avoids quoting issues
    and shell-metachar surprises.
    """
    if not label:
        raise ValueError("Label must not be empty.")
    if len(label) > LABEL_MAX_LENGTH:
        raise ValueError(f"Label must be at most {LABEL_MAX_LENGTH} characters.")
    if not LABEL_PATTERN.match(label):
        raise ValueError(
            "Label must start with an alphanumeric character and contain only "
            "letters, digits, '.', '_', or '-'."
        )


def _slugify_org(name: str) -> str:
    lowered = name.lower()
    collapsed = re.sub(r"[^a-z0-9]+", "-", lowered)
    return collapsed.strip("-")


def suggest_label(email: str, organization_name: str) -> str:
    """Suggest a default label from ``email`` + optional ``organization_name``.

    Examples:
        ``("user@example.com", "Example Org")`` → ``"user-example-org"``
        ``("foo@bar.com", "")``                       → ``"foo"``
        ``("foo@bar.com", "My Org & Co!")``           → ``"foo-my-org-co"``
    """
    local = email.split("@", 1)[0]
    org_slug = _slugify_org(organization_name)
    if org_slug:
        return f"{local}-{org_slug}"
    return local


class AccountStore:
    """Wrapper around ``accounts.json``: load, mutate, persist atomically."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else get_accounts_file()
        self._accounts: dict[str, Account] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> None:
        data = read_json(self._path)
        if data is None:
            self._accounts = {}
            return
        raw = data.get("accounts", {}) if isinstance(data, dict) else {}
        loaded: dict[str, Account] = {}
        for label, row in raw.items():
            if not isinstance(row, dict):
                continue
            loaded[label] = Account(
                label=label,
                email=str(row.get("email", "")),
                organization_name=str(row.get("organizationName", "")),
                oauth_account=dict(row.get("oauthAccount", {})),
            )
        self._accounts = loaded

    def reload(self) -> None:
        self._load()

    def _save(self) -> None:
        payload = {
            "accounts": {
                acc.label: {
                    "email": acc.email,
                    "organizationName": acc.organization_name,
                    "oauthAccount": acc.oauth_account,
                }
                for acc in self._accounts.values()
            }
        }
        write_json_atomic(self._path, payload)

    def get(self, label: str) -> Account | None:
        return self._accounts.get(label)

    def list(self) -> list[Account]:
        return sorted(self._accounts.values(), key=lambda a: a.label)

    def __contains__(self, label: object) -> bool:
        return isinstance(label, str) and label in self._accounts

    def __len__(self) -> int:
        return len(self._accounts)

    def add(self, account: Account) -> None:
        self._accounts[account.label] = account
        self._save()

    def remove(self, label: str) -> bool:
        if label not in self._accounts:
            return False
        del self._accounts[label]
        self._save()
        return True
