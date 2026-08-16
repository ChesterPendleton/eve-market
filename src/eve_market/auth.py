"""EVE SSO via OAuth2 with PKCE.

Desktop applications use the PKCE flow, which needs no client secret: the app
proves it started the exchange by holding a one-time verifier. Register the
application at https://developers.eveonline.com/applications as a native app
with the same callback URL configured here.

Tokens are written to a file in the user's config directory with 0600
permissions. The refresh token is a long-lived credential — anyone holding it
can read your character data until you revoke it in EVE's third-party
application settings.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://login.eveonline.com/v2/oauth/authorize/"
TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"

# Scopes this tool uses, and why each is needed.
SCOPES = [
    "esi-markets.read_character_orders.v1",  # your live orders, for relisting
    "esi-wallet.read_character_wallet.v1",  # transactions, to fill the ledger
    "esi-assets.read_assets.v1",  # stock on hand
    "esi-markets.structure_markets.v1",  # citadel order books you can dock at
    "esi-ui.open_window.v1",  # open the market window in the client
    "esi-ui.write_waypoint.v1",  # set a hauling route
]

# Refresh a little early; a token that expires mid-request is a wasted call.
EXPIRY_MARGIN_SECONDS = 60


def token_path() -> Path:
    """Where tokens live. Honours XDG on Linux, APPDATA on Windows."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "eve-market" / "tokens.json"


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float
    character_id: int
    character_name: str
    scopes: list[str] = field(default_factory=list)

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at - EXPIRY_MARGIN_SECONDS

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "character_id": self.character_id,
            "character_name": self.character_name,
            "scopes": self.scopes,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> Tokens:
        return cls(
            access_token=raw["access_token"],
            refresh_token=raw["refresh_token"],
            expires_at=raw["expires_at"],
            character_id=raw["character_id"],
            character_name=raw.get("character_name", ""),
            scopes=raw.get("scopes", []),
        )


class AuthError(RuntimeError):
    pass


def _pkce_pair() -> tuple[str, str]:
    """Generate a PKCE verifier and its S256 challenge."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def _decode_jwt_claims(token: str) -> dict:
    """Read the claims out of an access token without verifying the signature.

    Safe here only because the token came straight from CCP's token endpoint
    over TLS — we're reading our own character id from it, not trusting it as
    an authentication decision. Never do this with a token from elsewhere.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError) as exc:
        raise AuthError("could not read the access token's claims") from exc


def _character_from_claims(claims: dict) -> tuple[int, str]:
    # The subject looks like "CHARACTER:EVE:12345678".
    subject = claims.get("sub", "")
    try:
        character_id = int(subject.rsplit(":", 1)[1])
    except (IndexError, ValueError) as exc:
        raise AuthError(f"unexpected subject claim: {subject!r}") from exc
    return character_id, claims.get("name", "")


class _CallbackHandler(BaseHTTPRequestHandler):
    """Catches the one redirect back from CCP's login page.

    ``result`` is class-level on purpose: BaseHTTPRequestHandler is
    instantiated per request by the server, so there's no instance for the
    caller to read the query parameters back from.
    """

    result: ClassVar[dict] = {}

    def do_GET(self) -> None:
        params = parse_qs(urlparse(self.path).query)
        _CallbackHandler.result = {k: v[0] for k, v in params.items()}
        body = (
            b"<html><body style='font-family:sans-serif;padding:3rem'>"
            b"<h2>Authorised.</h2><p>You can close this tab and return to the "
            b"terminal.</p></body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Silence the default stderr logging."""


def _await_callback(port: int, timeout: float) -> dict:
    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = timeout
    _CallbackHandler.result = {}

    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout)
    server.server_close()

    if not _CallbackHandler.result:
        raise AuthError(
            f"no callback received on port {port} within {timeout:.0f}s. "
            "Check the callback URL registered for your application matches."
        )
    return _CallbackHandler.result


def login(
    client_id: str,
    callback_url: str,
    *,
    scopes: list[str] | None = None,
    timeout: float = 180.0,
    open_browser: bool = True,
) -> Tokens:
    """Run the interactive PKCE login and return fresh tokens."""
    if not client_id:
        raise AuthError(
            "no client id configured. Register a native application at "
            "https://developers.eveonline.com/applications and set EVE_CLIENT_ID."
        )

    scopes = scopes or SCOPES
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    port = urlparse(callback_url).port or 8000

    query = urlencode(
        {
            "response_type": "code",
            "redirect_uri": callback_url,
            "client_id": client_id,
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    url = f"{AUTHORIZE_URL}?{query}"

    if open_browser:
        webbrowser.open(url)

    result = _await_callback(port, timeout)

    if result.get("state") != state:
        # A mismatched state means the response didn't come from our request.
        raise AuthError("state mismatch on the OAuth callback; aborting")
    if "code" not in result:
        raise AuthError(f"login failed: {result.get('error_description', result)}")

    response = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": result["code"],
            "client_id": client_id,
            "code_verifier": verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30.0,
    )
    if response.status_code != 200:
        raise AuthError(f"token exchange failed ({response.status_code}): {response.text[:200]}")

    return _tokens_from_response(response.json(), scopes)


def refresh(client_id: str, tokens: Tokens) -> Tokens:
    """Exchange a refresh token for a new access token."""
    response = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "client_id": client_id,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30.0,
    )
    if response.status_code != 200:
        raise AuthError(
            f"refresh failed ({response.status_code}). Run `eve-market login` again. "
            f"{response.text[:200]}"
        )
    return _tokens_from_response(response.json(), tokens.scopes)


def _tokens_from_response(payload: dict, scopes: list[str]) -> Tokens:
    access = payload["access_token"]
    claims = _decode_jwt_claims(access)
    character_id, character_name = _character_from_claims(claims)
    granted = claims.get("scp", scopes)
    if isinstance(granted, str):
        granted = [granted]
    return Tokens(
        access_token=access,
        # A refresh response may or may not rotate the refresh token.
        refresh_token=payload.get("refresh_token", ""),
        expires_at=time.time() + float(payload.get("expires_in", 1200)),
        character_id=character_id,
        character_name=character_name,
        scopes=list(granted),
    )


def save(tokens: Tokens, path: Path | None = None) -> Path:
    """Persist tokens with owner-only permissions."""
    target = path or token_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    # Create with restrictive permissions rather than relaxing them afterwards,
    # so the secret is never briefly world-readable.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(tokens.to_dict(), fh)
    return target


def load(path: Path | None = None) -> Tokens | None:
    target = path or token_path()
    if not target.exists():
        return None
    try:
        return Tokens.from_dict(json.loads(target.read_text()))
    except (ValueError, KeyError):
        log.warning("token file at %s is unreadable; log in again", target)
        return None


def ensure_fresh(client_id: str, path: Path | None = None) -> Tokens | None:
    """Load tokens, refreshing and re-saving them if they've expired."""
    tokens = load(path)
    if tokens is None:
        return None
    if not tokens.expired:
        return tokens

    refreshed = refresh(client_id, tokens)
    if not refreshed.refresh_token:
        # CCP didn't rotate it, so carry the existing one forward.
        refreshed.refresh_token = tokens.refresh_token
    save(refreshed, path)
    return refreshed


def missing_scopes(tokens: Tokens, required: list[str]) -> list[str]:
    """Which of ``required`` the current grant is missing."""
    held = set(tokens.scopes)
    return [scope for scope in required if scope not in held]
