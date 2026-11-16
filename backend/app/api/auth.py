"""Single-operator dashboard authentication (JWT bearer tokens, HS256)."""

from __future__ import annotations

import hmac
from datetime import timedelta
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings

bearer = HTTPBearer(auto_error=False)


def check_password(cfg: Settings, username: str, password: str) -> bool:
    ok_user = hmac.compare_digest(username.encode(), cfg.auth_username.encode())
    ok_pass = hmac.compare_digest(password.encode(), cfg.auth_password.get_secret_value().encode())
    return ok_user and ok_pass


def issue_token(cfg: Settings, username: str, now_ts: float) -> tuple[str, float]:
    exp = now_ts + timedelta(minutes=cfg.jwt_ttl_minutes).total_seconds()
    token = jwt.encode(
        {"sub": username, "iat": int(now_ts), "exp": int(exp)},
        cfg.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )
    return token, exp


def decode_token(cfg: Settings, token: str) -> str | None:
    try:
        claims = jwt.decode(
            token,
            cfg.jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            options={"require": ["sub", "exp"]},
        )
    except jwt.PyJWTError:
        return None
    return str(claims["sub"])


def require_user(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str:
    cfg: Settings = request.app.state.runtime.cfg
    user = decode_token(cfg, creds.credentials) if creds else None
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


User = Annotated[str, Depends(require_user)]
