"""Mobile-app auth: Google ID token verification + server JWT mint/verify.

The Flutter app does native Google Sign-In, gets an ID token, POSTs it to
/auth/exchange, and gets back a server-signed JWT it uses on every other
request via `Authorization: Bearer`. The cookie/oauth2-proxy path used by
the browser is untouched; bearer is an additive second front door.

Trust chain on the public path (`example.com`):
  app -> Caddy -> (sees Authorization: Bearer, forward_auths to /auth/verify
                   instead of oauth2-proxy) -> tunnel -> Orin nginx -> upstream

So /auth/verify is the Caddy hook; it does not gate any other route here.
The LAN path stays open because Caddy is the WAN gate — running the FastAPI
process directly on the LAN trusts the network, same as it did before.

Config (all loaded lazily on first call so importing this module is cheap
and unit-friendly):
  BELFRY_JWT_SECRET           — random hex, HS256 signing key
  BELFRY_GOOGLE_CLIENT_IDS    — comma-separated list of acceptable `aud`s
                                (one per mobile platform; web client stays
                                with oauth2-proxy on EC2)
  BELFRY_ALLOWED_EMAILS_FILE  — defaults to /etc/belfry/allowed-emails;
                                same one-email-per-line format as
                                /etc/oauth2-proxy/emails on EC2
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import jwt as pyjwt
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

_JWT_ALG = "HS256"
# 30 days. No refresh endpoint — a leaked token is valid for the window,
# which is acceptable for a personal app. To revoke earlier, remove the
# email from /etc/belfry/allowed-emails and restart belfry: verify_jwt
# re-checks the allow-list on every call.
_JWT_TTL_SECONDS = 30 * 24 * 3600


class AuthError(Exception):
    """Any verification failure. Callers translate to HTTP 401."""


_initialized = False
_secret: str = ""
_client_ids: set[str] = set()
_allowed_emails: set[str] = set()
_google_request: google_requests.Request | None = None


def _init() -> None:
    """First-call init so importing this module never crashes a dev shell
    that doesn't have the env wired up. Raises AuthError if config is
    missing — endpoints turn that into a 503 with a useful message."""
    global _initialized, _secret, _client_ids, _allowed_emails, _google_request
    if _initialized:
        return
    secret = os.environ.get("BELFRY_JWT_SECRET", "")
    if not secret:
        raise AuthError("BELFRY_JWT_SECRET is not set")
    client_ids = {
        s.strip()
        for s in os.environ.get("BELFRY_GOOGLE_CLIENT_IDS", "").split(",")
        if s.strip()
    }
    if not client_ids:
        raise AuthError("BELFRY_GOOGLE_CLIENT_IDS is empty")
    emails_file = Path(
        os.environ.get("BELFRY_ALLOWED_EMAILS_FILE", "/etc/belfry/allowed-emails")
    )
    if not emails_file.is_file():
        raise AuthError(f"{emails_file} does not exist")
    allowed = {
        line.strip().lower()
        for line in emails_file.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    if not allowed:
        raise AuthError(f"{emails_file} has no entries")
    _secret = secret
    _client_ids = client_ids
    _allowed_emails = allowed
    _google_request = google_requests.Request()
    _initialized = True


def verify_google_id_token(token: str) -> str:
    """Verify a Google ID token against Google's JWKS; check `aud` and
    `email`. Returns the verified email."""
    _init()
    try:
        info = google_id_token.verify_oauth2_token(
            token, _google_request, audience=None
        )
    except ValueError as e:
        raise AuthError(f"google id token invalid: {e}") from e
    if info.get("aud") not in _client_ids:
        raise AuthError("google id token aud not in allow-list")
    if not info.get("email_verified"):
        raise AuthError("google email not verified")
    email = str(info.get("email", "")).lower()
    if email not in _allowed_emails:
        raise AuthError(f"email {email!r} not in allow-list")
    return email


def mint_jwt(email: str) -> tuple[str, int]:
    """Returns (token, expires_at_unix). Token carries sub=email, iat, exp."""
    _init()
    now = int(time.time())
    exp = now + _JWT_TTL_SECONDS
    payload = {"sub": email, "iat": now, "exp": exp}
    return pyjwt.encode(payload, _secret, algorithm=_JWT_ALG), exp


def verify_jwt(token: str) -> str:
    """Returns the email on success. Re-checks the allow-list every call
    so removing someone from the file locks them out within one restart,
    not a 30-day window."""
    _init()
    try:
        payload = pyjwt.decode(token, _secret, algorithms=[_JWT_ALG])
    except pyjwt.PyJWTError as e:
        raise AuthError(f"bearer invalid: {e}") from e
    email = str(payload.get("sub", "")).lower()
    if email not in _allowed_emails:
        raise AuthError(f"email {email!r} no longer in allow-list")
    return email


def extract_bearer(authorization_header: str | None) -> str:
    """Pull the token out of `Authorization: Bearer <token>`."""
    if not authorization_header:
        raise AuthError("missing Authorization header")
    parts = authorization_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise AuthError("malformed Authorization header")
    return parts[1].strip()
