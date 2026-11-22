"""In-app notifications (created only by backend events) and user reports."""
from datetime import datetime, timedelta, timezone
from hashlib import blake2b

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.core.rate_limit import report_target_limiter
from app.db.session import get_db
from app.deps import get_current_user
from app.models import LearningSkill, Message, Notification, Report, Review, TeachingSkill, User
from app.routers.chat import _conversation_context
from app.schemas import NotificationOut, ReportIn, ReportOut, UnreadCountOut

router = APIRouter(prefix="/api", tags=["notifications-and-reports"])


@router.get("/notifications", response_model=list[NotificationOut])
def list_notifications(
    unread_only: bool = False,
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    me: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Newest first. Read notifications stay in the list (nothing is auto-deleted)."""
    stmt = select(Notification).where(Notification.recipient_id == me.user_id)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    return db.scalars(stmt.order_by(Notification.notification_id.desc()).limit(limit).offset(offset)).all()


@router.get("/notifications/unread-count", response_model=UnreadCountOut)
def unread_count(me: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """For the badge on the bell icon."""
    n = db.scalar(
        select(func.count(Notification.notification_id)).where(
            Notification.recipient_id == me.user_id, Notification.read_at.is_(None)
        )
    )
    return UnreadCountOut(unread_count=n or 0)


@router.post("/notifications/{notification_id}/read", response_model=NotificationOut)
def open_notification(notification_id: int, me: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Call this when the user opens a notification. It marks it Read (and does nothing if already read)."""
    n = db.get(Notification, notification_id)
    if n is None or n.recipient_id != me.user_id:
        raise HTTPException(status_code=404, detail="Notification not found")
    if n.read_at is None:
        n.read_at = func.now()  # the database clock, so it is never earlier than created_at
        db.commit()
        db.refresh(n)
    return n


def _authorize_report_target(data: ReportIn, me: User, db: Session) -> tuple[str, int]:
    """Return a target key only when the reporter can access the target normally."""
    if data.reported_user_id is not None:
        if data.reported_user_id == me.user_id:
            raise HTTPException(status_code=400, detail="You cannot report yourself")
        target = db.get(User, data.reported_user_id)
        if target is None or target.account_status == "Deleted":
            raise HTTPException(status_code=404, detail="Report target not found")
        return "User", target.user_id

    content_id = data.content_id
    if data.content_type == "Message":
        message = db.get(Message, content_id)
        if message is None:
            raise HTTPException(status_code=404, detail="Report target not found")
        try:
            # Keep report access identical to existing conversation/message access.
            _conversation_context(db, message.conversation_id, me)
        except HTTPException:
            raise HTTPException(status_code=404, detail="Report target not found") from None
    elif data.content_type == "Review":
        review = db.get(Review, content_id)
        teacher = db.get(User, review.teacher_id) if review is not None else None
        # Reviews are reportable only while they are exposed on an active public profile.
        if review is None or teacher is None or teacher.account_status != "Active":
            raise HTTPException(status_code=404, detail="Report target not found")
    elif data.content_type == "TeachingSkill":
        skill = db.get(TeachingSkill, content_id)
        owner = db.get(User, skill.user_id) if skill is not None else None
        # Public profiles expose active teaching skills for active users only.
        if skill is None or not skill.is_active or owner is None or owner.account_status != "Active":
            raise HTTPException(status_code=404, detail="Report target not found")
    elif data.content_type == "LearningSkill":
        skill = db.get(LearningSkill, content_id)
        # Learning skills are private and are only exposed from /api/me/learning-skills.
        if skill is None or not skill.is_active or skill.user_id != me.user_id:
            raise HTTPException(status_code=404, detail="Report target not found")
    else:
        raise HTTPException(status_code=404, detail="Report target not found")
    return data.content_type, content_id


@router.post("/reports", response_model=ReportOut, status_code=201)
def create_report(data: ReportIn, me: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Report only a user or content item the authenticated reporter can access."""
    target_type, target_id = _authorize_report_target(data, me, db)
    retry_after = report_target_limiter.claim(me.user_id, target_type, target_id)
    if retry_after:
        raise HTTPException(
            status_code=409,
            detail="You have already reported this target recently.",
            headers={"Retry-After": str(retry_after)},
        )

    try:
        # Serialize duplicate checks across workers without adding a schema migration.
        lock_payload = f"{me.user_id}:{target_type}:{target_id}".encode("ascii")
        lock_key = int.from_bytes(blake2b(lock_payload, digest_size=8).digest(), "big", signed=True)
        db.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key})
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        duplicate_query = select(Report.report_id).where(
            Report.reporter_user_id == me.user_id,
            Report.created_at >= cutoff,
        )
        if target_type == "User":
            duplicate_query = duplicate_query.where(Report.reported_user_id == target_id)
        else:
            duplicate_query = duplicate_query.where(
                Report.content_type == target_type,
                Report.content_id == target_id,
            )
        if db.scalar(duplicate_query) is not None:
            report_target_limiter.release(me.user_id, target_type, target_id)
            raise HTTPException(
                status_code=409,
                detail="You have already reported this target recently.",
                headers={"Retry-After": str(24 * 60 * 60)},
            )

        report = Report(reporter_user_id=me.user_id, **data.model_dump())
        db.add(report)
        db.commit()
        db.refresh(report)
        return report
    except HTTPException:
        db.rollback()
        raise
    except DBAPIError as exc:
        db.rollback()
        report_target_limiter.release(me.user_id, target_type, target_id)
        if isinstance(exc, IntegrityError):
            raise HTTPException(status_code=404, detail="Report target not found or no longer available.") from None
        raise HTTPException(status_code=503, detail="The report could not be verified. Please try again.") from None
