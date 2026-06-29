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
            profile = await xbl.profile.get_profile_by_xuid(
                xbl.xuid, ["GameDisplayName", "Gamerscore", "GameDisplayPicRaw"]
            )
            settings = {}
            if profile and profile.profile_users:
                for s in profile.profile_users[0].settings:
                    settings[s.id] = s.value
            result: dict[str, Any] = {
                "authenticated": True,
                "xuid": str(xbl.xuid),
                "gamertag": settings.get("GameDisplayName", "unknown"),
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
            settings_list = [
                "GameDisplayName", "Gamerscore", "GameDisplayPicRaw",
                "AccountTier", "TenureLevel", "PreferredColor",
                "RealName", "Bio", "Watermarks",
            ]
            if gamertag:
                resp = await xbl.profile.get_profile_by_gamertag(gamertag)
            elif xuid:
                resp = await xbl.profile.get_profile_by_xuid(xuid, settings_list)
            else:
                resp = await xbl.profile.get_profile_by_xuid(xbl.xuid, settings_list)

            if not resp or not resp.profile_users:
                raise NotFoundError(f"Profile not found: {gamertag or xuid or 'self'}")

            user = resp.profile_users[0]
            settings = {s.id: s.value for s in user.settings}
            return {
                "xuid": str(user.id),
                "gamertag": settings.get("GameDisplayName", ""),
                "gamerscore": settings.get("Gamerscore", ""),
                "account_tier": settings.get("AccountTier", ""),
                "tenure_level": settings.get("TenureLevel", ""),
                "preferred_color": settings.get("PreferredColor", ""),
                "real_name": settings.get("RealName", ""),
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
                    item: dict[str, Any] = {
                        "name": a.name,
                        "description": getattr(a, "description", ""),
                        "gamerscore": getattr(a, "rewards", [{}])[0].get("value", 0)
                        if getattr(a, "rewards", None)
                        else 0,
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
            profile = await xbl.profile.get_profile_by_xuid(
                target_xuid, ["Gamerscore", "GameDisplayName"]
            )
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
            resp = await xbl.usersearch.get_live_search(query)
            results = []
            if resp and resp.results:
                for r in resp.results[:limit]:
                    item: dict[str, Any] = {
                        "gamertag": getattr(r, "gamertag", "?"),
                        "xuid": str(getattr(r, "xuid", "")),
                    }
                    if hasattr(r, "gamerscore"):
                        item["gamerscore"] = r.gamerscore
                    results.append(item)
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
            await xbl.message.send_message(xuids, message_text)
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
            if title_id:
                resp = await xbl.gameclips.get_recent_community_clips_by_title_id(title_id)
            else:
                resp = await xbl.gameclips.get_recent_own_clips(target_xuid)
            clips = []
            if resp and resp.game_clips:
                for c in resp.game_clips[:limit]:
                    item: dict[str, Any] = {
                        "title_name": getattr(c, "title_name", "?"),
                        "clip_id": getattr(c, "game_clip_id", ""),
                        "date_recorded": str(getattr(c, "date_recorded", "")),
                        "duration_seconds": getattr(c, "duration_in_seconds", 0),
                        "views": getattr(c, "views", 0),
                    }
                    if hasattr(c, "game_clip_uris") and c.game_clip_uris:
                        item["uri"] = c.game_clip_uris[0].uri
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
            if title_id:
                resp = await xbl.screenshots.get_recent_community_screenshots_by_title_id(title_id)
            else:
                resp = await xbl.screenshots.get_recent_own_screenshots(target_xuid)
            screenshots = []
            if resp and resp.screenshots:
                for s in resp.screenshots[:limit]:
                    item: dict[str, Any] = {
                        "title_name": getattr(s, "title_name", "?"),
                        "screenshot_id": getattr(s, "screenshot_id", ""),
                        "date_taken": str(getattr(s, "date_taken", "")),
                        "views": getattr(s, "views", 0),
                    }
                    if hasattr(s, "screenshot_uris") and s.screenshot_uris:
                        item["uri"] = s.screenshot_uris[0].uri
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
            cmd_map: dict[str, Any] = {
                "power_off": lambda: sg.command(console_id, "Power", "TurnOff"),
                "power_on": lambda: sg.command(console_id, "Power", "TurnOn"),
                "reboot": lambda: sg.command(console_id, "Power", "Reboot"),
                "mute": lambda: sg.command(console_id, "Audio", "Mute"),
                "unmute": lambda: sg.command(console_id, "Audio", "Unmute"),
                "volume_up": lambda: sg.command(console_id, "Audio", "VolumeUp"),
                "volume_down": lambda: sg.command(console_id, "Audio", "VolumeDown"),
                "play": lambda: sg.command(console_id, "Media", "Play"),
                "pause": lambda: sg.command(console_id, "Media", "Pause"),
                "go_home": lambda: sg.command(console_id, "Shell", "GoHome"),
                "go_back": lambda: sg.command(console_id, "Shell", "GoBack"),
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
            resp = await xbl.catalog.product_search(query, max_items=limit)
            products = []
            if resp and resp.results:
                for r in resp.results[:limit]:
                    item: dict[str, Any] = {
                        "name": getattr(r, "title", "?"),
                        "product_id": getattr(r, "product_id", ""),
                    }
                    if hasattr(r, "publisher_name"):
                        item["publisher"] = r.publisher_name
                    products.append(item)
            return products
        except XboxError:
            raise
        except Exception as e:
            raise _classify_error(str(e)) from e

    async def get_game_details(self, product_id: str) -> dict[str, Any]:
        """Get detailed game info from the catalog."""
        xbl = await self._ensure_auth()
        try:
            resp = await xbl.catalog.get_products(
                [product_id], AlternateIdType.LEGACY_XBOX_PRODUCT_ID
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
            resp = await xbl.lists.get_items(xbl.xuid, "XBLPins")
            items = []
            if resp and hasattr(resp, "list_items"):
                for item in resp.list_items[:limit]:
                    items.append({
                        "name": getattr(item, "title", "?"),
                        "title_id": getattr(item, "item_id", ""),
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
