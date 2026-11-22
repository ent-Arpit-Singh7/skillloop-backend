from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import settings

_hasher = PasswordHasher()
# A valid, fixed Argon2id hash used to perform the same expensive verification
# when an email address does not exist. Never use its plaintext as a real password.
DUMMY_PASSWORD_HASH = "$argon2id$v=19$m=65536,t=3,p=4$XWu/FlUwX82fbsI+PhREkA$kJr0AUBtNWmj4XloNJEexB0TmPNmadtDdtikQTihEkM"
ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    if len(password) < 8 or len(password) > 128:
        raise ValueError("Password must contain between 8 and 128 characters.")
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError, VerifyMismatchError, TypeError):
        return False


def create_access_token(subject_id: int, role: str) -> str:
    """role is 'user' or 'admin'. Admin tokens can never act as users and vice versa."""
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {"sub": str(subject_id), "role": role, "token_type": "access", "exp": expire}
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
        # Access tokens must be explicitly typed; never accept an untyped JWT
        # or a refresh token on protected API routes.
        return payload if payload.get("token_type") == "access" else None
    except jwt.PyJWTError:
        return None


def create_refresh_token(subject_id: int, role: str) -> str:
    """Issue a separate, longer-lived token intended only for /auth/refresh."""
    expire = datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days)
    payload = {"sub": str(subject_id), "role": role, "token_type": "refresh", "exp": expire}
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_refresh_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
        return payload if payload.get("token_type") == "refresh" else None
    except jwt.PyJWTError:
        return None
