from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.rate_limit import account_login_limiter
from app.core.security import (
    create_access_token, create_refresh_token, decode_refresh_token,
    DUMMY_PASSWORD_HASH, hash_password, verify_password,
)
from app.db.session import get_db
from app.deps import get_current_user
from app.models import Admin, User
from app.schemas import LoginIn, MessageOut, RefreshIn, RegisterIn, TokenOut, UserOut

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=201)
def register(data: RegisterIn, db: Session = Depends(get_db)):
    """Creates a normal USER account. There is no way to register as admin."""
    if db.scalar(select(User).where(func.lower(User.email) == data.email)):
        raise HTTPException(status_code=409, detail="Email already registered")
    user = User(
        full_name=data.full_name.strip(),
        email=data.email,
        password_hash=hash_password(data.password),
        city=data.city,
        country=data.country,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=TokenOut)
def login(data: LoginIn, db: Session = Depends(get_db)):
    limited, retry_after = account_login_limiter.is_limited("user", data.email)
    if limited:
        raise HTTPException(status_code=429, detail="Too many failed login attempts. Please try again later.", headers={"Retry-After": str(retry_after)})
    user = db.scalar(select(User).where(func.lower(User.email) == data.email))
    # Always perform Argon2 verification, including for unknown email addresses,
    # to reduce timing-based account enumeration.
    password_hash = user.password_hash if user is not None else DUMMY_PASSWORD_HASH
    password_ok = verify_password(data.password, password_hash)
    if user is None or not password_ok:
        account_login_limiter.record_failure("user", data.email)
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    account_login_limiter.clear("user", data.email)
    if user.account_status != "Active":
        raise HTTPException(status_code=403, detail=f"This account is {user.account_status.lower()}. Login is not allowed.")
    return TokenOut(
        access_token=create_access_token(user.user_id, "user"),
        refresh_token=create_refresh_token(user.user_id, "user"),
    )


@router.post("/logout", response_model=MessageOut)
def logout(_: User = Depends(get_current_user)):
    """Tokens are stateless, so React must delete its stored token."""
    return MessageOut(detail="Logged out. Delete the token on the client.")


@router.post("/admin/login", response_model=TokenOut)
def admin_login(data: LoginIn, db: Session = Depends(get_db)):
    limited, retry_after = account_login_limiter.is_limited("admin", data.email)
    if limited:
        raise HTTPException(status_code=429, detail="Too many failed login attempts. Please try again later.", headers={"Retry-After": str(retry_after)})
    admin = db.scalar(select(Admin).where(func.lower(Admin.email) == data.email))
    password_hash = admin.password_hash if admin is not None else DUMMY_PASSWORD_HASH
    password_ok = verify_password(data.password, password_hash)
    if admin is None or not password_ok:
        account_login_limiter.record_failure("admin", data.email)
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    account_login_limiter.clear("admin", data.email)
    return TokenOut(
        access_token=create_access_token(admin.admin_id, "admin"),
        refresh_token=create_refresh_token(admin.admin_id, "admin"),
    )


@router.post("/refresh", response_model=TokenOut)
def refresh_tokens(data: RefreshIn, db: Session = Depends(get_db)):
    """Exchange a valid 14-day refresh token for a new access/refresh pair.

    Refresh tokens are signed and time-limited but stateless; logout must delete
    both tokens on the client. Server-side refresh-token revocation requires a
    persistent token-session table and is intentionally not claimed here.
    """
    payload = decode_refresh_token(data.refresh_token)
    if not payload or payload.get("role") not in ("user", "admin") or not str(payload.get("sub", "")).isdigit():
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    subject_id = int(payload["sub"])
    if payload["role"] == "user":
        subject = db.get(User, subject_id)
        if subject is None:
            raise HTTPException(status_code=401, detail="Account not found")
        if subject.account_status != "Active":
            raise HTTPException(status_code=403, detail="This account is not active")
    else:
        subject = db.get(Admin, subject_id)
        if subject is None:
            raise HTTPException(status_code=401, detail="Administrator not found")
    return TokenOut(
        access_token=create_access_token(subject_id, payload["role"]),
        refresh_token=create_refresh_token(subject_id, payload["role"]),
    )
