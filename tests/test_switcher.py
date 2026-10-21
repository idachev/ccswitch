"""Regression coverage for live-login switching. No real Keychain or HTTP calls."""
import io
import json
from types import SimpleNamespace

import pytest
from rich.console import Console

from ccswitch import switcher as mod
from ccswitch.accounts import Account, AccountStore
from ccswitch.keychain import KeychainError
from ccswitch.oauth import login_blob


def blob(token, **extra):
    return json.dumps({"claudeAiOauth": {"accessToken": token, "refreshToken": token + "-refresh",
                                       "expiresAt": 99999999999999, "scopes": ["user:inference"]}, **extra})


@pytest.fixture
def state(tmp_home, monkeypatch):
    config = tmp_home / ".claude.json"
    accounts = {name: {"emailAddress": name + "@example.invalid", "organizationUuid": name,
                      "accountUuid": name} for name in ("a", "b")}
    config.write_text(json.dumps({"oauthAccount": accounts["a"], "projects": {"old": {}}}))
    store = AccountStore(tmp_home / "accounts.json")
    for name, data in accounts.items():
        store.add(Account(name, data["emailAddress"], name, data))
    live = (mod.CLAUDE_CREDENTIALS_SERVICE, "review-user")
    def saved(name):
        return mod.CCSWITCH_KEYCHAIN_SERVICE, name
    entries = {live: blob("a-live"), saved("a"): blob("a-saved"), saved("b"): blob("b-saved")}
    monkeypatch.setattr(mod, "_current_user", lambda: "review-user")
    monkeypatch.setattr(mod, "keychain_read", lambda service, account: entries.get((service, account)))
    monkeypatch.setattr(mod, "keychain_write", lambda service, account, value: entries.__setitem__((service, account), value))
    monkeypatch.setattr(mod, "keychain_delete", lambda service, account: entries.pop((service, account), None))
    monkeypatch.setattr(mod, "refresh_oauth_credentials", lambda value: pytest.fail("Unexpected refresh"))
    output = io.StringIO()
    sw = mod.Switcher(store, Console(file=output, force_terminal=False))
    return SimpleNamespace(config=config, accounts=accounts, entries=entries, live=live, saved=saved,
                           sw=sw, output=output, store=store, root=tmp_home / ".claude")


def active(state):
    return json.loads(state.entries[state.live])["claudeAiOauth"]["accessToken"]


def test_same_account_keeps_live_rotated_token(state):
    state.sw.use("a")
    assert active(state) == "a-live"
    assert state.entries[state.saved("a")] == login_blob(state.entries[state.live])


def test_switch_and_back_preserve_freshest_login_and_other_secrets(state):
    extra = {"mcpOAuth": {"server": "current"}, "pluginSecrets": {"plugin": "current"}}
    state.entries[state.live] = blob("a-live", **extra)
    state.entries[state.saved("b")] = blob("b-saved", mcpOAuth={"server": "obsolete"})
    state.sw.use("b")
    assert active(state) == "b-saved"
    assert json.loads(state.entries[state.live])["mcpOAuth"] == extra["mcpOAuth"]
    assert json.loads(state.entries[state.live])["pluginSecrets"] == extra["pluginSecrets"]
    assert set(json.loads(state.entries[state.saved("a")])) == {"claudeAiOauth"}
    assert json.loads(state.config.read_text())["projects"] == {"old": {}}
    state.entries[state.live] = blob("b-rotated", **extra)
    state.sw.use("a")
    assert active(state) == "a-live"
    state.sw.use("b")
    assert active(state) == "b-rotated"


def test_refresh_uses_locks_and_retains_concurrent_mcp_and_config_updates(state, monkeypatch):
    old = json.loads(state.entries[state.saved("b")])
    old["claudeAiOauth"]["expiresAt"] = 1
    state.entries[state.saved("b")] = json.dumps(old)
    def refresh(value):
        assert (state.root / ".oauth_refresh.lock").is_dir()
        assert state.root.parent.joinpath(state.root.name + ".lock").is_dir()
        assert not (state.root / ".storage-write.lock").exists()
        assert not state.config.with_suffix(".json.lock").exists()
        data = json.loads(state.config.read_text())
        data["projects"]["new-project"] = {"trusted": True}
        state.config.write_text(json.dumps(data))
        state.entries[state.live] = blob("a-live", mcpOAuth={"server": "refreshed"})
        return blob("b-refreshed")
    monkeypatch.setattr(mod, "refresh_oauth_credentials", refresh)
    state.sw.use("b")
    assert active(state) == "b-refreshed"
    assert json.loads(state.config.read_text())["projects"]["new-project"] == {"trusted": True}
    assert json.loads(state.entries[state.live])["mcpOAuth"]["server"] == "refreshed"


