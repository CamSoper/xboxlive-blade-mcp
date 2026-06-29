"""Xbox Live OAuth2 interactive authentication flow.

Handles the browser-based MSA login → XAU → XSTS token chain.
Spins up a temporary localhost HTTP server to catch the OAuth redirect.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlparse

from xbox.webapi.authentication.manager import AuthenticationManager
from xbox.webapi.common.signed_session import SignedSession

from xboxlive_blade_mcp.models import get_client_id, get_client_secret, get_token_path

logger = logging.getLogger(__name__)

REDIRECT_URI = "http://localhost:8400/auth/callback"
AUTH_PORT = 8400


class _CallbackHandler(BaseHTTPRequestHandler):
    """Captures the OAuth2 redirect code."""

    auth_code: str | None = None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if "code" in params:
            _CallbackHandler.auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Authentication successful!</h2>"
                b"<p>You can close this tab and return to the terminal.</p>"
                b"</body></html>"
            )
        elif "error" in params:
            error = params.get("error", ["unknown"])[0]
            desc = params.get("error_description", [""])[0]
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                f"<html><body><h2>Authentication failed</h2>"
                f"<p>{error}: {desc}</p></body></html>".encode()
            )
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass  # Suppress default HTTP server logs


async def authenticate() -> None:
    """Run the interactive OAuth2 authentication flow."""
    client_id = get_client_id()
    client_secret = get_client_secret()

    async with SignedSession() as session:
        auth_mgr = AuthenticationManager(session, client_id, client_secret, REDIRECT_URI)

        # Generate auth URL
        auth_url = auth_mgr.generate_authorization_url()

        print("\n=== Xbox Live Authentication ===\n")
        print("Opening browser for Microsoft account login...")
        print(f"If the browser doesn't open, visit:\n{auth_url}\n")

        webbrowser.open(auth_url)

        # Start local server to catch redirect
        _CallbackHandler.auth_code = None
        server = HTTPServer(("localhost", AUTH_PORT), _CallbackHandler)
        server_thread = Thread(target=server.handle_request, daemon=True)
        server_thread.start()

        print(f"Waiting for redirect on http://localhost:{AUTH_PORT}/auth/callback ...")
        server_thread.join(timeout=120)
        server.server_close()

        code = _CallbackHandler.auth_code
        if not code:
            print("\nError: No authorization code received. Timed out or cancelled.")
            sys.exit(1)

        print("Authorization code received. Exchanging for tokens...")

        try:
            await auth_mgr.request_tokens(code)
        except Exception as e:
            print(f"\nError: Token exchange failed: {e}")
            sys.exit(1)

        # Save tokens
        token_path = get_token_path()
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(auth_mgr.oauth.model_dump_json(indent=2))
        token_path.chmod(0o600)

    print(f"\nTokens saved to {token_path}")
    print("Authentication complete! The MCP server can now connect to Xbox Live.")


def run_auth() -> None:
    """Entry point for auth subcommand."""
    asyncio.run(authenticate())
