# ccswitch — safer OAuth switching fork

macOS CLI for switching Claude Code browser logins while retaining one config
and session directory. Fork of [lfreeman/ccswitch](https://github.com/lfreeman/ccswitch).
Requires macOS and Python 3.12+.

This fork coordinates credential writes, preserves unrelated secrets, and adds
rollback/recovery. It does not log into accounts for you or restart sessions.
Native lock paths and credential caching were inspected in Claude Code **2.1.306**.
Adoption by two real, already-running account sessions still needs an end-to-end
test. Claude Code updates can change these private implementation details.

## Install this fork

The PyPI package is the upstream implementation, not this fork. From this checkout:

```bash
uv tool install .
ccswitch --help
```

Review a pinned commit before installing updates. Installation does not enroll or
switch any accounts.

## Enroll two accounts with one ~/.claude

Use Claude Code's normal browser login, without an environment token or API key.
Let running requests finish before enrollment or switching.

```bash
# Sign into account A with /login inside Claude Code, then:
ccswitch add --label personal

# Use /login inside Claude Code to sign into account B, then:
ccswitch add --label work

# Select the saved login and allow the normal credential cache to expire:
ccswitch use personal --wait
```

Rename a saved account with `ccswitch rename work company`. This moves its saved
browser login and optional setup-token to the new label. The active Claude login
and sessions stay unchanged. Existing account labels cannot be overwritten.
Labels use letters, digits, `.`, `_`, or `-`, start with a letter or digit, and
contain at most 64 characters.

Credentials are copied before the metadata label is committed. If copying or
metadata persistence fails, the old label stays usable; retry the same rename.
An interruption can leave extra Keychain copies. If old-entry cleanup fails after
the rename, the command reports the old entry to remove in Keychain Access.

Save A immediately before signing into B, so its saved refresh token is current.
Avoid `/logout` between enrollment and switching: revocation can invalidate a
saved token. If the provider rejects an old login, use `/login` and save it again.
Do not copy the same browser-login refresh chain into another config directory
or machine that can refresh it independently.

For later changes:

```bash
ccswitch use work --wait
ccswitch use personal --wait
```

The global switch replaces only `claudeAiOauth` in Claude's Keychain entry and
`oauthAccount` in its config. MCP/plugin secrets, other config values, settings,
project files, and session history remain in place. JSON formatting can change.
All sessions using that same stored login are candidates for the change; this is
not a way to keep different accounts active in different sessions simultaneously.

Before switching away, ccswitch saves the latest active login. Selecting the
already-active account only updates its saved copy; it never restores an older
token over the live one. An expired target must refresh successfully before any
live-login changes occur. Refresh failure aborts the switch.

## Running sessions and the 30-second wait

Claude Code 2.1.306 contains a 30-second credential cache and checks storage for
changed credentials when creating an API client. An idle session using native
Keychain credentials is therefore expected to adopt the new token on a later
request after the cache expires. This is based on code inspection, not a completed
two-account live-session test.

`--wait` waits 30 seconds after the writes and after releasing Claude's locks.
It does not signal a process, prove which account a request used, or migrate a
request already in flight. It does not verify Remote Control or connector state.
If a session keeps its old identity or fails authentication, use `/login` or
restart it and resume its existing session.

ccswitch rejects conflicting authentication variables in its own environment,
including `CLAUDE_CODE_OAUTH_TOKEN`, API keys, custom API URLs, and cloud backends.
It cannot inspect or change environments inherited by existing Claude processes.
Settings such as `apiKeyHelper` can also select a different authentication source.
`current` and `list` show stored account metadata, not a live server-side identity
check for each running session.

## Commands

| Command | Purpose |
| --- | --- |
| `ccswitch add --label NAME` | Save the current browser login; confirm before updating an existing matching label. |
| `ccswitch list` | List saved labels and mark the stored active identity. |
| `ccswitch current` | Show the identity stored in Claude's config. |
| `ccswitch use NAME [--wait]` | Save the latest current login, then switch to NAME. |
| `ccswitch recover` | Restore the old login after an interrupted switch, when its state still matches the journal. |
| `ccswitch recover --keep-current` | Discard the recovery journal after manually repairing the login. |
| `ccswitch remove NAME` | Confirm removal of a saved login and setup-token; keep live Claude state. |
| `ccswitch add-token NAME` | Read a separate `claude setup-token` through a hidden prompt. |
| `ccswitch exec NAME -- COMMAND...` | Run a disposable command with that separate setup-token. |

`add-token` has no `--token` option: tokens must not enter shell history or process
arguments. The setup-token must belong to the intended account; ccswitch cannot
infer its account identity locally.

`exec` requires `add-token`. It never hands a browser-login refresh token to a
child that could rotate it and discard the replacement. The child receives a
temporary config directory, deleted on exit. Use `use` for shared session history;
`exec` is a separate disposable workflow. A setup-token is passed through the
child environment, so run only trusted commands.

## Failure recovery

A switch spans Keychain and a JSON file; these cannot be one atomic transaction.
Before changing the live state, ccswitch stores an encrypted-by-Keychain recovery
journal. A normal write failure triggers conditional rollback. A crash or failed
rollback leaves the journal and blocks further switches or enrollment:

```bash
ccswitch recover
```

Recovery preserves unrelated config and secrets. It refuses to overwrite a login
that has changed since the failed transaction. In that case, establish the desired
account with Claude Code's `/login`, then run:

```bash
ccswitch recover --keep-current
ccswitch add --label personal
```

Use the label matching the account you actually logged into. Without a pending
journal, ordinary expired/revoked-login recovery is `/login`, then `add` again.
`/login` repairs authentication; it does not restore deleted settings or secrets.

ccswitch serializes its own writers and acquires Claude's current and legacy OAuth
refresh locks, storage-write lock, and config lock. It uses heartbeat directory
locks and checks ownership before live writes. It can reclaim a lock left by its
own dead process, but never deliberately removes a foreign lock. A contention
timeout leaves the other process alone; retry after it finishes. An interrupted
lock setup/cleanup or PID reuse can require manual inspection of a stuck lock.

There is still an unavoidable failure window if a server rotates a refresh token
and the response or subsequent Keychain persistence is lost. `/login` is the repair
path. Process suspension can also outlive native stale-lock intervals. Locks and
recovery reduce races; they cannot guarantee recovery from every system failure
or an external program that ignores these locks.

## Storage and credential handling

| Location | Contents |
| --- | --- |
| Claude config, normally `~/.claude.json` | Only `oauthAccount` is changed. |
| `~/.config/ccswitch/accounts.json` | Labels and account metadata, mode `0600`; honors absolute `XDG_CONFIG_HOME`. |
| Keychain service `ccswitch`, account label | Browser login only, without MCP/plugin secrets. |
| Keychain service `ccswitch-token`, account label | Optional separate setup-token for `exec`. |
| Keychain service `ccswitch-recovery` | Temporary login recovery journal, scoped to user, service, and config path. |
| Keychain service `Claude Code-credentials`, OS account | Claude's live credentials; preserve every field except the selected login. |

Credentials are accessed with Security.framework directly, not `security -w`
arguments. Keychain errors contain status codes rather than secret values. New
JSON temporary files are private before content is written. Expected errors and
CLI tracebacks do not display local variables.

Absolute `CLAUDE_CONFIG_DIR` and `CLAUDE_SECURESTORAGE_CONFIG_DIR` are supported.
The Keychain service suffix follows Claude's SHA-256 naming scheme; an explicitly
empty secure-storage override selects the default Keychain service. The legacy
`<config-home>/.config.json` takes precedence when present. For a shared default
`~/.claude`, leave both variables unset. Keep one ccswitch metadata directory;
labels and saved Keychain entries are global to the macOS user.

Plaintext `.credentials.json` fallback is not supported. A missing or inaccessible
Keychain entry fails closed. macOS may prompt to allow Python access to Keychain.

## Validation

```bash
uv sync --frozen
uv run pytest -q
uv run ruff check .
```

Tests use synthetic credentials and temporary config directories. They cover
rotating-token round trips, selecting the same account, failed refresh, rollback,
crash recovery, intervening config/secret updates, custom storage paths, secret-free
CLI arguments, and real competing processes for directory locks. The native
Keychain adapter was also checked on macOS with a unique synthetic entry through
create/read/update/delete; no real Claude credentials were used for that check.

The remaining acceptance test is A → B → A with two actual accounts and existing
idle Claude sessions, including another switch after a real token refresh. Check
the provider's account usage for each request; the local `current` output alone
cannot prove which account served it.