def test_keychain_failure_does_not_change_config(state, monkeypatch):
    original = mod.keychain_write
    failed = False
    def write(service, account, value):
        nonlocal failed
        if (service, account) == state.live and not failed:
            failed = True
            raise KeychainError("Simulated denied write")
        original(service, account, value)
    monkeypatch.setattr(mod, "keychain_write", write)
    with pytest.raises(KeychainError):
        state.sw.use("b")
    assert json.loads(state.config.read_text())["oauthAccount"] == state.accounts["a"]
    assert active(state) == "a-live"
    assert (mod.RECOVERY_SERVICE, state.sw._recovery_key) not in state.entries


def test_config_failure_rolls_back_keychain(state, monkeypatch):
    original = mod.write_json_atomic
    failed = False
    def write(path, value):
        nonlocal failed
        if path == state.config and not failed:
            failed = True
            raise OSError("Simulated config failure")
        original(path, value)
    monkeypatch.setattr(mod, "write_json_atomic", write)
    with pytest.raises(OSError):
        state.sw.use("b")
    assert active(state) == "a-live"
    assert json.loads(state.config.read_text())["oauthAccount"] == state.accounts["a"]


def test_failed_rollback_is_recoverable_and_blocks_another_switch(state, monkeypatch):
    original = mod.write_json_atomic
    monkeypatch.setattr(mod, "write_json_atomic", lambda *args: (_ for _ in ()).throw(OSError("disk failure")))
    with pytest.raises(ValueError, match="Recovery data"):
        state.sw.use("b")
    assert (mod.RECOVERY_SERVICE, state.sw._recovery_key) in state.entries
    with pytest.raises(ValueError, match="recover"):
        state.sw.use("b")
    monkeypatch.setattr(mod, "write_json_atomic", original)
    state.sw.recover()
    assert active(state) == "a-live"
    assert (mod.RECOVERY_SERVICE, state.sw._recovery_key) not in state.entries


def seed_journal(state):
    state.entries[mod.RECOVERY_SERVICE, state.sw._recovery_key] = json.dumps({
        "oldLogin": json.loads(blob("a-live"))["claudeAiOauth"],
        "newLogin": json.loads(blob("b-saved"))["claudeAiOauth"],
        "oldAccount": state.accounts["a"], "newAccount": state.accounts["b"],
    })


def test_recover_refuses_to_overwrite_later_login(state):
    seed_journal(state)
    state.entries[state.live] = blob("manual-new-login")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        state.sw.recover()
    assert active(state) == "manual-new-login"
    state.sw.recover(keep_current=True)
    assert active(state) == "manual-new-login"
    assert (mod.RECOVERY_SERVICE, state.sw._recovery_key) not in state.entries


def test_crash_journal_restores_only_login_fields(state):
    seed_journal(state)
    state.entries[state.live] = blob("b-saved", pluginSecrets={"new": "keep"})
    state.sw.recover()
    assert active(state) == "a-live"
    assert json.loads(state.entries[state.live])["pluginSecrets"] == {"new": "keep"}


def test_failed_refresh_leaves_active_login_unchanged(state, monkeypatch):
    expired = json.loads(blob("b-expired"))
    expired["claudeAiOauth"]["expiresAt"] = 1
    state.entries[state.saved("b")] = json.dumps(expired)
    monkeypatch.setattr(mod, "refresh_oauth_credentials", lambda value: None)
    with pytest.raises(ValueError, match="Refresh failed"):
        state.sw.use("b")
    assert active(state) == "a-live"
    assert json.loads(state.config.read_text())["oauthAccount"] == state.accounts["a"]


