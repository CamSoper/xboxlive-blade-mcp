"""Durable Xbox Live OAuth token store, keep-alive and device-code sign-in.

Why this exists: Microsoft rotates the MSA refresh token on every refresh, and
xbox-webapi only keeps the rotated token in memory. The server used to write
tokens.json once at startup, so a long-running container held a fresh token in
RAM while the copy on disk aged until Microsoft stopped honoring it (consumer
refresh tokens die after ~90 days unused). The next restart then failed with
"400 Bad Request" from login.live.com and needed a manual browser re-auth.

The fixes here:

* ``save_tokens`` writes atomically (temp file + rename) under a lock, so a
  crash mid-write can't leave a truncated token store.
* ``PersistingAuthManager`` saves every token Microsoft hands back, the moment
  it arrives, so disk never lags memory.
* ``start_keepalive`` refreshes the on-disk token on a timer (default every
  12 h), so an idle server still uses its refresh token well inside the
  90-day window.
* ``DeviceFlow`` is a device-code sign-in: the server asks Microsoft for a
  short code, the person opens https://www.microsoft.com/link and types it,
  and the server stores the tokens itself. No localhost redirect, no copying
  tokens.json around.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from xbox.webapi.authentication.manager import DEFAULT_SCOPES, AuthenticationManager
from xbox.webapi.authentication.models import OAuth2TokenResponse

from xboxlive_blade_mcp.models import (
    get_client_id,
    get_client_secret,
    get_token_path,
    scrub_credentials,
)

logger = logging.getLogger(__name__)

TOKEN_URL = "https://login.live.com/oauth20_token.srf"
DEVICE_CODE_URL = "https://login.live.com/oauth20_connect.srf"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------


def load_tokens() -> OAuth2TokenResponse | None:
    """Load cached OAuth tokens from disk, or None if absent/unreadable."""
    path = get_token_path()
    if not path.exists():
        return None
    try:
        return OAuth2TokenResponse(**json.loads(path.read_text()))
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to load tokens from %s: %s", path, e)
        return None


def save_tokens(tokens: OAuth2TokenResponse) -> None:
    """Atomically write tokens to disk (0600)."""
    path = get_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = tokens.model_dump_json(indent=2)
    with _lock:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tokens.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise


def token_age() -> timedelta | None:
    """How long since the on-disk token was issued (None if no token)."""
    tokens = load_tokens()
    if tokens is None:
        return None
    return datetime.now(timezone.utc) - tokens.issued


# ---------------------------------------------------------------------------
# Auth manager that never lets disk lag memory
# ---------------------------------------------------------------------------


class PersistingAuthManager(AuthenticationManager):
    """AuthenticationManager that saves every new OAuth token immediately."""

    async def request_oauth_token(self, authorization_code: str) -> OAuth2TokenResponse:
        tokens = await super().request_oauth_token(authorization_code)
        save_tokens(tokens)
        return tokens

    async def refresh_oauth_token(self) -> OAuth2TokenResponse:
        tokens = await super().refresh_oauth_token()
        if not tokens.refresh_token and self.oauth is not None:
            # Microsoft omits the refresh token when it doesn't rotate it.
            tokens.refresh_token = self.oauth.refresh_token
        save_tokens(tokens)
        return tokens


# ---------------------------------------------------------------------------
# Synchronous refresh (keep-alive)
# ---------------------------------------------------------------------------


class RefreshError(Exception):
    """Microsoft refused the refresh token."""


def _token_request(data: dict[str, str]) -> httpx.Response:
    data = dict(data)
    data["client_id"] = get_client_id()
    secret = get_client_secret()
    if secret:
        data["client_secret"] = secret
    return httpx.post(TOKEN_URL, data=data, timeout=30)


def refresh_on_disk() -> OAuth2TokenResponse:
    """Refresh the on-disk token now and save the rotated one."""
    tokens = load_tokens()
    if tokens is None or not tokens.refresh_token:
        raise RefreshError("no cached refresh token")
    resp = _token_request(
        {
            "grant_type": "refresh_token",
            "scope": " ".join(DEFAULT_SCOPES),
            "refresh_token": tokens.refresh_token,
        }
    )
    if resp.status_code != 200:
        raise RefreshError(scrub_credentials(f"{resp.status_code} {resp.text[:300]}"))
    new = OAuth2TokenResponse(**resp.json())
    if not new.refresh_token:
        new.refresh_token = tokens.refresh_token
    save_tokens(new)
    return new


def start_keepalive(interval_s: float | None = None) -> threading.Thread:
    """Refresh the stored token on a timer so it never ages out while idle."""
    if interval_s is None:
        interval_s = float(os.environ.get("XBOX_KEEPALIVE_SECONDS", str(12 * 3600)))

    def _loop() -> None:
        # First pass shortly after startup, then every interval.
        delay = 60.0
        while True:
            time.sleep(delay)
            delay = interval_s
            if load_tokens() is None:
                continue
            try:
                refresh_on_disk()
                logger.info("xbox keepalive: refresh token renewed")
            except RefreshError as e:
                logger.error(
                    "xbox keepalive: refresh failed (%s); sign in again with "
                    "the xbox_auth_start tool or `xboxlive-blade-mcp auth --device`",
                    e,
                )
            except Exception as e:  # noqa: BLE001 -- network blips, retry next round
                logger.warning("xbox keepalive: %s", scrub_credentials(str(e)))
                delay = min(interval_s, 15 * 60)

    t = threading.Thread(target=_loop, name="xbox-token-keepalive", daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Device-code sign-in
# ---------------------------------------------------------------------------


@dataclass
class DeviceFlow:
    """One device-code sign-in attempt."""

    user_code: str
    verification_uri: str
    device_code: str = field(repr=False)
    interval: int
    expires_at: float
    status: str = "pending"  # pending | done | failed | expired
    detail: str = ""

    @classmethod
    def start(cls) -> DeviceFlow:
        resp = httpx.post(
            DEVICE_CODE_URL,
            data={
                "client_id": get_client_id(),
                "scope": " ".join(DEFAULT_SCOPES),
                "response_type": "device_code",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            hint = ""
            if "mobile" in resp.text:
                hint = (
                    " -- turn on 'Allow public client flows' in the Azure app "
                    "registration's Authentication settings, or use `xboxlive-blade-mcp auth`"
                )
            raise RefreshError(scrub_credentials(f"{resp.status_code} {resp.text[:300]}") + hint)
        d: dict[str, Any] = resp.json()
        return cls(
            user_code=d["user_code"],
            verification_uri=d.get("verification_uri", "https://www.microsoft.com/link"),
            device_code=d["device_code"],
            interval=int(d.get("interval", 5)),
            expires_at=time.time() + int(d.get("expires_in", 900)),
        )

    def poll_once(self) -> str:
        """One token poll. Updates and returns status."""
        if self.status != "pending":
            return self.status
        if time.time() > self.expires_at:
            self.status = "expired"
            return self.status
        resp = _token_request({"grant_type": DEVICE_GRANT, "device_code": self.device_code})
        if resp.status_code == 200:
            save_tokens(OAuth2TokenResponse(**resp.json()))
            self.status = "done"
            return self.status
        try:
            err = resp.json().get("error", "")
        except ValueError:
            err = ""
        if err == "authorization_pending":
            return self.status
        if err == "slow_down":
            self.interval += 5
            return self.status
        if err in ("expired_token", "authorization_declined", "bad_verification_code"):
            self.status = "expired" if err == "expired_token" else "failed"
        else:
            self.status = "failed"
        self.detail = scrub_credentials(f"{resp.status_code} {resp.text[:300]}")
        return self.status

    def wait(self) -> str:
        """Block until the flow finishes."""
        while self.poll_once() == "pending":
            time.sleep(self.interval)
        return self.status

    def run_in_background(self) -> threading.Thread:
        t = threading.Thread(target=self.wait, name="xbox-device-flow", daemon=True)
        t.start()
        return t
