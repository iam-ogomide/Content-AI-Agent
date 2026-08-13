"""Canva OAuth helper for designer.py.

WHERE THE TOKEN LIVES: MongoDB when MONGO_URI is set, otherwise a local JSON
file next to this module. Mongo is what makes this shareable — one person
authorizes once, and every other machine (and a deployed server) reads that
same token instead of opening its own browser tab. The file fallback keeps
this runnable for anyone without database access.

The file alone was never enough for a second machine or a real deployment: a
container that gets recreated loses the file, and a server can't open a
browser to recover — so it would fail permanently, not just once.

SECURITY: in Mongo, these tokens are readable by anything holding MONGO_URI.
A Canva refresh token lets its holder act on that Canva account, so treat the
URI as the credential it is. This is a deliberate trade for shared access; a
secrets manager is the stricter option if that stops being acceptable.

SETUP (once for the whole team, not once per person):
  1. ONE person runs a design request interactively, on a machine with a real
     browser. The mcp library registers a client with Canva automatically on
     first connection (no manual curl step needed — that was true of an older
     flow, current mcp versions handle it internally and cache the result via
     the storage below).
  2. A browser tab opens asking them to log into the Canva account this
     service should act as — the shared/brand account, not a personal one,
     since every design this system makes lands wherever that login points.
  3. With Mongo configured, that's it for everyone. Other machines read the
     stored token and never see a browser. Without Mongo, each machine repeats
     steps 1-2 with its own local file and its own Canva account.
  4. If the stored token gets revoked, or expires with no refresh token, step 1
     has to happen again. That's a Canva auth-model constraint, not something
     this code can route around.

A browser prompt at any other time is a BUG, not normal operation — the
access token lasts 4 hours and the refresh token renews it without a browser.
See the long comment in _CachedTokenStorage.get_tokens for the one that caused
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
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)

load_dotenv()

TOKEN_CACHE_PATH = Path(__file__).resolve().parent / ".canva_tokens.json"
REDIRECT_URI = os.getenv("CANVA_REDIRECT_URI", "http://localhost:8765/callback")

# Same database as the conversation store (see orchestrator.py), its own
# collection. Absent MONGO_URI, everything here falls back to the local file.
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB")
TOKENS_COLLECTION_NAME = "content_ai_canva_auth"

_mongo_client = None

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


class _CachedTokenStorage(TokenStorage):
    """The token-handling logic, over an abstract read/write of one dict.

    Subclasses supply only _read and _write — a file or a Mongo document. All
    the parts that are easy to get wrong (expiry bookkeeping, the blanked
    access_token that forces a refresh) live here once, so both backends
    behave identically and a fix can't land in one and miss the other.

    The stored dict is the same shape either way:
        {"tokens": {...}, "expires_at": <float|None>, "client_info": {...}}
    """

    def _read(self) -> dict:
        raise NotImplementedError

    def _write(self, data: dict) -> None:
        raise NotImplementedError

    async def get_tokens(self) -> OAuthToken | None:
        stored = self._read()
        data = stored.get("tokens")
        if not data:
            return None

        # Hand back an EXPIRED token with its access_token blanked, keeping the
        # refresh_token. This looks odd and is load-bearing.
        #
        # OAuthClientProvider only learns a token's expiry time when it receives
        # the token itself, in memory. Restored from storage, its expiry is
        # unknown, and is_token_valid() reads "unknown" as "valid":
        #     ... and (not self.token_expiry_time or time.time() <= ...)
        # So it skipped the refresh branch, sent a stale access token, got a 401,
        # and ran the FULL browser authorization flow — asking someone to log in
        # every few hours while a working refresh_token sat in the cache unused.
        #
        # An empty access_token fails is_token_valid() while can_refresh_token()
        # still passes, which is exactly the refresh path. Only the copy we
        # return is blanked; storage keeps both tokens.
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


class FileTokenStorage(_CachedTokenStorage):
    """Local-file cache. Per-machine, and per-Canva-account as a result.

    The fallback when there's no MONGO_URI. Fine for one person on one laptop;
    it can't be shared, and it doesn't survive a container restart — see the
    module docstring.
    """

    def __init__(self, path: Path = TOKEN_CACHE_PATH):
        self._path = path

    def _read(self) -> dict:
        if not self._path.exists():
            return {}
        return json.loads(self._path.read_text())

    def _write(self, data: dict) -> None:
        self._path.write_text(json.dumps(data, indent=2))
        # Tokens are secrets, so lock the file to its owner. Note this only
        # bites on POSIX: Windows ignores everything here except the read-only
        # bit, and the file stays 0o666 there — verified, not assumed. Don't
        # rely on this as the only thing protecting the cache.
        os.chmod(self._path, 0o600)


class MongoTokenStorage(_CachedTokenStorage):
    """Shared cache in MongoDB, so one browser login covers every machine.

    One document under a fixed _id, because there is exactly one Canva identity
    this service acts as. Not keyed per user: the point is that everybody's
    designs land in the same brand account.

    Mongo failures degrade to the local file rather than breaking a design.
    A read that fails would otherwise look like "no token" and open a browser;
    a write that fails would drop a freshly rotated refresh_token, and since
    Canva invalidates the old one on rotation, that means a forced re-login.
    Keeping the file in step means the run survives an unreachable database.
    """

    DOC_ID = "canva_oauth"

    def __init__(self, collection, fallback: FileTokenStorage | None = None):
        self._collection = collection
        self._fallback = fallback or FileTokenStorage()

    def _read(self) -> dict:
        try:
            doc = self._collection.find_one({"_id": self.DOC_ID}) or {}
        except PyMongoError as e:
            print(f"Canva token store unreachable ({e}); using the local file "
                  f"copy for this run.", flush=True)
            return self._fallback._read()
        doc.pop("_id", None)
        return doc

    def _write(self, data: dict) -> None:
        # The file copy is written first and unconditionally: if Mongo then
        # fails, the rotated token still exists somewhere on this machine.
        self._fallback._write(data)
        try:
            self._collection.update_one(
                {"_id": self.DOC_ID}, {"$set": data}, upsert=True
            )
        except PyMongoError as e:
            print(f"Couldn't save the Canva token to Mongo ({e}). It's cached "
                  f"locally, so this machine is fine — other machines may need "
                  f"to re-authorize.", flush=True)


def _token_collection():
    """The shared token collection, or None when Mongo isn't configured.

    Built lazily: this module is imported whenever designer.py is, but the vast
    majority of turns never touch Canva, and a MongoClient per import is a
    connection pool nobody asked for.
    """
    global _mongo_client
    if not (MONGO_URI and MONGO_DB):
        return None
    if _mongo_client is None:
        # Fail fast rather than blocking a design request for 30s on a URI that
        # can't be reached — the file fallback is right there.
        _mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    return _mongo_client[MONGO_DB][TOKENS_COLLECTION_NAME]


def _get_storage() -> _CachedTokenStorage:
    """Pick the token store, and move an existing local token into it once.

    The seeding step is what makes switching to Mongo invisible: whoever
    authorized before this change keeps their token, and it becomes the one
    everyone else uses. Without it, the first Mongo run would find an empty
    collection and open a browser despite a perfectly good token on disk.
    """
    collection = _token_collection()
    if collection is None:
        return FileTokenStorage()

    storage = MongoTokenStorage(collection)
    local = FileTokenStorage()
    if local._read() and not storage._read():
        storage._write(local._read())
        print("Moved the existing Canva token into MongoDB — other machines can "
              "now use it without authorizing again.", flush=True)
    return storage


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

    The first call with no token stored anywhere triggers the one-time browser
    login (see the module docstring's SETUP section). Every call after that
    reuses the stored token — from Mongo if configured, so other machines are
    covered too — refreshing it when needed, which OAuthClientProvider handles
    on its own.
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
        storage=_get_storage(),
        redirect_handler=_open_browser_for_auth,
        callback_handler=_wait_for_callback,
    )