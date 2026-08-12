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
  3. If the cached token ever gets revoked, or expires with no refresh token,
     step 1 has to happen again. That's a Canva auth-model constraint, not
     something this code can route around.

A browser prompt at any other time is a BUG, not normal operation — the
access token lasts 4 hours and the refresh token renews it without a browser.
See the long comment in FileTokenStorage.get_tokens for the one that caused
exactly that, and what makes the refresh path get taken.

Requires: pip install mcp
"""

import asyncio
import json
import os
import threading
import time
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

# How long to wait for someone to finish the browser login before giving up.
# There MUST be a limit. Without one, an expired token turns any request into a
# permanent hang: the server sits on a socket waiting for a login nobody is
# there to perform, and the caller sees a request that never returns and no
# message explaining why. Long enough for a real login, short enough to fail.
AUTH_CALLBACK_TIMEOUT = float(os.getenv("CANVA_AUTH_TIMEOUT", "300"))

# Refresh this many seconds before the token actually expires, so a token that
# is valid when a request starts can't expire while a slow Canva render
# (generate-design, export-design) is still in flight.
TOKEN_EXPIRY_SKEW = 120

# Only one browser login at a time. Two requests arriving after the token
# expires will both try to re-authorize, and both try to bind the same
# redirect port — the loser raised OSError from inside the MCP task group,
# which reached the user as "unhandled errors in a TaskGroup". The second
# waiter now queues, and by the time it gets the lock the first login has
# already cached a token, so it usually has nothing left to do.
_auth_lock = threading.Lock()


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
        stored = self._read()
        data = stored.get("tokens")
        if not data:
            return None

        # Hand back an EXPIRED token with its access_token blanked, keeping the
        # refresh_token. This looks odd and is load-bearing.
        #
        # OAuthClientProvider only learns a token's expiry time when it receives
        # the token itself, in memory. Restored from disk, its expiry is unknown,
        # and is_token_valid() reads "unknown" as "valid":
        #     ... and (not self.token_expiry_time or time.time() <= ...)
        # So it skipped the refresh branch, sent a stale access token, got a 401,
        # and ran the FULL browser authorization flow — asking someone to log in
        # every few hours while a working refresh_token sat in this file unused.
        #
        # An empty access_token fails is_token_valid() while can_refresh_token()
        # still passes, which is exactly the refresh path. Only the copy we
        # return is blanked; the file keeps both tokens.
        if self._is_expired(stored):
            data = {**data, "access_token": ""}
        return OAuthToken(**data)

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json")
        # Absolute time, because expires_in is only meaningful next to the moment
        # the token arrived — and that moment is what a file loses.
        data["expires_at"] = (
            time.time() + tokens.expires_in - TOKEN_EXPIRY_SKEW
            if tokens.expires_in
            else None
        )
        self._write(data)

    @staticmethod
    def _is_expired(stored: dict) -> bool:
        """Whether the cached access token is past use.

        A cache written before expires_at existed has no timestamp. Treat that
        as expired: a refresh costs one request, while guessing "still good" is
        what caused the browser prompts.
        """
        expires_at = stored.get("expires_at")
        return not expires_at or time.time() >= expires_at

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

    OAuthClientProvider requires this to be an async function. The HTTPServer
    work below is blocking, so it runs in a worker thread via
    asyncio.to_thread rather than directly on the event loop — otherwise it
    would freeze everything else in this process for however long the browser
    login takes.
    """
    return await asyncio.to_thread(_wait_for_callback_blocking)


def _wait_for_callback_blocking() -> AuthorizationCodeResult:
    """Spins up a one-shot local server to catch Canva's OAuth redirect.

    Waits up to AUTH_CALLBACK_TIMEOUT for the browser to hit REDIRECT_URI,
    then returns the authorization result and shuts the server down. Only
    usable when a browser can reach localhost on this machine — i.e. someone
    running this by hand, which matches where you are right now (not deployed).

    Every exit path from here either returns a code or raises something that
    names the problem. Silence is the one outcome that isn't allowed.
    """
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            params = parse_qs(urlparse(self.path).query)
            code = params.get("code", [None])[0]
            if code:
                result["code"] = code
                result["state"] = params.get("state", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>Canva authorized \xe2\x80\x94 you can close this tab.</body></html>")

        def log_message(self, *args):
            pass  # keep console quiet

    parsed = urlparse(REDIRECT_URI)
    with _auth_lock:
        try:
            server = HTTPServer((parsed.hostname, parsed.port), Handler)
        except OSError as e:
            raise RuntimeError(
                f"Canva needs re-authorization, but port {parsed.port} on this "
                f"machine is already taken ({e}). Close whatever is using it, or "
                f"set CANVA_REDIRECT_URI to a free port (and add that URI to the "
                f"Canva app's allowed redirects)."
            ) from e

        # A deadline, not serve_forever(): handle_request() returns on timeout,
        # so an abandoned login fails with a message instead of hanging. Loop
        # rather than handling exactly one request — a browser that also asks
        # for /favicon.ico would otherwise spend the only request we serve.
        server.timeout = 5.0
        deadline = time.monotonic() + AUTH_CALLBACK_TIMEOUT
        try:
            while not result.get("code") and time.monotonic() < deadline:
                server.handle_request()
        finally:
            server.server_close()

    if not result.get("code"):
        raise RuntimeError(
            f"Canva authorization wasn't completed within "
            f"{int(AUTH_CALLBACK_TIMEOUT)}s. The cached token has expired, so the "
            f"browser tab that opened has to be logged in and approved before "
            f"designs can be created again."
        )

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