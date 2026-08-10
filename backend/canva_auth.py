"""Canva OAuth helper for designer.py.

STATUS: dev-mode only. Tokens are cached in a local JSON file next to this
module. That is fine while the system isn't deployed anywhere — but the
moment this runs somewhere that doesn't have a persistent local disk (a
container that gets recreated, a serverless function, a second replica),
this breaks, because the token cache won't be there next time. Before you
deploy, swap FileTokenStorage's two methods to read/write a secrets manager
or a database row instead of a file — nothing else in this module needs to
change if you do.

SETUP (one-time, per Canva account this service will act as):
  1. Run designer.py once, interactively, with a real browser available on
     that machine. The mcp library registers a client with Canva
     automatically on first connection (no manual curl step needed — that
     was true of an older flow, current mcp versions handle it internally
     and cache the result via FileTokenStorage below).
  2. A browser tab opens asking someone to log into the Canva account this
     service should act as, and approve access. After that one login, the
     token is cached and future runs are headless.
  3. If the cached token ever gets revoked/expires without a refresh token,
     step 1 has to happen again. That's a Canva auth-model constraint, not
     something this code can route around.

Requires: pip install mcp
"""

import asyncio
import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)

load_dotenv()

TOKEN_CACHE_PATH = Path(__file__).resolve().parent / ".canva_tokens.json"
REDIRECT_URI = os.getenv("CANVA_REDIRECT_URI", "http://localhost:8765/callback")


class FileTokenStorage(TokenStorage):
    """Dev-only token cache. Replace with a secrets store before deploying —
    see the module docstring."""

    def __init__(self, path: Path = TOKEN_CACHE_PATH):
        self._path = path

    def _read(self) -> dict:
        if not self._path.exists():
            return {}
        return json.loads(self._path.read_text())

    def _write(self, data: dict) -> None:
        self._path.write_text(json.dumps(data, indent=2))
        os.chmod(self._path, 0o600)  # tokens are secrets; keep the file locked down

    async def get_tokens(self) -> OAuthToken | None:
        data = self._read().get("tokens")
        return OAuthToken(**data) if data else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json")
        self._write(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        data = self._read().get("client_info")
        return OAuthClientInformationFull(**data) if data else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._read()
        data["client_info"] = client_info.model_dump(mode="json")
        self._write(data)


async def _open_browser_for_auth(authorization_url: str) -> None:
    print(f"\nOpening browser to authorize Canva access:\n{authorization_url}\n")
    webbrowser.open(authorization_url)


async def _wait_for_callback() -> AuthorizationCodeResult:
    """Spins up a one-shot local server to catch Canva's OAuth redirect.

    OAuthClientProvider requires this to be an async function. The actual
    HTTPServer.serve_forever() call below is blocking, so it runs in a
    worker thread via asyncio.to_thread rather than directly on the event
    loop — otherwise it would freeze everything else in this process for
    however long the browser login takes.
    """
    return await asyncio.to_thread(_wait_for_callback_blocking)


def _wait_for_callback_blocking() -> AuthorizationCodeResult:
    """Spins up a one-shot local server to catch Canva's OAuth redirect.

    Blocks until the browser hits REDIRECT_URI, then returns the
    authorization result and shuts the server down. Only usable when a
    browser can reach localhost on this machine — i.e. someone running this
    by hand, which matches where you are right now (not deployed).
    """
    result = {}
    server_holder = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            params = parse_qs(urlparse(self.path).query)
            result["code"] = params.get("code", [None])[0]
            result["state"] = params.get("state", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>Canva authorized \xe2\x80\x94 you can close this tab.</body></html>")
            threading.Thread(target=server_holder["server"].shutdown).start()

        def log_message(self, *args):
            pass  # keep console quiet

    parsed = urlparse(REDIRECT_URI)
    server = HTTPServer((parsed.hostname, parsed.port), Handler)
    server_holder["server"] = server
    server.serve_forever()

    return AuthorizationCodeResult(code=result["code"], state=result["state"])


def get_canva_auth() -> OAuthClientProvider:
    """Build the auth object to pass as `auth=` to streamablehttp_client.

    First call on a fresh machine triggers the one-time browser login (see
    module docstring's SETUP section). Subsequent calls reuse the cached
    token via FileTokenStorage until it needs a refresh, which
    OAuthClientProvider handles on its own.
    """
    client_metadata = OAuthClientMetadata(
        redirect_uris=[REDIRECT_URI],
        client_name="CreditChek Marketing Designer Agent",
        grant_types=["authorization_code", "refresh_token"],
        token_endpoint_auth_method="client_secret_post",
    )

    return OAuthClientProvider(
        server_url="https://mcp.canva.com/mcp",
        client_metadata=client_metadata,
        storage=FileTokenStorage(),
        redirect_handler=_open_browser_for_auth,
        callback_handler=_wait_for_callback,
    )