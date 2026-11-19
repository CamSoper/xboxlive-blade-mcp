"""Xbox Live Blade MCP Server — profiles, achievements, friends, presence, SmartGlass.

Wraps the Xbox Live REST APIs via xbox-webapi-python as MCP tools. Token-efficient
by default: compact pipe-delimited output, null-field omission.
Write operations gated by XBOX_WRITE_ENABLED. Console commands (SmartGlass power,
reboot) require explicit confirm=true.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

from xboxlive_blade_mcp.client import XboxClient, XboxError
from xboxlive_blade_mcp.formatters import (
    format_achievement_summary,
    format_achievements,
    format_clips,
    format_consoles,
    format_friends,
    format_game_details,
    format_games,
    format_inbox,
    format_info,
    format_presence,
    format_screenshots,
    format_search_results,
    format_store_results,
)
from xboxlive_blade_mcp.models import (
    DEFAULT_LIMIT,
    is_write_enabled,
    require_confirm,
    require_write,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Transport configuration
# ---------------------------------------------------------------------------

TRANSPORT = os.environ.get("XBOX_MCP_TRANSPORT", "stdio")
HTTP_HOST = os.environ.get("XBOX_MCP_HOST", "127.0.0.1")
HTTP_PORT = int(os.environ.get("XBOX_MCP_PORT", "8500"))

# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "XboxLiveBlade",
    instructions=(
        "Xbox Live operations: profiles, achievements, friends, presence, game library, "
        "messages, game clips, screenshots, Store catalog, and SmartGlass console control. "
        "All data is for the authenticated Xbox Live account unless xuid= targets another user. "
        "Write operations require XBOX_WRITE_ENABLED=true. "
        "SmartGlass power commands require confirm=true."
    ),
)

# Lazy-initialized client
_client: XboxClient | None = None


def _get_client() -> XboxClient:
    """Get or create the XboxClient singleton."""
    global _client  # noqa: PLW0603
    if _client is None:
        _client = XboxClient()
    return _client


def _error(e: XboxError) -> str:
    """Format a client error as a user-friendly string."""
    return f"Error: {e}"


# ===========================================================================
# DOMAIN 1: META (1 tool)
# ===========================================================================


@mcp.tool()
async def xbox_info() -> str:
    """Health check: auth status, gamertag, XUID, token expiry, write gate status."""
    try:
        result = await _get_client().info()
        output = format_info(result)
        output += f" | write_gate={'enabled' if is_write_enabled() else 'disabled'}"
        return output
    except XboxError as e:
        return _error(e)


_device_flow = None


@mcp.tool()
async def xbox_auth_start() -> str:
    """Start an Xbox Live sign-in when auth has failed. Returns a code: open the link, type the code, sign in to the Microsoft account. The server stores the tokens itself."""
    global _device_flow  # noqa: PLW0603
    import asyncio

    from xboxlive_blade_mcp.token_store import DeviceFlow, RefreshError

    try:
        flow = await asyncio.to_thread(DeviceFlow.start)
    except (RefreshError, Exception) as e:  # noqa: BLE001
        return f"Error: could not start sign-in: {e}"
    _device_flow = flow
    flow.run_in_background()
    minutes = max(1, int((flow.expires_at - __import__("time").time()) // 60))
    return (
        f"Open {flow.verification_uri} and enter code {flow.user_code}, then sign in "
        f"with the Xbox account. The code works for about {minutes} minutes. "
        "Call xbox_auth_status to confirm."
    )


@mcp.tool()
async def xbox_auth_status() -> str:
    """Check the sign-in started by xbox_auth_start, and the stored token's age."""
    from xboxlive_blade_mcp.token_store import token_age

    flow = _device_flow
    parts = []
    if flow is None:
        parts.append("sign-in: none started")
    else:
        parts.append(f"sign-in: {flow.status}")
        if flow.detail:
            parts.append(flow.detail)
        if flow.status == "done":
            _get_client().reset_auth()
    age = token_age()
    parts.append("token: none" if age is None else f"token_age_hours={age.total_seconds() / 3600:.1f}")
    return " | ".join(parts)


# ===========================================================================
# DOMAIN 2: PROFILE (2 tools) — contract: profile, search_users
# ===========================================================================


