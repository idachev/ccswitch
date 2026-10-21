"""OAuth helpers: extract fields, detect expiry, refresh access tokens.

Before installing an expired saved login, :func:`refresh_oauth_credentials` obtains a fresh
access token directly against ``platform.claude.com/v1/oauth/token`` using
the saved refresh token. The endpoint, client ID, request shape, and beta
here follow the ``claude`` binary (verified
by inspecting its bundled JS); if the refresh succeeds, callers should
persist the returned blob both back to its per-label store and into the
live ``Claude Code-credentials`` Keychain entry so subsequent operations
don't refresh again until the new token expires.
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
# Refresh slightly ahead of the wire-reported expiry to absorb clock skew and
# request latency.
OAUTH_EXPIRY_BUFFER_MS = 5 * 60 * 1000
REFRESH_TIMEOUT_SECONDS = 10


def login_blob(credentials: str) -> str:
    """Validate and retain only the Claude login, never MCP/plugin credentials."""
    oauth = extract_oauth_data(credentials)
    if not oauth or any(not isinstance(oauth.get(k), str) or not oauth[k] for k in ("accessToken", "refreshToken")):
        raise ValueError("Missing Claude OAuth access/refresh token; run /login and add the account again.")
    expiry = oauth.get("expiresAt")
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry) or expiry <= 0:
        raise ValueError("Missing or invalid OAuth expiry; run /login and add the account again.")
    scopes = oauth.get("scopes")
    if not isinstance(scopes, list) or not scopes or any(not isinstance(s, str) or not s for s in scopes):
        raise ValueError("Missing OAuth scopes; run /login and add the account again.")
    return json.dumps({"claudeAiOauth": oauth})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a refresh credential to a redirected endpoint.
        return None


def extract_oauth_data(credentials: str) -> dict[str, Any] | None:
    """Return the ``claudeAiOauth`` subtree from a credentials blob, or ``None``."""
    try:
        data = json.loads(credentials)
    except json.JSONDecodeError:
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    return oauth if isinstance(oauth, dict) else None


def is_oauth_token_expired(expires_at: object) -> bool:
    """Return ``True`` when ``expires_at`` (ms since epoch) is past or about to pass."""
    if not isinstance(expires_at, (int, float)) or not math.isfinite(expires_at):
        return False
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return now_ms + OAUTH_EXPIRY_BUFFER_MS >= int(expires_at)


def refresh_oauth_credentials(credentials: str) -> str | None:
    """Refresh the access token via the OAuth endpoint; return the updated blob.

    Returns ``None`` on any failure (network error, invalid_grant, malformed
    response, missing refresh token). Callers must abort the switch and suggest
    /login followed by re-adding the affected profile.
    """
    try:
        data = json.loads(credentials)
    except json.JSONDecodeError:
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return None
    refresh_token = oauth.get("refreshToken")
    if not isinstance(refresh_token, str) or not refresh_token:
        return None

    body = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": oauth.get("clientId") or OAUTH_CLIENT_ID,
        }
    ).encode()
    req = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "ccswitch/0.1"},
        method="POST",
    )
    try:
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=REFRESH_TIMEOUT_SECONDS) as resp:
            resp_data = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError):
        return None

    if not isinstance(resp_data, dict):
        return None
    access = resp_data.get("access_token")
    expires_in = resp_data.get("expires_in")
    if (not isinstance(access, str) or not access or isinstance(expires_in, bool)
            or not isinstance(expires_in, (int, float)) or not math.isfinite(expires_in) or expires_in <= 0):
        return None

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    oauth["accessToken"] = access
    oauth["expiresAt"] = now_ms + int(expires_in) * 1000
    new_refresh = resp_data.get("refresh_token")
    if isinstance(new_refresh, str) and new_refresh:
        oauth["refreshToken"] = new_refresh
        oauth.pop("refreshTokenExpiresAt", None)
    refresh_expiry = resp_data.get("refresh_token_expires_in")
    if isinstance(refresh_expiry, (int, float)) and math.isfinite(refresh_expiry) and refresh_expiry > 0:
        oauth["refreshTokenExpiresAt"] = now_ms + int(refresh_expiry * 1000)
    scope_str = resp_data.get("scope")
    if isinstance(scope_str, str) and scope_str:
        oauth["scopes"] = scope_str.split()

    data["claudeAiOauth"] = oauth
    return json.dumps(data)
