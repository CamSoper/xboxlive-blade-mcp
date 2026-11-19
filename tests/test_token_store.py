"""Token store: atomic saves, rotation persistence, keepalive refresh."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from xboxlive_blade_mcp import token_store


def _tok(rt: str = "rt-old") -> dict:
    return {
        "token_type": "bearer",
        "expires_in": 3600,
        "scope": "Xboxlive.signin Xboxlive.offline_access",
        "access_token": "at",
        "refresh_token": rt,
        "user_id": "u1",
    }


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "tokens.json"
    monkeypatch.setenv("XBOX_CLIENT_ID", "cid")
    monkeypatch.setenv("XBOX_TOKEN_PATH", str(path))
    return path


def test_save_is_atomic_and_private(store: Path) -> None:
    from xbox.webapi.authentication.models import OAuth2TokenResponse

    token_store.save_tokens(OAuth2TokenResponse(**_tok()))
    assert json.loads(store.read_text())["refresh_token"] == "rt-old"
    assert oct(store.stat().st_mode & 0o777) == "0o600"
    assert [p.name for p in store.parent.iterdir()] == ["tokens.json"]


def test_refresh_on_disk_saves_rotated_token(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store.write_text(json.dumps(_tok()))
    sent = {}

    def fake_post(url: str, data: dict, timeout: int) -> httpx.Response:
        sent.update(data)
        return httpx.Response(200, json=_tok("rt-new"))

    monkeypatch.setattr(token_store.httpx, "post", fake_post)
    token_store.refresh_on_disk()
    assert sent["refresh_token"] == "rt-old"
    assert "client_secret" not in sent
    assert json.loads(store.read_text())["refresh_token"] == "rt-new"


def test_refresh_on_disk_failure_keeps_old_token(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store.write_text(json.dumps(_tok()))
    monkeypatch.setattr(
        token_store.httpx, "post",
        lambda *a, **k: httpx.Response(400, json={"error": "invalid_grant"}),
    )
    with pytest.raises(token_store.RefreshError):
        token_store.refresh_on_disk()
    assert json.loads(store.read_text())["refresh_token"] == "rt-old"


async def test_persisting_manager_saves_on_refresh(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from xbox.webapi.authentication.models import OAuth2TokenResponse
    from xbox.webapi.common.signed_session import SignedSession

    async def fake_request(self, data):  # noqa: ANN001
        return OAuth2TokenResponse(**_tok("rt-rotated"))

    monkeypatch.setattr(token_store.AuthenticationManager, "_oauth2_token_request", fake_request)
    async with SignedSession() as s:
        mgr = token_store.PersistingAuthManager(s, "cid", "", "http://localhost")
        mgr.oauth = OAuth2TokenResponse(**_tok())
        await mgr.refresh_oauth_token()
    assert json.loads(store.read_text())["refresh_token"] == "rt-rotated"
