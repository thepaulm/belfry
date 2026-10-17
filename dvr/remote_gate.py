"""Second authorization gate for requests that arrived from the internet.

Why this exists
---------------
Until 2026-10-17 the public path had exactly one gate: oauth2-proxy on EC2.
Its allow-list turned out to have never been enforced — `email_domains = ["*"]`
sets `allowAll` in oauth2-proxy's validator, which overwrites the
authenticated-emails-file result — so from the first commit until that date any
Google account on earth was authorized, and three accounts outside the
allow-list reached live video. Nothing on the Orin noticed, because the Orin
trusted whatever came out of the tunnel.

This module makes the Orin check for itself: different machine, different
process, different file, different code. An EC2-side misconfiguration alone is
no longer sufficient to expose the cameras.

Trust boundary is the port
--------------------------
nginx listens on two ports:

  :80             LAN. Unauthenticated by design — network position is the gate.
  127.0.0.1:8081  Where the EC2 reverse tunnel lands. Every request must carry
                  an allow-listed identity.

Port, not source IP: the tunnel forwards to the Orin's own loopback, so
tunnel traffic and local traffic are both 127.0.0.1 and can't be told apart
by address.

nginx enforces via `auth_request` against /api/internal/remote-gate, which
calls `check_request()` here. That placement matters — `/hls/` is proxied
straight from nginx to MediaMTX and never reaches FastAPI, so a Python-only
middleware would leave live video unprotected on the public path. The ASGI
middleware below is a backstop for FastAPI's own routes if the nginx block is
ever dropped or misedited.

Identity headers come from Caddy, which sets them only after its own gate
passes: `X-Auth-Request-Email` on the browser/oauth2-proxy path,
`X-Auth-Email` on the mobile bearer/cookie path. Caddy strips any
client-supplied copies at ingress (see cloud/Caddyfile), so a forged header
can't reach us, and a Caddy gate that fails open sends no header at all —
which this rejects.
"""
from __future__ import annotations

import logging
import os

from . import auth

logger = logging.getLogger("belfry.remote_gate")

# Set by the :8081 nginx server block via proxy_set_header, which replaces
# anything the client sent. Absent => the request did not arrive from the
# tunnel.
ORIGIN_HEADER = "x-belfry-origin"
REMOTE_ORIGIN = "remote"

# Checked in order. Browser path first, then the mobile bearer/cookie path.
EMAIL_HEADERS = ("x-auth-request-email", "x-auth-email")

# Reachable from the public side without an identity header, because they are
# how an identity is obtained in the first place. Both verify for themselves:
# /auth/exchange validates a Google ID token against Google's JWKS plus our
# client-ID and email allow-lists; /auth/verify validates a server JWT and is
# the forward_auth target Caddy calls to mint the very header we check for.
EXEMPT_PATHS = frozenset({"/auth/exchange", "/auth/verify"})

# nginx's auth_request target. Exempt from the ASGI middleware so the endpoint
# itself always runs and its X-Original-URI handling stays authoritative —
# otherwise the middleware 403s the subrequest first and the exemption logic
# below becomes dead code that silently stops working if `auth_request off`
# is ever dropped from a location. The endpoint returns only 204/403, never data.
GATE_PATH = "/api/internal/remote-gate"

_MIDDLEWARE_EXEMPT = EXEMPT_PATHS | {GATE_PATH}


class _AllowList:
    """Loaded once at startup, like dvr/auth.py — edit the file then restart
    belfry. A load failure is cached as "deny everything remote", never as
    "allow": the whole point of this module is to fail closed."""

    def __init__(self) -> None:
        self._emails: set[str] | None = None
        self._error: str = "allow-list not loaded"
        self.reload()

    def reload(self) -> None:
        try:
            self._emails = auth.load_allowed_emails()
            self._error = ""
            logger.info("remote gate armed: %d allowed address(es)", len(self._emails))
        except auth.AuthError as e:
            self._emails = None
            self._error = str(e)
            logger.error(
                "remote gate FAILED TO LOAD (%s) — all remote requests will be "
                "denied until this is fixed and belfry is restarted",
                e,
            )

    def check(self, email: str | None) -> tuple[bool, str]:
        if self._emails is None:
            return False, f"allow-list unavailable: {self._error}"
        if not email:
            return False, "no identity header on a remote-origin request"
        if email.strip().lower() not in self._emails:
            return False, f"email {email!r} not in allow-list"
        return True, ""


_allow_list: _AllowList | None = None


def _list() -> _AllowList:
    global _allow_list
    if _allow_list is None:
        _allow_list = _AllowList()
    return _allow_list


def reload_allow_list() -> None:
    _list().reload()


def enabled() -> bool:
    """Off switch for a LAN-only deployment with no tunnel. Defaults on —
    a gate you have to remember to turn on is not a gate."""
    return os.environ.get("BELFRY_REMOTE_GATE", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def is_remote(headers: dict[str, str]) -> bool:
    return headers.get(ORIGIN_HEADER, "").strip().lower() == REMOTE_ORIGIN


def email_from(headers: dict[str, str]) -> str | None:
    for h in EMAIL_HEADERS:
        v = headers.get(h)
        if v and v.strip():
            return v.strip()
    return None


def check_request(path: str, headers: dict[str, str]) -> tuple[bool, str]:
    """(allowed, reason). Header keys must be lowercase."""
    if not enabled():
        return True, "gate disabled"
    if not is_remote(headers):
        return True, "local origin"
    if path in EXEMPT_PATHS:
        return True, "exempt path"
    return _list().check(email_from(headers))


class RemoteAllowListGate:
    """Pure-ASGI backstop over FastAPI's own routes.

    Deliberately not BaseHTTPMiddleware: that wrapper buffers responses and
    breaks the SSE endpoints (/api/inference/live, /api/inference/playback).
    This one only reads request headers and either short-circuits or hands the
    untouched scope onward, so streaming is unaffected.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", ())
        }
        path = scope.get("path", "")
        ok, reason = (
            (True, "gate endpoint")
            if path in _MIDDLEWARE_EXEMPT
            else check_request(path, headers)
        )
        if ok:
            return await self.app(scope, receive, send)

        logger.warning(
            "remote gate denied %s %s: %s",
            scope.get("method", "?"),
            path,
            reason,
        )
        body = b'{"detail":"not authorized"}'
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
