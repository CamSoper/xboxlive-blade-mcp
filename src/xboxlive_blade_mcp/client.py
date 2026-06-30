"""Xbox Live client — wraps xbox-webapi-python for MCP tool access.

Async client using xbox-webapi's provider system with automatic token refresh,
credential scrubbing, and typed exceptions.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from xbox.webapi.api.client import XboxLiveClient
from xbox.webapi.api.provider.catalog.models import AlternateIdType
from xbox.webapi.api.provider.smartglass.models import VolumeDirection
from xbox.webapi.authentication.manager import AuthenticationManager
from xbox.webapi.authentication.models import OAuth2TokenResponse
from xbox.webapi.common.signed_session import SignedSession

from xboxlive_blade_mcp.models import (
    get_client_id,
    get_client_secret,
    get_token_path,
    scrub_credentials,
)

logger = logging.getLogger(__name__)

REDIRECT_URI = "http://localhost:8400/auth/callback"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class XboxError(Exception):
    """Base exception for Xbox Live client errors."""

    def __init__(self, message: str, details: str = "") -> None:
        super().__init__(message)
        self.details = details


class AuthError(XboxError):
    """Authentication failed — tokens expired or invalid."""


class NotFoundError(XboxError):
    """Requested resource not found."""


class RateLimitError(XboxError):
    """Rate limit exceeded."""


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

_ERROR_PATTERNS: list[tuple[str, type[XboxError]]] = [
    ("unauthorized", AuthError),
    ("401", AuthError),
    ("invalid_grant", AuthError),
    ("expired", AuthError),
    ("not found", NotFoundError),
    ("404", NotFoundError),
    ("429", RateLimitError),
    ("rate limit", RateLimitError),
    ("throttle", RateLimitError),
]


def _classify_error(message: str) -> XboxError:
    """Map error message to a typed exception."""
    lower = message.lower()
    for pattern, exc_cls in _ERROR_PATTERNS:
        if pattern in lower:
            return exc_cls(scrub_credentials(message))
    return XboxError(scrub_credentials(message))


# ---------------------------------------------------------------------------
# Token persistence
# ---------------------------------------------------------------------------


def _load_tokens() -> OAuth2TokenResponse | None:
    """Load cached OAuth tokens from disk."""
    path = get_token_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return OAuth2TokenResponse(**data)
    except Exception as e:
        logger.warning("Failed to load tokens from %s: %s", path, e)
        return None


def _save_tokens(tokens: OAuth2TokenResponse) -> None:
    """Save OAuth tokens to disk."""
    path = get_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tokens.model_dump_json(indent=2))
    path.chmod(0o600)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class XboxClient:
    """Xbox Live API client wrapping xbox-webapi-python.

    Manages authentication lifecycle, provides typed methods for each MCP tool,
    and handles errors with credential scrubbing.
    """

    def __init__(self) -> None:
        self._auth_mgr: AuthenticationManager | None = None
        self._xbl: XboxLiveClient | None = None
        self._session: SignedSession | None = None

    async def _ensure_auth(self) -> XboxLiveClient:
        """Ensure we have valid auth and return the XboxLiveClient."""
        if self._xbl is not None:
            return self._xbl

        client_id = get_client_id()
        client_secret = get_client_secret()

        self._session = SignedSession()
        self._auth_mgr = AuthenticationManager(
            self._session, client_id, client_secret, REDIRECT_URI
        )

        tokens = _load_tokens()
        if tokens is None:
            raise AuthError(
                "No cached Xbox Live tokens found. "
                "Run 'xboxlive-blade-mcp auth' to authenticate via browser."
            )

        self._auth_mgr.oauth = tokens

        try:
            await self._auth_mgr.refresh_tokens()
            _save_tokens(self._auth_mgr.oauth)
        except Exception as e:
            raise AuthError(
                scrub_credentials(f"Token refresh failed: {e}. "
                                  "Run 'xboxlive-blade-mcp auth' to re-authenticate.")
            ) from e

        self._xbl = XboxLiveClient(self._auth_mgr)
        return self._xbl

    @property
    def auth_manager(self) -> AuthenticationManager | None:
        return self._auth_mgr

    # -----------------------------------------------------------------------
    # Info
    # -----------------------------------------------------------------------

    async def info(self) -> dict[str, Any]:
        """Health check: auth status, gamertag, token expiry."""
        try:
            xbl = await self._ensure_auth()
            profile = await xbl.profile.get_profile_by_xuid(str(xbl.xuid))
            settings = {}
            if profile and profile.profile_users:
                for s in profile.profile_users[0].settings:
                    settings[s.id] = s.value
            result: dict[str, Any] = {
                "authenticated": True,
                "xuid": str(xbl.xuid),
                "gamertag": settings.get("Gamertag")
                or settings.get("GameDisplayName", "unknown"),
            }
            if self._auth_mgr and self._auth_mgr.oauth:
                result["token_expires"] = str(
                    getattr(self._auth_mgr.oauth, "expires_on", "")
                )
            return result
        except XboxError:
            raise
        except Exception as e:
            raise _classify_error(str(e)) from e

    # -----------------------------------------------------------------------
    # Profile
    # -----------------------------------------------------------------------

    async def get_profile(
        self,
        gamertag: str | None = None,
        xuid: str | None = None,
    ) -> dict[str, Any]:
        """Get user profile by gamertag or XUID."""
        xbl = await self._ensure_auth()
        try:
            # xbox-webapi 2.x requests a fixed, comprehensive settings list
            # internally; the caller no longer passes one.
            if gamertag:
                resp = await xbl.profile.get_profile_by_gamertag(gamertag)
            elif xuid:
                resp = await xbl.profile.get_profile_by_xuid(xuid)
            else:
                resp = await xbl.profile.get_profile_by_xuid(str(xbl.xuid))

            if not resp or not resp.profile_users:
                raise NotFoundError(f"Profile not found: {gamertag or xuid or 'self'}")

            user = resp.profile_users[0]
            settings = {s.id: s.value for s in user.settings}
            return {
                "xuid": str(user.id),
                "gamertag": settings.get("Gamertag", settings.get("GameDisplayName", "")),
                "gamerscore": settings.get("Gamerscore", ""),
                "account_tier": settings.get("AccountTier", ""),
                "tenure_level": settings.get("TenureLevel", ""),
                "preferred_color": settings.get("PreferredColor", ""),
                "real_name": settings.get("RealNameOverride", settings.get("RealName", "")),
                "bio": settings.get("Bio", ""),
            }
        except XboxError:
            raise
        except Exception as e:
            raise _classify_error(str(e)) from e

    # -----------------------------------------------------------------------
    # Achievements
    # -----------------------------------------------------------------------

    async def get_achievements(
        self,
        xuid: str | None = None,
        title_id: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Get achievements for a game (Xbox One era)."""
        xbl = await self._ensure_auth()
        target_xuid = xuid or str(xbl.xuid)
        try:
            if title_id:
                resp = await xbl.achievements.get_achievements_xboxone_gameprogress(
                    target_xuid, title_id
                )
            else:
                resp = await xbl.achievements.get_achievements_xboxone_recent_progress_and_info(
                    target_xuid
                )

            achievements = []
            if resp and resp.achievements:
                for a in resp.achievements[:limit]:
                    # rewards is a list of Reward model objects (not dicts);
                    # the Gamerscore reward carries the point value.
                    gamerscore = 0
                    for rw in (getattr(a, "rewards", None) or []):
                        if getattr(rw, "type", "") == "Gamerscore":
                            try:
                                gamerscore = int(rw.value)
                            except (TypeError, ValueError):
                                gamerscore = 0
                            break
                    item: dict[str, Any] = {
                        "name": a.name,
                        "description": getattr(a, "description", ""),
                        "gamerscore": gamerscore,
                        "earned": a.progress_state == "Achieved",
                    }
                    if a.progression and a.progression.time_unlocked:
                        item["earned_date"] = str(a.progression.time_unlocked)
                    if getattr(a, "rarity", None):
                        rarity = a.rarity
                        if hasattr(rarity, "current_percentage"):
                            item["rare"] = rarity.current_percentage < 10
                    achievements.append(item)
            return achievements
        except XboxError:
            raise
        except Exception as e:
            raise _classify_error(str(e)) from e

    async def get_achievement_summary(
        self, xuid: str | None = None
    ) -> dict[str, Any]:
        """Get overall achievement stats."""
        xbl = await self._ensure_auth()
        target_xuid = xuid or str(xbl.xuid)
        try:
            profile = await xbl.profile.get_profile_by_xuid(target_xuid)
            settings = {}
            if profile and profile.profile_users:
                settings = {s.id: s.value for s in profile.profile_users[0].settings}

            titles = await xbl.titlehub.get_title_history(target_xuid, max_items=100)
            total_achievements = 0
            earned_achievements = 0
            if titles and titles.titles:
                for t in titles.titles:
                    if t.achievement:
                        total_achievements += getattr(t.achievement, "total_gamerscore", 0)
                        earned_achievements += getattr(t.achievement, "current_gamerscore", 0)

            return {
                "gamerscore": settings.get("Gamerscore", "0"),
                "titles_played": len(titles.titles) if titles and titles.titles else 0,
                "total_achievements": total_achievements,
                "earned_achievements": earned_achievements,
                "completion_percentage": (
                    round(earned_achievements / total_achievements * 100, 1)
                    if total_achievements > 0
                    else 0
                ),
            }
        except XboxError:
            raise
        except Exception as e:
            raise _classify_error(str(e)) from e

    # -----------------------------------------------------------------------
    # Games (TitleHub)
    # -----------------------------------------------------------------------

    async def get_games(
        self,
        xuid: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Get game library with playtime and achievement progress."""
        xbl = await self._ensure_auth()
        target_xuid = xuid or str(xbl.xuid)
        try:
            resp = await xbl.titlehub.get_title_history(target_xuid, max_items=limit)
            games = []
            if resp and resp.titles:
                for t in resp.titles[:limit]:
                    item: dict[str, Any] = {
                        "name": t.name,
                        "title_id": str(t.title_id),
                    }
                    if t.achievement:
                        item["total_gamerscore"] = getattr(t.achievement, "total_gamerscore", 0)
                        item["earned_gamerscore"] = getattr(t.achievement, "current_gamerscore", 0)
                        item["achievement_count"] = getattr(t.achievement, "total_achievements", 0)
                        item["earned_achievements"] = getattr(t.achievement, "current_achievements", 0)
                    if t.title_history:
                        item["last_played"] = str(t.title_history.last_time_played)
                    games.append(item)
            return games
        except XboxError:
            raise
        except Exception as e:
            raise _classify_error(str(e)) from e

    async def get_games_recent(
        self, xuid: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Get recently played games ordered by last session."""
        return await self.get_games(xuid=xuid, limit=limit)

    # -----------------------------------------------------------------------
    # Social (People)
    # -----------------------------------------------------------------------

    async def get_friends(self, limit: int = 25) -> list[dict[str, Any]]:
        """Get friends list with online status."""
        xbl = await self._ensure_auth()
        try:
            resp = await xbl.people.get_friends_own()
            friends = []
            if resp and resp.people:
                for p in resp.people[:limit]:
                    item: dict[str, Any] = {
                        "gamertag": p.gamertag,
                        "xuid": str(p.xuid),
                        "presence_state": getattr(p, "presence_state", "unknown"),
                    }
                    if hasattr(p, "presence_text") and p.presence_text:
                        item["current_game"] = p.presence_text
                    if hasattr(p, "gamerscore"):
                        item["gamerscore"] = p.gamerscore
                    friends.append(item)
            return friends
        except XboxError:
            raise
        except Exception as e:
            raise _classify_error(str(e)) from e

    async def get_presence(
        self, xuid: str | None = None
    ) -> dict[str, Any]:
        """Get presence status for a user."""
        xbl = await self._ensure_auth()
        target_xuid = xuid or str(xbl.xuid)
        try:
            resp = await xbl.presence.get_presence(target_xuid)
            result: dict[str, Any] = {
                "xuid": target_xuid,
                "state": getattr(resp, "state", "unknown"),
            }
            if hasattr(resp, "devices") and resp.devices:
                for device in resp.devices:
                    if hasattr(device, "titles") and device.titles:
                        for title in device.titles:
                            if hasattr(title, "name"):
                                result["current_game"] = title.name
                            if hasattr(title, "rich_presence"):
                                result["rich_presence"] = getattr(
                                    title.rich_presence, "rich_presence_string", ""
                                )
            if hasattr(resp, "last_seen") and resp.last_seen:
                result["last_seen"] = str(resp.last_seen.timestamp)
            return result
        except XboxError:
            raise
        except Exception as e:
            raise _classify_error(str(e)) from e

    async def search_users(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search for users by gamertag."""
        xbl = await self._ensure_auth()
        try:
            # Bypass the typed provider: xbox-webapi 2.1.0's UserSearchResponse
            # requires a non-null `text`, but the /suggest endpoint returns null
            # for some entries. Parse the JSON directly.
            us = xbl.usersearch
            resp = await xbl.session.get(
                us.USERSEARCH_URL + "/suggest",
                params={"q": query},
                headers=us.HEADERS_USER_SEARCH,
            )
            resp.raise_for_status()
            data = resp.json()
            results = []
            for r in (data.get("results") or [])[:limit]:
                inner = r.get("result") or {}
                gamertag = inner.get("gamertag") or r.get("text")
                xuid = inner.get("id") or ""
                if not gamertag and not xuid:
                    continue
                results.append({
                    "gamertag": gamertag or "?",
                    "xuid": str(xuid),
                })
            return results
        except XboxError:
            raise
        except Exception as e:
            raise _classify_error(str(e)) from e

    # -----------------------------------------------------------------------
    # Messages
    # -----------------------------------------------------------------------

    async def get_inbox(self, limit: int = 25) -> list[dict[str, Any]]:
        """Get message inbox."""
        xbl = await self._ensure_auth()
        try:
            resp = await xbl.message.get_inbox()
            messages = []
            if resp:
                msg_list = getattr(resp, "results", [])
                for m in msg_list[:limit]:
                    item: dict[str, Any] = {
                        "summary": getattr(m, "summary", ""),
                        "is_read": getattr(m, "is_read", None),
                        "sent": str(getattr(m, "sent", "")),
                    }
                    if hasattr(m, "header") and m.header:
                        item["sender"] = getattr(m.header, "sender", "")
                    messages.append(item)
            return messages
        except XboxError:
            raise
        except Exception as e:
            raise _classify_error(str(e)) from e

    async def send_message(self, xuids: list[str], message_text: str) -> dict[str, Any]:
        """Send a message to one or more users."""
        xbl = await self._ensure_auth()
        try:
            # 2.x send_message targets a single xuid; fan out to each recipient.
            for x in xuids:
                await xbl.message.send_message(x, message_text)
            return {"status": "sent", "recipients": len(xuids)}
        except XboxError:
            raise
        except Exception as e:
            raise _classify_error(str(e)) from e

    # -----------------------------------------------------------------------
    # Media (clips / screenshots)
    # -----------------------------------------------------------------------

    async def get_clips(
        self, xuid: str | None = None, title_id: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Get game clips."""
        xbl = await self._ensure_auth()
        target_xuid = xuid or str(xbl.xuid)
        try:
            # Bypass the typed provider: xbox-webapi 2.1.0's GameclipsResponse
            # rejects real API data (e.g. integer `state`). Parse JSON directly.
            gc = xbl.gameclips
            url = gc.GAMECLIPS_METADATA_URL + f"/users/xuid({target_xuid})"
            if title_id:
                url += f"/titles/{title_id}"
            url += "/clips"
            resp = await xbl.session.get(
                url,
                params={"skipItems": 0, "maxItems": limit},
                headers=gc.HEADERS_GAMECLIPS_METADATA,
            )
            resp.raise_for_status()
            clips = []
            for c in (resp.json().get("gameClips") or [])[:limit]:
                item: dict[str, Any] = {
                    "title_name": c.get("titleName", "?"),
                    "clip_id": c.get("gameClipId", ""),
                    "date_recorded": str(c.get("dateRecorded", "")),
                    "duration_seconds": c.get("durationInSeconds", 0),
                    "views": c.get("views", 0),
                }
                uris = c.get("gameClipUris") or []
                if uris:
                    item["uri"] = uris[0].get("uri")
                clips.append(item)
            return clips
        except XboxError:
            raise
        except Exception as e:
            raise _classify_error(str(e)) from e

    async def get_screenshots(
        self, xuid: str | None = None, title_id: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Get screenshots."""
        xbl = await self._ensure_auth()
        target_xuid = xuid or str(xbl.xuid)
        try:
            # Bypass the typed provider: xbox-webapi 2.1.0's ScreenshotResponse
            # rejects real API data (e.g. integer `state`). Parse JSON directly.
            ss = xbl.screenshots
            url = ss.SCREENSHOTS_METADATA_URL + f"/users/xuid({target_xuid})"
            if title_id:
                url += f"/titles/{title_id}"
            url += "/screenshots"
            resp = await xbl.session.get(
                url,
                params={"skipItems": 0, "maxItems": limit},
                headers=ss.HEADERS_SCREENSHOTS_METADATA,
            )
            resp.raise_for_status()
            screenshots = []
            for s in (resp.json().get("screenshots") or [])[:limit]:
                item: dict[str, Any] = {
                    "title_name": s.get("titleName", "?"),
                    "screenshot_id": s.get("screenshotId", ""),
                    "date_taken": str(s.get("dateTaken", "")),
                    "views": s.get("views", 0),
                }
                uris = s.get("screenshotUris") or []
                if uris:
                    item["uri"] = uris[0].get("uri")
                screenshots.append(item)
            return screenshots
        except XboxError:
            raise
        except Exception as e:
            raise _classify_error(str(e)) from e

    # -----------------------------------------------------------------------
    # Consoles (SmartGlass)
    # -----------------------------------------------------------------------

    async def get_consoles(self) -> list[dict[str, Any]]:
        """Get registered consoles."""
        xbl = await self._ensure_auth()
        try:
            resp = await xbl.smartglass.get_console_list()
            consoles = []
            if resp and resp.result:
                for c in resp.result:
                    item: dict[str, Any] = {
                        "name": getattr(c, "name", "?"),
                        "console_id": getattr(c, "id", ""),
                        "console_type": getattr(c, "console_type", ""),
                        "power_state": getattr(c, "power_state", "unknown"),
                        "is_on": getattr(c, "power_state", "") == "On",
                    }
                    consoles.append(item)
            return consoles
        except XboxError:
            raise
        except Exception as e:
            raise _classify_error(str(e)) from e

    async def console_command(
        self, console_id: str, command: str
    ) -> dict[str, Any]:
        """Send a SmartGlass command to a console."""
        xbl = await self._ensure_auth()
        try:
            sg = xbl.smartglass
            # xbox-webapi 2.x exposes discrete SmartGlass methods rather than a
            # single command(category, action) entry point.
            cmd_map: dict[str, Any] = {
                "power_off": lambda: sg.turn_off(console_id),
                "power_on": lambda: sg.wake_up(console_id),
                "reboot": lambda: sg.reboot(console_id),
                "mute": lambda: sg.mute(console_id),
                "unmute": lambda: sg.unmute(console_id),
                "volume_up": lambda: sg.volume(console_id, VolumeDirection.Up),
                "volume_down": lambda: sg.volume(console_id, VolumeDirection.Down),
                "play": lambda: sg.play(console_id),
                "pause": lambda: sg.pause(console_id),
                "go_home": lambda: sg.go_home(console_id),
                "go_back": lambda: sg.go_back(console_id),
            }

            if command not in cmd_map:
                available = ", ".join(sorted(cmd_map.keys()))
                raise XboxError(f"Unknown command: {command}. Available: {available}")

            await cmd_map[command]()
            return {"console_id": console_id, "command": command, "status": "sent"}
        except XboxError:
            raise
        except Exception as e:
            raise _classify_error(str(e)) from e

    # -----------------------------------------------------------------------
    # Store / Catalog
    # -----------------------------------------------------------------------

    async def search_games_catalog(
        self, query: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Search the Xbox Store catalog."""
        xbl = await self._ensure_auth()
        try:
            resp = await xbl.catalog.product_search(query, top=limit)
            products = []
            if resp and resp.results:
                for r in resp.results[:limit]:
                    # Each search result nests title/product_id under products[].
                    inner = (getattr(r, "products", None) or [None])[0]
                    if inner is None:
                        continue
                    products.append({
                        "name": getattr(inner, "title", "?"),
                        "product_id": getattr(inner, "product_id", ""),
                        "type": getattr(inner, "type", ""),
                    })
            return products
        except XboxError:
            raise
        except Exception as e:
            raise _classify_error(str(e)) from e

    async def get_game_details(self, product_id: str) -> dict[str, Any]:
        """Get detailed game info from the catalog."""
        xbl = await self._ensure_auth()
        try:
            # product_id here is a legacy Xbox product id, so resolve via the
            # alternate-id endpoint (get_products expects catalog "big IDs").
            resp = await xbl.catalog.get_product_from_alternate_id(
                product_id, AlternateIdType.LEGACY_XBOX_PRODUCT_ID
            )
            if not resp or not resp.products:
                raise NotFoundError(f"Product not found: {product_id}")

            p = resp.products[0]
            return {
                "name": getattr(p, "localized_properties", [{}])[0].get("ProductTitle", "?")
                if getattr(p, "localized_properties", None)
                else "?",
                "product_id": getattr(p, "product_id", ""),
                "publisher": getattr(p, "localized_properties", [{}])[0].get("PublisherName", "")
                if getattr(p, "localized_properties", None)
                else "",
                "developer": getattr(p, "localized_properties", [{}])[0].get("DeveloperName", "")
                if getattr(p, "localized_properties", None)
                else "",
                "description": getattr(p, "localized_properties", [{}])[0].get("ShortDescription", "")
                if getattr(p, "localized_properties", None)
                else "",
                "category": getattr(p, "product_kind", ""),
            }
        except XboxError:
            raise
        except Exception as e:
            raise _classify_error(str(e)) from e

    # -----------------------------------------------------------------------
    # Friend management (gated)
    # -----------------------------------------------------------------------

    async def add_friend(self, xuid: str) -> dict[str, Any]:
        """Add a friend by XUID."""
        xbl = await self._ensure_auth()
        try:
            session = xbl.session
            url = f"https://social.xboxlive.com/users/me/people/xuid({xuid})"
            resp = await session.put(url, data="")
            resp.raise_for_status()
            return {"xuid": xuid, "status": "added"}
        except XboxError:
            raise
        except Exception as e:
            raise _classify_error(str(e)) from e

    async def remove_friend(self, xuid: str) -> dict[str, Any]:
        """Remove a friend by XUID."""
        xbl = await self._ensure_auth()
        try:
            session = xbl.session
            url = f"https://social.xboxlive.com/users/me/people/xuid({xuid})"
            resp = await session.delete(url)
            resp.raise_for_status()
            return {"xuid": xuid, "status": "removed"}
        except XboxError:
            raise
        except Exception as e:
            raise _classify_error(str(e)) from e

    # -----------------------------------------------------------------------
    # Lists (purchased games)
    # -----------------------------------------------------------------------

    async def get_games_purchased(self, limit: int = 25) -> list[dict[str, Any]]:
        """Get purchased/owned games list."""
        xbl = await self._ensure_auth()
        try:
            resp = await xbl.lists.get_items(str(xbl.xuid), "XBLPins")
            items = []
            if resp and hasattr(resp, "list_items"):
                for li in resp.list_items[:limit]:
                    # The pinned title lives under list_item.item.
                    inner = getattr(li, "item", None)
                    if inner is None:
                        continue
                    items.append({
                        "name": getattr(inner, "title", "?"),
                        "title_id": getattr(inner, "item_id", ""),
                    })
            return items
        except XboxError:
            raise
        except Exception as e:
            raise _classify_error(str(e)) from e

    # -----------------------------------------------------------------------
    # Cleanup
    # -----------------------------------------------------------------------

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session and not self._session.is_closed:
            await self._session.aclose()