@mcp.tool()
async def xbox_profile(
    gamertag: Annotated[str | None, Field(description="Gamertag to look up (omit for self)")] = None,
    xuid: Annotated[str | None, Field(description="XUID to look up (omit for self)")] = None,
) -> str:
    """Get user profile: gamertag, gamerscore, account tier, tenure, bio. [gaming-v1: profile]"""
    try:
        from xboxlive_blade_mcp.formatters import format_profile

        result = await _get_client().get_profile(gamertag=gamertag, xuid=xuid)
        return format_profile(result)
    except XboxError as e:
        return _error(e)


@mcp.tool()
async def xbox_search_users(
    query: Annotated[str, Field(description="Gamertag or partial name to search")],
    limit: Annotated[int, Field(description="Max results")] = 10,
) -> str:
    """Search for users by gamertag. [gaming-v1: search_users]"""
    try:
        results = await _get_client().search_users(query, limit)
        return format_search_results(results)
    except XboxError as e:
        return _error(e)


# ===========================================================================
# DOMAIN 3: ACHIEVEMENTS (4 tools) — contract: achievements, achievement_summary,
#   achievement_compare, achievement_groups
# ===========================================================================


@mcp.tool()
async def xbox_achievements(
    title_id: Annotated[str | None, Field(description="Game title ID (omit for recent across all games)")] = None,
    xuid: Annotated[str | None, Field(description="Target user XUID (omit for self)")] = None,
    limit: Annotated[int, Field(description="Max results")] = DEFAULT_LIMIT,
) -> str:
    """List achievements for a game with earned status and gamerscore. [gaming-v1: achievements]"""
    try:
        results = await _get_client().get_achievements(xuid=xuid, title_id=title_id, limit=limit)
        return format_achievements(results, title=title_id or "Recent")
    except XboxError as e:
        return _error(e)


@mcp.tool()
async def xbox_achievement_summary(
    xuid: Annotated[str | None, Field(description="Target user XUID (omit for self)")] = None,
) -> str:
    """Overall achievement stats: gamerscore, titles played, completion percentage. [gaming-v1: achievement_summary]"""
    try:
        result = await _get_client().get_achievement_summary(xuid=xuid)
        return format_achievement_summary(result)
    except XboxError as e:
        return _error(e)


@mcp.tool()
async def xbox_achievement_compare(
    title_id: Annotated[str, Field(description="Game title ID to compare")],
    xuid: Annotated[str, Field(description="Other user's XUID to compare against")],
    limit: Annotated[int, Field(description="Max results")] = DEFAULT_LIMIT,
) -> str:
    """Compare achievement progress between you and another user for a game. [gaming-v1: achievement_compare]"""
    try:
        my_achievements = await _get_client().get_achievements(title_id=title_id, limit=limit)
        their_achievements = await _get_client().get_achievements(xuid=xuid, title_id=title_id, limit=limit)

        my_earned = {a["name"] for a in my_achievements if a.get("earned")}
        their_earned = {a["name"] for a in their_achievements if a.get("earned")}

        lines = [f"## {title_id} — Achievement Comparison"]
        my_count = f"{len(my_earned)}/{len(my_achievements)}"
        their_count = f"{len(their_earned)}/{len(their_achievements)}"
        lines.append(f"You: {my_count} | Them: {their_count}")
        for a in my_achievements:
            name = a.get("name", "?")
            me = "Y" if name in my_earned else "N"
            them = "Y" if name in their_earned else "N"
            g = a.get("gamerscore", 0)
            lines.append(f"{name} | {g}G | you={me} | them={them}")
        return "\n".join(lines)
    except XboxError as e:
        return _error(e)


@mcp.tool()
async def xbox_achievement_groups(
    title_id: Annotated[str, Field(description="Game title ID")],
    xuid: Annotated[str | None, Field(description="Target user XUID (omit for self)")] = None,
) -> str:
    """Achievement groups (base game, DLC) with per-group completion stats. [gaming-v1: achievement_groups]"""
    try:
        achievements = await _get_client().get_achievements(xuid=xuid, title_id=title_id, limit=100)
        # xbox-webapi returns flat list; group by checking if achievements have group metadata
        earned = sum(1 for a in achievements if a.get("earned"))
        total_g = sum(a.get("gamerscore", 0) for a in achievements)
        earned_g = sum(a.get("gamerscore", 0) for a in achievements if a.get("earned"))
        lines = [
            f"## {title_id}",
            f"Base game: {earned}/{len(achievements)} achievements | {earned_g}/{total_g}G",
        ]
        return "\n".join(lines)
    except XboxError as e:
        return _error(e)


