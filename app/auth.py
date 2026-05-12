from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
from jwt import InvalidTokenError
from passlib.context import CryptContext
from pydantic import BaseModel, ValidationError
from starlette.responses import Response

from app.config import settings

# `bcrypt` is intentionally not used for new hashes here.
# Recent bcrypt/passlib combinations can fail at runtime inside containers,
# while pbkdf2_sha256 works without an external backend and supports long passwords.
PASSWORD_CONTEXT = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


class SessionTokenPayload(BaseModel):
    sub: str
    email: str
    exp: int


def normalize_email(email: str) -> str:
    return email.strip().lower()


def hash_password(password: str) -> str:
    return PASSWORD_CONTEXT.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return PASSWORD_CONTEXT.verify(password, password_hash)


def create_session_token(*, user_id: str, email: str) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.AUTH_SESSION_TTL_SECONDS)
    payload = {
        "sub": user_id,
        "email": email,
        "exp": int(expires_at.timestamp()),
    }
    return jwt.encode(payload, settings.AUTH_JWT_SECRET, algorithm="HS256")


def decode_session_token(token: str) -> SessionTokenPayload | None:
    try:
        payload = jwt.decode(token, settings.AUTH_JWT_SECRET, algorithms=["HS256"])
        return SessionTokenPayload.model_validate(payload)
    except (InvalidTokenError, ValidationError):
        return None


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.AUTH_COOKIE_NAME,
        value=token,
        max_age=settings.AUTH_SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.AUTH_COOKIE_SECURE,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.AUTH_COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=settings.AUTH_COOKIE_SECURE,
        path="/",
    )
