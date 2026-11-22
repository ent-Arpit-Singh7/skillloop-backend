"""Reusable dependencies: who is calling, and are they allowed?"""
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.security import decode_access_token
from app.db.session import get_db
from app.models import Admin, User

bearer_scheme = HTTPBearer(auto_error=False)


def _payload(creds: HTTPAuthorizationCredentials | None, role: str) -> dict:
    """Checks the token is valid and was issued for this role ('user' or 'admin')."""
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if creds is None:
        raise unauthorized
    payload = decode_access_token(creds.credentials)
    if not payload or payload.get("role") != role or not str(payload.get("sub", "")).isdigit():
        raise unauthorized
    return payload


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    payload = _payload(creds, "user")
    user = db.get(User, int(payload["sub"]))
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if user.account_status != "Active":
        # A suspended/blocked user may still hold an old token: refuse it.
        raise HTTPException(status_code=403, detail=f"This account is {user.account_status}.")
    return user


def get_current_admin(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> Admin:
    payload = _payload(creds, "admin")
    admin = db.get(Admin, int(payload["sub"]))
    if admin is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return admin