# ===========================================================================
# DOMAIN 4: GAMES (5 tools) — contract: games_played, games_recent,
#   games_purchased, game_details, search_games
# ===========================================================================


@mcp.tool()
async def xbox_games(
    xuid: Annotated[str | None, Field(description="Target user XUID (omit for self)")] = None,
    limit: Annotated[int, Field(description="Max results")] = DEFAULT_LIMIT,
) -> str:
    """Game library with playtime, achievement progress, and last played dates. [gaming-v1: games_played]"""
    try:
        results = await _get_client().get_games(xuid=xuid, limit=limit)
        return format_games(results)
    except XboxError as e:
        return _error(e)


@mcp.tool()
async def xbox_games_recent(
    xuid: Annotated[str | None, Field(description="Target user XUID (omit for self)")] = None,
    limit: Annotated[int, Field(description="Max results")] = 10,
) -> str:
    """Recently played games ordered by last session. [gaming-v1: games_recent]"""
    try:
        results = await _get_client().get_games_recent(xuid=xuid, limit=limit)
        return format_games(results)
    except XboxError as e:
        return _error(e)


@mcp.tool()
async def xbox_games_purchased(
    limit: Annotated[int, Field(description="Max results")] = DEFAULT_LIMIT,
) -> str:
    """Game purchase/entitlement history from Xbox Lists. [gaming-v1: games_purchased]"""
    try:
        results = await _get_client().get_games_purchased(limit=limit)
        return format_games(results)
    except XboxError as e:
        return _error(e)


@mcp.tool()
async def xbox_game_details(
    product_id: Annotated[str, Field(description="Xbox Store product ID")],
) -> str:
    """Title metadata: name, publisher, developer, description, category. [gaming-v1: game_details]"""
    try:
        result = await _get_client().get_game_details(product_id)
        return format_game_details(result)
    except XboxError as e:
        return _error(e)


@mcp.tool()
async def xbox_search_games(
    query: Annotated[str, Field(description="Game title to search for")],
    limit: Annotated[int, Field(description="Max results")] = 10,
) -> str:
    """Search Xbox Store catalog by title. [gaming-v1: search_games]"""
    try:
        results = await _get_client().search_games_catalog(query, limit)
        return format_store_results(results)
    except XboxError as e:
        return _error(e)


# ===========================================================================
# DOMAIN 5: SOCIAL (3 tools) — contract: friends, presence + extra: inbox
# ===========================================================================


@mcp.tool()
async def xbox_friends(
    limit: Annotated[int, Field(description="Max results")] = DEFAULT_LIMIT,
) -> str:
    """Friends list with online status, current game, and gamerscore. [gaming-v1: friends]"""
    try:
        results = await _get_client().get_friends(limit)
        return format_friends(results)
    except XboxError as e:
        return _error(e)


@mcp.tool()
async def xbox_presence(
    xuid: Annotated[str | None, Field(description="Target user XUID (omit for self)")] = None,
) -> str:
    """Online/offline/in-game presence with rich presence string. [gaming-v1: presence]"""
    try:
        result = await _get_client().get_presence(xuid)
        return format_presence(result)
    except XboxError as e:
        return _error(e)


@mcp.tool()
async def xbox_inbox(
    limit: Annotated[int, Field(description="Max messages")] = DEFAULT_LIMIT,
) -> str:
    """Message inbox — sender, timestamp, summary, read status."""
    try:
        results = await _get_client().get_inbox(limit)
        return format_inbox(results)
    except XboxError as e:
        return _error(e)


# ===========================================================================
# DOMAIN 6: MEDIA (2 tools) — beyond contract
# ===========================================================================


@mcp.tool()
async def xbox_clips(
    xuid: Annotated[str | None, Field(description="Target user XUID (omit for self)")] = None,
    title_id: Annotated[str | None, Field(description="Filter by game title ID")] = None,
    limit: Annotated[int, Field(description="Max results")] = 10,
) -> str:
    """Game clips with title, date, duration, views, and URI."""
    try:
        results = await _get_client().get_clips(xuid=xuid, title_id=title_id, limit=limit)
        return format_clips(results)
    except XboxError as e:
        return _error(e)


@mcp.tool()
async def xbox_screenshots(
    xuid: Annotated[str | None, Field(description="Target user XUID (omit for self)")] = None,
    title_id: Annotated[str | None, Field(description="Filter by game title ID")] = None,
    limit: Annotated[int, Field(description="Max results")] = 10,
) -> str:
    """Screenshots with title, date, views, and URI."""
    try:
        results = await _get_client().get_screenshots(xuid=xuid, title_id=title_id, limit=limit)
        return format_screenshots(results)
    except XboxError as e:
        return _error(e)