@pytest.mark.parametrize("saved", ["{broken", "{}", "[]", '{"claudeAiOauth": {"accessToken": "x"}}'])
def test_invalid_saved_login_never_replaces_active(state, saved):
    state.entries[state.saved("b")] = saved
    with pytest.raises(ValueError):
        state.sw.use("b")
    assert active(state) == "a-live"


def test_missing_saved_login_never_replaces_active(state):
    del state.entries[state.saved("b")]
    with pytest.raises(ValueError, match="missing"):
        state.sw.use("b")
    assert active(state) == "a-live"


def test_custom_config_service_does_not_touch_default_keychain(state, monkeypatch, tmp_home):
    from ccswitch.paths import get_credentials_service
    alt = tmp_home / "alternate"
    alt.mkdir()
    (alt / ".claude.json").write_text(state.config.read_text())
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(alt))
    service = get_credentials_service()
    state.entries[service, "review-user"] = blob("alt-live")
    state.sw.use("b")
    assert active(state) == "a-live"
    assert json.loads(state.entries[service, "review-user"])["claudeAiOauth"]["accessToken"] == "b-saved"


@pytest.mark.parametrize("key", ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL"])
def test_environment_override_refuses_switch(state, monkeypatch, key):
    monkeypatch.setenv(key, "synthetic-value")
    before = dict(state.entries)
    with pytest.raises(ValueError, match=key):
        state.sw.use("b")
    assert state.entries == before


def test_wait_releases_all_claude_locks_before_sleep(state, monkeypatch):
    waits = []
    def sleep(seconds):
        waits.append(seconds)
        assert not (state.root / ".oauth_refresh.lock").exists()
        assert not (state.root / ".storage-write.lock").exists()
    monkeypatch.setattr(mod.time, "sleep", sleep)
    state.sw.use("b", wait=True)
    assert waits == [30]


def test_add_saves_only_login_and_refuses_label_identity_collision(state):
    state.entries[state.live] = blob("a-live", pluginSecrets={"secret": "not-a-profile"})
    state.sw.add("new-label")
    assert set(json.loads(state.entries[state.saved("new-label")])) == {"claudeAiOauth"}
    with pytest.raises(ValueError, match="different account"):
        state.sw.add("b")


def test_add_updates_credentials_after_manual_login(state):
    state.entries[state.live] = blob("a-new-login")
    state.sw._confirm = lambda *args, **kwargs: True
    state.sw.add("a")
    assert state.entries[state.saved("a")] == blob("a-new-login")


def test_unsaved_active_decline_does_not_mutate_login(state):
    state.store.remove("a")
    state.sw._confirm = lambda *args, **kwargs: False
    state.sw.use("b")
    assert active(state) == "a-live"


def test_unsaved_active_can_be_captured_before_switch(state):
    state.store.remove("a")
    state.sw._confirm = lambda *args, **kwargs: True
    state.sw.use("b")
    assert state.store.get("a") is not None
    assert active(state) == "b-saved"


def test_list_and_current_use_metadata_only(state):
    state.sw.current()
    state.sw.list_accounts()
    assert "a@example.invalid" in state.output.getvalue()
    assert "b@example.invalid" in state.output.getvalue()


def test_remove_does_not_change_active_login(state):
    state.sw._confirm = lambda *args, **kwargs: True
    state.sw.remove("a")
    assert state.store.get("a") is None
    assert active(state) == "a-live"


def test_setup_token_saved_separately(state):
    state.sw.add_token("a", "synthetic-setup-token")
    assert state.entries[mod.SETUP_TOKEN_SERVICE, "a"] == "synthetic-setup-token"
    assert active(state) == "a-live"


@pytest.mark.parametrize("label", ["a", "b"])
@pytest.mark.parametrize("with_token", [False, True])
def test_rename_preserves_login_and_moves_saved_credentials(state, label, with_token):
    original_config = state.config.read_bytes()
    original_live = state.entries[state.live]
    original_account = state.store.get(label)
    original_saved = state.entries[state.saved(label)]
    if with_token:
        state.entries[mod.SETUP_TOKEN_SERVICE, label] = "setup-synthetic"
    state.sw.rename(label, "renamed")
    reloaded = AccountStore(state.store.path)
    assert reloaded.get(label) is None
    renamed = reloaded.get("renamed")
    assert renamed.oauth_account == original_account.oauth_account
    assert renamed.email == original_account.email
    assert renamed.organization_name == original_account.organization_name
    assert state.entries[state.saved("renamed")] == original_saved
    assert state.saved(label) not in state.entries
    assert (mod.SETUP_TOKEN_SERVICE, label) not in state.entries
    assert state.entries.get((mod.SETUP_TOKEN_SERVICE, "renamed")) == (
        "setup-synthetic" if with_token else None
    )
    assert state.config.read_bytes() == original_config
    assert state.entries[state.live] == original_live
    state.sw.use("renamed")


@pytest.mark.parametrize("source,destination", [("missing", "new"), ("a", "b"), ("a", "bad name"), ("a", "new\n")])
def test_rename_rejects_invalid_names_without_mutation(state, source, destination):
    before = dict(state.entries)
    metadata = state.store.path.read_bytes()
    with pytest.raises(ValueError):
        state.sw.rename(source, destination)
    assert state.entries == before
    assert state.store.path.read_bytes() == metadata


def test_rename_same_label_does_not_access_keychain(state, monkeypatch):
    monkeypatch.setattr(mod, "keychain_read", lambda *args: pytest.fail("Unexpected Keychain access"))
    state.sw.rename("a", "a")
    assert "Already named" in state.output.getvalue()


@pytest.mark.parametrize("service", [mod.CCSWITCH_KEYCHAIN_SERVICE, mod.SETUP_TOKEN_SERVICE])
def test_rename_does_not_overwrite_orphan_destination(state, service):
    state.entries[service, "new"] = "unrelated-secret"
    before = dict(state.entries)
    with pytest.raises(ValueError, match="different entry"):
        state.sw.rename("a", "new")
    assert state.entries == before
    assert state.store.get("a") is not None


@pytest.mark.parametrize("failure", ["copy", "metadata"])
def test_rename_failure_keeps_original_usable_and_allows_retry(state, monkeypatch, failure):
    state.entries[mod.SETUP_TOKEN_SERVICE, "a"] = "setup-synthetic"
    original_entries = dict(state.entries)
    original_metadata = state.store.path.read_bytes()
    with monkeypatch.context() as patch:
        if failure == "copy":
            write = mod.keychain_write
            def fail(service, account, value):
                if service == mod.SETUP_TOKEN_SERVICE:
                    raise KeychainError("denied")
                write(service, account, value)
            patch.setattr(mod, "keychain_write", fail)
        else:
            def fail():
                raise OSError("disk full")
            patch.setattr(state.store, "_save", fail)
        with pytest.raises((KeychainError, OSError)):
            state.sw.rename("a", "new")
    assert state.store.path.read_bytes() == original_metadata
    assert state.store.get("a") is not None
    assert state.store.get("new") is None
    for key, value in original_entries.items():
        assert state.entries[key] == value
    state.sw.rename("a", "new")
    assert state.store.get("new") is not None
    assert state.saved("a") not in state.entries


def test_rename_missing_saved_login_leaves_metadata_unchanged(state):
    del state.entries[state.saved("a")]
    with pytest.raises(ValueError, match="missing"):
        state.sw.rename("a", "new")
    assert state.store.get("a") is not None
    assert state.store.get("new") is None


def test_rename_cleanup_failure_reports_completed_rename(state, monkeypatch):
    state.entries[mod.SETUP_TOKEN_SERVICE, "a"] = "setup-synthetic"
    delete = mod.keychain_delete
    def fail(service, label):
        if service == mod.CCSWITCH_KEYCHAIN_SERVICE:
            raise KeychainError("denied")
        delete(service, label)
    monkeypatch.setattr(mod, "keychain_delete", fail)
    state.sw.rename("a", "new")
    assert AccountStore(state.store.path).get("new") is not None
    assert state.entries[state.saved("new")] == state.entries[state.saved("a")]
    assert (mod.SETUP_TOKEN_SERVICE, "a") not in state.entries
    assert "Rename completed" in state.output.getvalue()
