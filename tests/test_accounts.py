"""Tests for ccswitch.accounts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ccswitch.accounts import (
    LABEL_MAX_LENGTH,
    Account,
    AccountStore,
    suggest_label,
    validate_label,
)


# ---- validate_label ----------------------------------------------------


def test_validate_label_accepts_simple() -> None:
    validate_label("work")
    validate_label("personal")
    validate_label("user_1")
    validate_label("a.b.c")
    validate_label("a1")


def test_validate_label_rejects_empty() -> None:
    with pytest.raises(ValueError, match="empty"):
        validate_label("")


def test_validate_label_rejects_space() -> None:
    with pytest.raises(ValueError):
        validate_label("has space")


def test_validate_label_rejects_trailing_newline() -> None:
    with pytest.raises(ValueError):
        validate_label("work\n")


def test_validate_label_rejects_too_long() -> None:
    with pytest.raises(ValueError, match=str(LABEL_MAX_LENGTH)):
        validate_label("a" * (LABEL_MAX_LENGTH + 1))


def test_validate_label_accepts_max_length() -> None:
    validate_label("a" * LABEL_MAX_LENGTH)


def test_validate_label_rejects_leading_dash() -> None:
    with pytest.raises(ValueError):
        validate_label("-foo")


def test_validate_label_rejects_leading_dot() -> None:
    with pytest.raises(ValueError):
        validate_label(".foo")


def test_validate_label_rejects_shell_metachar() -> None:
    for bad in ("foo;bar", "foo|bar", "foo$bar", "foo`bar", "foo&bar", "foo/bar", "foo\\bar"):
        with pytest.raises(ValueError):
            validate_label(bad)


# ---- suggest_label ----------------------------------------------------


def test_suggest_label_email_and_org() -> None:
    assert suggest_label("user@example.com", "Example Org") == "user-example-org"


def test_suggest_label_no_org() -> None:
    assert suggest_label("foo@bar.com", "") == "foo"


def test_suggest_label_collapses_non_alphanumeric() -> None:
    assert suggest_label("foo@bar.com", "My Org & Co!") == "foo-my-org-co"


def test_suggest_label_strips_trailing_dashes() -> None:
    assert suggest_label("foo@bar.com", "Trailing!!!") == "foo-trailing"


def test_suggest_label_whitespace_only_org_treated_as_no_org() -> None:
    assert suggest_label("foo@bar.com", "  ") == "foo"


def test_suggest_label_org_with_only_non_alphanumerics_treated_as_no_org() -> None:
    assert suggest_label("foo@bar.com", "!!!") == "foo"


def test_suggest_label_uppercases_to_lowercase_org() -> None:
    assert suggest_label("foo@bar.com", "ACME") == "foo-acme"


# ---- AccountStore ----------------------------------------------------


@pytest.fixture
def store_path(tmp_ccswitch_dir: Path) -> Path:
    return tmp_ccswitch_dir / "accounts.json"


def _sample_oauth_account() -> dict:
    return {
        "accountUuid": "abc-123",
        "emailAddress": "leo@example.com",
        "organizationUuid": "org-1",
        "organizationName": "Example",
    }


def test_empty_store_when_file_missing(store_path: Path) -> None:
    store = AccountStore(store_path)
    assert store.list() == []
    assert len(store) == 0
    assert store.get("anything") is None


def test_add_persists_and_reloads(store_path: Path) -> None:
    store = AccountStore(store_path)
    acc = Account("work", "leo@example.com", "Example", _sample_oauth_account())
    store.add(acc)

    reloaded = AccountStore(store_path)
    assert reloaded.get("work") == acc
    assert len(reloaded) == 1


def test_add_writes_expected_schema(store_path: Path) -> None:
    store = AccountStore(store_path)
    acc = Account("work", "leo@example.com", "Example", _sample_oauth_account())
    store.add(acc)

    on_disk = json.loads(store_path.read_text())
    assert on_disk == {
        "accounts": {
            "work": {
                "email": "leo@example.com",
                "organizationName": "Example",
                "oauthAccount": _sample_oauth_account(),
            }
        }
    }


def test_get_missing_returns_none(store_path: Path) -> None:
    store = AccountStore(store_path)
    store.add(Account("work", "leo@example.com", "Example", {}))
    assert store.get("not-there") is None


def test_remove_deletes_and_persists(store_path: Path) -> None:
    store = AccountStore(store_path)
    store.add(Account("work", "leo@example.com", "Example", {}))
    assert store.remove("work") is True

    reloaded = AccountStore(store_path)
    assert reloaded.get("work") is None


def test_remove_missing_is_false_no_raise(store_path: Path) -> None:
    store = AccountStore(store_path)
    assert store.remove("nothing-here") is False


def test_list_is_sorted_by_label(store_path: Path) -> None:
    store = AccountStore(store_path)
    store.add(Account("zeta", "z@x.com", "Z", {}))
    store.add(Account("alpha", "a@x.com", "A", {}))
    store.add(Account("mu", "m@x.com", "M", {}))
    assert [a.label for a in store.list()] == ["alpha", "mu", "zeta"]


def test_contains(store_path: Path) -> None:
    store = AccountStore(store_path)
    store.add(Account("work", "leo@example.com", "Example", {}))
    assert "work" in store
    assert "missing" not in store


def test_account_is_frozen() -> None:
    acc = Account("work", "leo@example.com", "Example", {})
    with pytest.raises((AttributeError, TypeError)):
        acc.label = "other"  # type: ignore[misc]


def test_add_overwrites_existing(store_path: Path) -> None:
    store = AccountStore(store_path)
    store.add(Account("work", "old@x.com", "Old", {}))
    store.add(Account("work", "new@x.com", "New", {"k": "v"}))
    got = store.get("work")
    assert got is not None
    assert got.email == "new@x.com"
    assert got.organization_name == "New"
    assert got.oauth_account == {"k": "v"}
