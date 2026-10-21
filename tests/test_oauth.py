"""Tests for ccswitch.oauth."""

from __future__ import annotations

import io
import json
import urllib.error
from typing import Any

import pytest

from ccswitch import oauth as oauth_mod
from ccswitch.oauth import (
    OAUTH_EXPIRY_BUFFER_MS,
    OAUTH_TOKEN_URL,
    extract_oauth_data,
    is_oauth_token_expired,
    refresh_oauth_credentials,
)


def _blob(
    access: str = "access-old",
    refresh: str = "refresh-tok",
    expires_at: int = 0,
    scopes: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": access,
                "refreshToken": refresh,
                "expiresAt": expires_at,
                "scopes": scopes if scopes is not None else ["user:inference", "user:profile"],
            }
        }
    )


# ---- extract_oauth_data ------------------------------------------------


def test_extract_oauth_data_happy() -> None:
    data = extract_oauth_data(_blob())
    assert data is not None
    assert data["accessToken"] == "access-old"


def test_extract_oauth_data_bad_json() -> None:
    assert extract_oauth_data("{not json") is None


def test_extract_oauth_data_missing_section() -> None:
    assert extract_oauth_data(json.dumps({"other": {}})) is None


def test_extract_oauth_data_top_level_not_dict() -> None:
    assert extract_oauth_data(json.dumps([])) is None


# ---- is_oauth_token_expired --------------------------------------------


def test_is_expired_when_past() -> None:
    assert is_oauth_token_expired(1) is True  # ~epoch


def test_is_expired_when_inside_buffer() -> None:
    # expires_at == now → already inside the buffer.
    from datetime import datetime, timezone
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    assert is_oauth_token_expired(now_ms) is True


def test_is_not_expired_when_well_ahead() -> None:
    from datetime import datetime, timezone
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    future = now_ms + OAUTH_EXPIRY_BUFFER_MS * 4
    assert is_oauth_token_expired(future) is False


def test_is_not_expired_for_non_numeric() -> None:
    assert is_oauth_token_expired(None) is False
    assert is_oauth_token_expired("never") is False
    assert is_oauth_token_expired({}) is False


# ---- refresh_oauth_credentials -----------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._buf = io.BytesIO(json.dumps(payload).encode())

    def read(self) -> bytes:
        return self._buf.read()

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


def _patch_urlopen(
    monkeypatch: pytest.MonkeyPatch, *, payload: dict | None = None, exc: Exception | None = None
) -> list[urllib.request.Request]:
    captured: list[urllib.request.Request] = []

    def fake_urlopen(req: urllib.request.Request, timeout: float | None = None) -> Any:
        captured.append(req)
        if exc is not None:
            raise exc
        return _FakeResponse(payload or {})

    monkeypatch.setattr(oauth_mod.urllib.request, "build_opener", lambda *args: type("Opener", (), {"open": staticmethod(fake_urlopen)})())
    return captured


def test_refresh_returns_updated_blob_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_urlopen(
        monkeypatch,
        payload={
            "access_token": "ACCESS-NEW",
            "expires_in": 3600,
            "refresh_token": "REFRESH-NEW",
            "scope": "user:inference user:profile",
        },
    )
    refreshed = refresh_oauth_credentials(_blob(access="ACCESS-OLD", refresh="REFRESH-OLD"))
    assert refreshed is not None
    data = json.loads(refreshed)["claudeAiOauth"]
    assert data["accessToken"] == "ACCESS-NEW"
    assert data["refreshToken"] == "REFRESH-NEW"
    assert data["scopes"] == ["user:inference", "user:profile"]
    assert isinstance(data["expiresAt"], int)
    # POST to the right URL with the refresh token in the body.
    assert len(captured) == 1
    req = captured[0]
    assert req.full_url == OAUTH_TOKEN_URL
    body = json.loads(req.data.decode())
    assert body["grant_type"] == "refresh_token"
    assert body["refresh_token"] == "REFRESH-OLD"


def test_refresh_preserves_existing_refresh_token_when_server_omits_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_urlopen(
        monkeypatch,
        payload={"access_token": "NEW", "expires_in": 3600},
    )
    refreshed = refresh_oauth_credentials(_blob(refresh="REFRESH-KEPT"))
    assert refreshed is not None
    data = json.loads(refreshed)["claudeAiOauth"]
    assert data["accessToken"] == "NEW"
    assert data["refreshToken"] == "REFRESH-KEPT"


def test_refresh_preserves_existing_scopes_when_server_omits_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_urlopen(
        monkeypatch,
        payload={"access_token": "NEW", "expires_in": 3600},
    )
    refreshed = refresh_oauth_credentials(_blob(scopes=["keep", "me"]))
    assert refreshed is not None
    assert json.loads(refreshed)["claudeAiOauth"]["scopes"] == ["keep", "me"]


def test_refresh_returns_none_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    exc = urllib.error.HTTPError(OAUTH_TOKEN_URL, 400, "Bad Request", {}, io.BytesIO(b'{"error":"invalid_grant"}'))
    _patch_urlopen(monkeypatch, exc=exc)
    assert refresh_oauth_credentials(_blob()) is None


def test_refresh_returns_none_on_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_urlopen(monkeypatch, exc=urllib.error.URLError("dns fail"))
    assert refresh_oauth_credentials(_blob()) is None


def test_refresh_returns_none_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_urlopen(monkeypatch, exc=TimeoutError("slow"))
    assert refresh_oauth_credentials(_blob()) is None


def test_refresh_returns_none_when_response_missing_access_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_urlopen(monkeypatch, payload={"expires_in": 3600})
    assert refresh_oauth_credentials(_blob()) is None


def test_refresh_returns_none_when_response_not_json(monkeypatch: pytest.MonkeyPatch) -> None:
    class GarbageResponse:
        def read(self) -> bytes:
            return b"{not json"

        def __enter__(self) -> "GarbageResponse":
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

    def fake_urlopen(req: Any, timeout: float | None = None) -> Any:
        return GarbageResponse()

    monkeypatch.setattr(oauth_mod.urllib.request, "build_opener", lambda *args: type("Opener", (), {"open": staticmethod(fake_urlopen)})())
    assert refresh_oauth_credentials(_blob()) is None


def test_refresh_returns_none_when_blob_has_no_refresh_token() -> None:
    blob = json.dumps({"claudeAiOauth": {"accessToken": "a", "expiresAt": 1}})
    assert refresh_oauth_credentials(blob) is None


def test_refresh_returns_none_when_blob_is_garbage() -> None:
    assert refresh_oauth_credentials("{not json") is None


def test_refresh_redirect_is_not_followed():
    handler = oauth_mod._NoRedirect()
    assert handler.redirect_request(None, None, 307, '', {}, 'https://other.invalid') is None


@pytest.mark.parametrize('expiry', [True, float('nan'), float('inf'), 0, -1, None])
def test_saved_login_rejects_invalid_expiry(expiry):
    value = json.dumps({'claudeAiOauth': {'accessToken': 'synthetic', 'refreshToken': 'synthetic',
                                        'expiresAt': expiry, 'scopes': ['user:inference']}})
    with pytest.raises(ValueError):
        oauth_mod.login_blob(value)
