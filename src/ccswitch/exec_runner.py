"""Disposable commands authenticated with setup-tokens only.

This command uses a temporary config directory; use `ccswitch use` for shared
settings and session history. Rotating browser-login tokens are never injected.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from ccswitch.accounts import AccountStore
from ccswitch.jsonio import write_json_atomic
from ccswitch.keychain import read as keychain_read


CCSWITCH_KEYCHAIN_SERVICE = "ccswitch"
SETUP_TOKEN_SERVICE = "ccswitch-token"
TMPDIR_PREFIX = "ccswitch-"

# Clear inherited OAuth settings, including legacy refresh variables, before
# injecting the setup-token. No browser-login refresh chain enters the child.
_OAUTH_ENV_VARS = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
    "CLAUDE_CODE_OAUTH_SCOPES",
)


def run_with_account(label: str, cmd_argv: list[str]) -> int:
    """Run ``cmd_argv`` as a child process scoped to the saved account ``label``.

    Returns the child's exit code. Raises ``ValueError`` if the label is
    unknown or its stored credentials are unusable — in both cases the
    global state and the temp dir are untouched.
    """
    if not cmd_argv:
        raise ValueError("exec requires a command to run after the label.")

    store = AccountStore()
    account = store.get(label)
    if account is None:
        raise ValueError(
            f"No saved account labeled {label!r}. Run `ccswitch list` to see saved labels."
        )

    # Browser-login refresh chains belong to the interactive process. A scratch
    # command must not rotate and then discard them with its temporary directory.
    setup_token = keychain_read(SETUP_TOKEN_SERVICE, label)
    if not setup_token:
        raise ValueError(f"exec requires a setup-token for {label!r}. Run `ccswitch add-token {label}`. "
                         "Browser-login refresh tokens are never handed to disposable child processes.")
    oauth_env = {"CLAUDE_CODE_OAUTH_TOKEN": setup_token}
    conflicts = [key for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                                "ANTHROPIC_PROFILE", "ANTHROPIC_FEDERATION_RULE_ID",
                                "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                                "CLAUDE_CODE_USE_MANTLE", "USE_STAGING_OAUTH", "USE_LOCAL_OAUTH")
                 if os.environ.get(key)]
    if conflicts:
        raise ValueError("exec authentication conflicts with: " + ", ".join(conflicts))

    tmpdir = tempfile.mkdtemp(prefix=TMPDIR_PREFIX)
    try:
        scratch_config = Path(tmpdir) / ".claude.json"
        write_json_atomic(scratch_config, {"oauthAccount": account.oauth_account})

        env = os.environ.copy()
        for var in _OAUTH_ENV_VARS:
            env.pop(var, None)
        env.update(oauth_env)
        env["CLAUDE_CONFIG_DIR"] = tmpdir
        env.pop("CLAUDE_SECURESTORAGE_CONFIG_DIR", None)
        env.pop("CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR", None)

        result = subprocess.run(cmd_argv, env=env)
        return result.returncode
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
