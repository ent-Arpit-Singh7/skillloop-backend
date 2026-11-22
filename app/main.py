import logging

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg import errors as pg_errors
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import models  # noqa: F401  (registers the table descriptions with SQLAlchemy)
from app.core.config import settings
from app.core.rate_limit import AuthRateLimitMiddleware
from app.db.session import get_db
from app.routers import admin, auth, blocks, chat, notifications, profile, requests, search, sessions

log = logging.getLogger("skillloop")

app = FastAPI(title="SkillLoop API", version="1.2.0")

app.add_middleware(
    AuthRateLimitMiddleware,
    max_requests=settings.auth_rate_limit_requests,
    window_seconds=settings.auth_rate_limit_window_seconds,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (auth, profile, search, requests, sessions, chat, notifications, blocks, admin):
    app.include_router(r.router)

# The database enforces many project rules itself (triggers, unique indexes, checks).
# When it refuses something, turn its error into a clear JSON response instead of a crash.
FRIENDLY_CONFLICTS = {
    "uq_teachingskill_user_name": "You already have an active teaching skill with this name",
    "uq_learningskill_user_name": "You already have an active learning skill with this name",
    "uq_pending_connection_request": "You already have a pending request for this skill",
    "uq_pending_network_connection_request": "You already have a pending connection request",
    "uq_user_email_lower": "Email already registered",
    "user_email_key": "Email already registered",
    "ex_availability_no_overlap": "This time slot overlaps another slot on the same day",
    "review_session_id_key": "This session has already been reviewed",
}


@app.exception_handler(DBAPIError)
async def database_error_handler(request: Request, exc: DBAPIError):
    orig = exc.orig
    first_line = str(orig).splitlines()[0] if orig else "Database error"
    constraint = getattr(getattr(orig, "diag", None), "constraint_name", None)
    if isinstance(orig, pg_errors.RaiseException):  # a business-rule trigger said no
        return JSONResponse(status_code=400, content={"detail": first_line})
    if isinstance(orig, (pg_errors.UniqueViolation, pg_errors.ExclusionViolation)):
        return JSONResponse(status_code=409, content={"detail": FRIENDLY_CONFLICTS.get(constraint, "This record already exists")})
    if isinstance(orig, pg_errors.ForeignKeyViolation):
        return JSONResponse(status_code=409, content={"detail": "This record is linked to other data or refers to something that does not exist"})
    if isinstance(orig, pg_errors.CheckViolation):
        return JSONResponse(status_code=400, content={"detail": f"Invalid data ({constraint or 'check failed'})"})
    log.exception("Unhandled database error")
    return JSONResponse(status_code=500, content={"detail": "Database error"})


@app.get("/api/health", tags=["health"])
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok", "database": "connected"}
