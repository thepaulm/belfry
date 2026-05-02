"""
Auth for both LAN (HTTP Basic) and cloud (oauth2-proxy → Caddy → tunnel).

Flow:
  - LAN browser hits Orin nginx directly. nginx clears any inbound
    X-Auth-Request-Email so a LAN client can't forge it. require_auth falls
    through to HTTP Basic.
  - Public browser hits Caddy on EC2 → oauth2-proxy authenticates → Caddy
    adds X-Auth-Request-Email → reverse-tunnel to Orin nginx. Because the
    request lands on Orin's loopback (127.0.0.1) via the SSH tunnel,
    nginx's map promotes the header to X-Forwarded-Auth-Email. require_auth
    sees a non-empty value and accepts.

The allowlist itself is enforced by oauth2-proxy on EC2; this module just
trusts that any non-empty X-Forwarded-Auth-Email reached us through that
gated path.
"""
from __future__ import annotations

import os
import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

USERNAME = os.environ.get("DVR_USERNAME", "admin")
PASSWORD = os.environ["DVR_PASSWORD"]  # required; fail fast if absent

_basic = HTTPBasic(auto_error=False)


def require_auth(
    request: Request,
    creds: HTTPBasicCredentials | None = Depends(_basic),
) -> str:
    email = request.headers.get("X-Forwarded-Auth-Email", "").strip()
    if email:
        return email

    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="auth required",
            headers={"WWW-Authenticate": "Basic"},
        )
    user_ok = secrets.compare_digest(creds.username.encode(), USERNAME.encode())
    pass_ok = secrets.compare_digest(creds.password.encode(), PASSWORD.encode())
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return creds.username