# ===========================================================================
# DOMAIN 7: CONSOLES — SmartGlass (2 tools) — contract: devices
# ===========================================================================


@mcp.tool()
async def xbox_consoles() -> str:
    """Registered consoles with power state and type. [gaming-v1: devices]"""
    try:
        results = await _get_client().get_consoles()
        return format_consoles(results)
    except XboxError as e:
        return _error(e)


@mcp.tool()
async def xbox_console_command(
    console_id: Annotated[str, Field(description="Console ID from xbox_consoles")],
    command: Annotated[
        str,
        Field(
            description=(
                "Command: power_on, power_off, reboot, mute, unmute, "
                "volume_up, volume_down, play, pause, go_home, go_back"
            )
        ),
    ],
    confirm: Annotated[bool, Field(description="Must be true — controls physical hardware")] = False,
) -> str:
    """Send SmartGlass command to a console. Requires XBOX_WRITE_ENABLED=true AND confirm=true."""
    gate = require_write()
    if gate:
        return gate
    conf = require_confirm(confirm)
    if conf:
        return conf
    try:
        result = await _get_client().console_command(console_id, command)
        return f"{result['command']} sent to {result['console_id']}"
    except XboxError as e:
        return _error(e)


# ===========================================================================
# DOMAIN 8: WRITE-GATED (3 tools) — contract: send_message, friend_add, friend_remove
# ===========================================================================


@mcp.tool()
async def xbox_send_message(
    xuids: Annotated[list[str], Field(description="Recipient XUIDs")],
    message: Annotated[str, Field(description="Message text (max 256 chars)")],
) -> str:
    """Send a text message to one or more users. Requires XBOX_WRITE_ENABLED=true. [gaming-v1: send_message]"""
    gate = require_write()
    if gate:
        return gate
    if len(message) > 256:
        return "Error: Message exceeds 256 character limit."
    try:
        result = await _get_client().send_message(xuids, message)
        return f"Message sent to {result['recipients']} recipient(s)"
    except XboxError as e:
        return _error(e)


@mcp.tool()
async def xbox_friend_add(
    xuid: Annotated[str, Field(description="XUID of user to add as friend")],
) -> str:
    """Add a friend / accept a pending friend request. Requires XBOX_WRITE_ENABLED=true. [gaming-v1: friend_add]"""
    gate = require_write()
    if gate:
        return gate
    try:
        result = await _get_client().add_friend(xuid)
        return f"Friend request sent to xuid={result['xuid']}"
    except XboxError as e:
        return _error(e)


@mcp.tool()
async def xbox_friend_remove(
    xuid: Annotated[str, Field(description="XUID of friend to remove")],
    confirm: Annotated[bool, Field(description="Must be true to confirm removal")] = False,
) -> str:
    """Remove a friend. Requires XBOX_WRITE_ENABLED=true AND confirm=true. [gaming-v1: friend_remove]"""
    gate = require_write()
    if gate:
        return gate
    conf = require_confirm(confirm)
    if conf:
        return conf
    try:
        result = await _get_client().remove_friend(xuid)
        return f"Removed friend xuid={result['xuid']}"
    except XboxError as e:
        return _error(e)


# ===========================================================================
# Entry point
# ===========================================================================


def main() -> None:
    """Run the MCP server or auth subcommand."""
    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        if "--device" in sys.argv[2:]:
            from xboxlive_blade_mcp.token_store import DeviceFlow

            flow = DeviceFlow.start()
            print(f"Open {flow.verification_uri} and enter code {flow.user_code}")
            status = flow.wait()
            print(f"Sign-in {status}. {flow.detail}".strip())
            sys.exit(0 if status == "done" else 1)
        from xboxlive_blade_mcp.xbox_auth import run_auth

        run_auth()
        return

    if os.environ.get("XBOX_KEEPALIVE", "true").lower() != "false":
        from xboxlive_blade_mcp.token_store import start_keepalive

        start_keepalive()

    if TRANSPORT == "http":
        from starlette.middleware import Middleware

        from xboxlive_blade_mcp.auth import BearerAuthMiddleware

        mcp.run(
            transport="streamable-http",
            host=HTTP_HOST,
            port=HTTP_PORT,
            middleware=[Middleware(BearerAuthMiddleware)],
        )
    else:
        mcp.run(transport="stdio")
