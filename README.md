# Xbox Live Blade MCP

Xbox Live profiles, achievements, friends, presence, game library, SmartGlass console control, and Store catalog — as an MCP server.

Built on [xbox-webapi-python](https://github.com/OpenXbox/xbox-webapi-python) (200+ stars, 16 API providers, MIT). Designed for [Sidereal](https://sidereal.cc) and any MCP-compatible host.

## Why another Xbox MCP?

| Feature | **xboxlive-blade-mcp** | Apify Xbox scrapers | gaming-mcp | Forerunner (Halo) |
|---------|:-----:|:-----:|:-----:|:-----:|
| Real Xbox Live API (not scraping) | **Yes** | No (web scrape) | No (local FS) | Yes |
| Profile / gamerscore | **Yes** | Partial | No | Halo only |
| Achievements | **Yes** | No | No | No |
| Friends / presence | **Yes** | No | No | No |
| Game library + playtime | **Yes** | No | Local only | No |
| Messages (read/send) | **Yes** | No | No | No |
| Game clips + screenshots | **Yes** | No | No | No |
| SmartGlass console control | **Yes** | No | No | No |
| Store catalog search | **Yes** | Products only | No | No |
| Token-efficient output | **Pipe-delimited** | JSON blobs | JSON blobs | JSON blobs |
| Write gates | **Yes** | N/A | N/A | N/A |
| Credential scrubbing | **Yes** | Apify-managed | N/A | Basic |
| Cross-platform | **macOS/Linux/Win** | Cloud only | Windows only | Cross-platform |
| Cost | **Free** | $19-25/mo | Free | Free |
| Tool count | **22** | 1 (via gateway) | 3 | 6 |
| gaming-v1 contract | **17/17** | 0/17 | 0/17 | 0/17 |

## Quick Start

### 1. Install

```bash
# Via uv (recommended)
uvx xboxlive-blade-mcp

# Via pip
pip install xboxlive-blade-mcp
```

### 2. Configure Azure App

1. Go to [portal.azure.com](https://portal.azure.com/#blade/Microsoft_AAD_RegisteredApps) > App registrations > New registration
2. Name: `xboxlive-blade-mcp` (or any name)
3. Redirect URI: `http://localhost:8400/auth/callback` (Web)
4. Note the **Application (client) ID**
5. Under Certificates & secrets, create a **Client secret** (optional for public clients)

### 3. Authenticate

```bash
export XBOX_CLIENT_ID="your-azure-app-client-id"
export XBOX_CLIENT_SECRET="your-client-secret"  # optional

xboxlive-blade-mcp auth
```

This opens a browser for Microsoft account login. Tokens are cached at `~/.xboxlive-blade-mcp/tokens.json` and auto-refresh on each use.

On a headless server, use the device-code sign-in instead: `xboxlive-blade-mcp auth --device` (or call the `xbox_auth_start` tool) prints a code to type at https://www.microsoft.com/link. This needs **Allow public client flows = Yes** on the Azure app registration.

Every token Microsoft returns is written to disk immediately (atomic write), and a background keep-alive refreshes the stored token every 12 hours (`XBOX_KEEPALIVE_SECONDS`; `XBOX_KEEPALIVE=false` turns it off), so the refresh token never ages out while the server is idle or long-running.

### 4. Run

```bash
# stdio (Claude Desktop, Sidereal)
xboxlive-blade-mcp

# HTTP transport
XBOX_MCP_TRANSPORT=http xboxlive-blade-mcp
```

### Claude Desktop / Sidereal config

```json
{
  "mcpServers": {
    "xbox": {
      "command": "uvx",
      "args": ["xboxlive-blade-mcp"],
      "env": {
        "XBOX_CLIENT_ID": "your-client-id",
        "XBOX_CLIENT_SECRET": "your-secret",
        "XBOX_WRITE_ENABLED": "false"
      }
    }
  }
}
```

## Tools (22)

### Read (17 tools)

| Tool | Description | gaming-v1 |
|------|-------------|-----------|
| `xbox_info` | Auth status, gamertag, write gate | — |
| `xbox_profile` | User profile (gamerscore, tier, bio) | `profile` |
| `xbox_search_users` | Search by gamertag | `search_users` |
| `xbox_achievements` | Per-game achievement list | `achievements` |
| `xbox_achievement_summary` | Overall gamerscore + completion % | `achievement_summary` |
| `xbox_achievement_compare` | Compare two users on a game | `achievement_compare` |
| `xbox_achievement_groups` | Base game + DLC completion | `achievement_groups` |
| `xbox_games` | Full game library | `games_played` |
| `xbox_games_recent` | Recently played | `games_recent` |
| `xbox_games_purchased` | Owned/entitled titles | `games_purchased` |
| `xbox_game_details` | Store product metadata | `game_details` |
| `xbox_search_games` | Catalog search | `search_games` |
| `xbox_friends` | Friends with online status | `friends` |
| `xbox_presence` | Detailed presence for one user | `presence` |
| `xbox_inbox` | Message inbox | — |
| `xbox_clips` | Game clips with URIs | — |
| `xbox_screenshots` | Screenshots with URIs | — |

### Write-gated (4 tools) — require `XBOX_WRITE_ENABLED=true`

| Tool | Description | gaming-v1 | Gate |
|------|-------------|-----------|------|
| `xbox_send_message` | Send text message (256 char max) | `send_message` | write |
| `xbox_friend_add` | Add friend / accept request | `friend_add` | write |
| `xbox_friend_remove` | Remove a friend | `friend_remove` | write + confirm |
| `xbox_console_command` | SmartGlass: power, volume, media | — | write + confirm |
| `xbox_consoles` | List registered consoles | `devices` | — |

### SmartGlass Commands

`xbox_console_command` supports: `power_on`, `power_off`, `reboot`, `mute`, `unmute`, `volume_up`, `volume_down`, `play`, `pause`, `go_home`, `go_back`

## Output Format

Compact, pipe-delimited, LLM-optimised:

```
# xbox_profile
TestPlayer | xuid=2535428504476914 | gamerscore=15230 | account_tier=Gold | bio=Playing games

# xbox_friends
CoolGamer | Online | xuid=253542850... | playing=Halo Infinite | gamerscore=8500
AnotherFriend | Offline | xuid=253542850... | last_seen=2026-04-01T18:30

# xbox_achievements (title_id=1240327261)
## 1240327261 (62/100 unlocked, 1250/2000G)
First Blood | 50G | earned=2026-01-15T10:00 | Win your first match
Legendary | 100G | locked | Complete campaign on Legendary
```

## Environment Variables

| Variable | Required | Secret | Default | Description |
|----------|----------|--------|---------|-------------|
| `XBOX_CLIENT_ID` | Yes | No | — | Azure App client ID |
| `XBOX_CLIENT_SECRET` | No | Yes | `""` | Azure App client secret |
| `XBOX_TOKEN_PATH` | No | No | `~/.xboxlive-blade-mcp/tokens.json` | Cached token location |
| `XBOX_WRITE_ENABLED` | No | No | `false` | Enable write operations |
| `XBOX_MCP_TRANSPORT` | No | No | `stdio` | `stdio` or `http` |
| `XBOX_MCP_HOST` | No | No | `127.0.0.1` | HTTP bind host |
| `XBOX_MCP_PORT` | No | No | `8500` | HTTP bind port |
| `XBOX_MCP_API_TOKEN` | No | Yes | — | Bearer token for HTTP auth |

## Security Model

- **Write gates**: All mutations (messaging, friend management, SmartGlass) require `XBOX_WRITE_ENABLED=true`
- **Confirm gates**: SmartGlass power commands and friend removal require explicit `confirm=true`
- **Credential scrubbing**: XBL3.0 tokens, JWTs, client secrets stripped from all error output
- **Token storage**: Cached at `~/.xboxlive-blade-mcp/tokens.json` with `chmod 600`
- **Auto-refresh**: Tokens refresh automatically on each request; re-auth only on full expiry
- **No credential leakage**: Tool responses never contain tokens or secrets

## Sidereal Marketplace

This MCP implements the [`gaming-v1`](https://sidereal.dev/contracts/gaming-v1) service contract (17/17 operations). It is published to the Sidereal Marketplace as a community plugin.

### Contract Coverage

| Tier | Coverage | Operations |
|------|----------|------------|
| Required | 4/4 | profile, achievements, achievement_summary, games_played |
| Recommended | 4/4 | games_recent, friends, presence, search_users |
| Optional | 6/6 | achievement_compare, achievement_groups, games_purchased, devices, game_details, search_games |
| Gated | 3/3 | send_message, friend_add, friend_remove |

## Development

```bash
# Clone
git clone https://github.com/Groupthink-dev/xboxlive-blade-mcp.git
cd xboxlive-blade-mcp

# Install with dev dependencies
uv sync --group dev --group test

# Run tests
uv run pytest

# Lint
uv run ruff check src/ tests/
uv run mypy src/

# Run locally
uv run xboxlive-blade-mcp
```

## Architecture

```
src/xboxlive_blade_mcp/
├── server.py          # FastMCP tools (22 tools across 8 domains)
├── client.py          # Xbox Live API client (wraps xbox-webapi-python)
├── auth.py            # Bearer token middleware (HTTP transport)
├── models.py          # Config, write/confirm gates, credential scrubbing
├── formatters.py      # Token-efficient pipe-delimited output
└── xbox_auth.py       # Interactive OAuth2 browser flow (auth subcommand)
```

### Dependencies

| Package | Purpose |
|---------|---------|
| [fastmcp](https://github.com/jlowin/fastmcp) | MCP server framework |
| [xbox-webapi](https://github.com/OpenXbox/xbox-webapi-python) | Xbox Live API client (16 providers, OAuth2, rate limiting) |
| [httpx](https://github.com/encode/httpx) | Async HTTP (transitive via xbox-webapi) |
| [pydantic](https://github.com/pydantic/pydantic) | Validation (transitive via fastmcp) |

## Underlying Xbox Live APIs

This MCP wraps the [unofficial Xbox Live REST APIs](https://github.com/OpenXbox/xbox-webapi-python) which are the same endpoints used by the official Xbox app. They are not officially documented by Microsoft but have been stable for years. The community-maintained [xbox-webapi-python](https://github.com/OpenXbox/xbox-webapi-python) library (200+ stars, 10 contributors) handles the complex auth chain (MSA OAuth2 -> Xbox User Token -> XSTS Token) and provides built-in rate limiting.

## Licence

MIT
