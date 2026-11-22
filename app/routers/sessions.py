"""Online sessions and reviews.

Flow: teacher proposes (Pending) -> learner accepts (Scheduled) or rejects (Rejected).
      Teacher may cancel a Scheduled session, and marks it Completed after it takes place.
      The learner may then review the teacher once per completed session. Reviews cannot be edited.
"""
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session as DbSession

from app.db.session import get_db
from app.deps import get_current_user
from app.models import Connection, Review, Session, TeachingSkill, User, UserBlock
from app.notifications import notify
from app.schemas import ReviewIn, ReviewOut, SessionOut, SessionProposeIn

router = APIRouter(prefix="/api", tags=["sessions-and-reviews"])


def _session_for(db: DbSession, session_id: int, me: User) -> Session:
    s = db.get(Session, session_id)
    if s is None or me.user_id not in (s.teacher_id, s.learner_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return s


def _out(s: Session, me: User) -> SessionOut:
    """The learner only sees the meeting link once the session is Scheduled."""
    out = SessionOut.model_validate(s)
    if me.user_id == s.learner_id and s.status in ("Pending", "Rejected"):
        out.meeting_link = None
    return out


def _require(s: Session, me: User, *, who: Literal["teacher", "learner"], status: str) -> None:
    owner = s.teacher_id if who == "teacher" else s.learner_id
    if owner != me.user_id:
        raise HTTPException(status_code=403, detail=f"Only the {who} can do this")
    if s.status != status:
        raise HTTPException(status_code=409, detail=f"Session is {s.status}, expected {status}")


@router.post("/sessions", response_model=SessionOut, status_code=201)
def propose_session(data: SessionProposeIn, me: User = Depends(get_current_user), db: DbSession = Depends(get_db)):
    conn = db.scalar(
        select(Connection)
        .where(Connection.connection_id == data.connection_id)
        .with_for_update()
    )
    if (
        conn is None
        or conn.status != "Active"
        or me.user_id not in (conn.user_one_id, conn.user_two_id)
    ):
        raise HTTPException(status_code=404, detail="Connection not found")
    skill = db.get(TeachingSkill, data.teaching_skill_id)
    if skill is None or not skill.is_active:
        raise HTTPException(status_code=404, detail="Teaching skill not found")
    # Only the teacher of the selected skill can propose a session.
    if skill.user_id != me.user_id:
        raise HTTPException(status_code=403, detail="Only the teacher of this skill can propose a session")
    if data.session_date < date.today():
        raise HTTPException(status_code=400, detail="session_date cannot be in the past")
    learner_id = conn.user_two_id if conn.user_one_id == me.user_id else conn.user_one_id
    blocked = db.scalar(select(UserBlock.block_id).where(
        or_(
            (UserBlock.blocker_id == me.user_id) & (UserBlock.blocked_id == learner_id),
            (UserBlock.blocker_id == learner_id) & (UserBlock.blocked_id == me.user_id),
        )
    ))
    if blocked:
        raise HTTPException(status_code=403, detail="Cannot schedule a session because one user has blocked the other")
    s = Session(
        connection_id=conn.connection_id,
        teaching_skill_id=skill.teaching_skill_id,
        teacher_id=me.user_id,
        learner_id=learner_id,
        topic=data.topic,
        session_date=data.session_date,
        start_time=data.start_time,
        duration_minutes=data.duration_minutes,
        meeting_link=data.meeting_link,
        optional_note=data.optional_note,
    )
    db.add(s)
    db.flush()
    notify(
        db, learner_id, "session_requested", "New session proposal",
        f"{me.full_name} proposed a session: {data.topic}.", "Session", s.session_id,
    )
    db.commit()
    db.refresh(s)
    return _out(s, me)


@router.get("/sessions", response_model=list[SessionOut])
def list_sessions(
    status: Literal["Pending", "Scheduled", "Rejected", "Cancelled", "Completed"] | None = None,
    role: Literal["teacher", "learner", "all"] = "all",
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    me: User = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    if role == "teacher":
        cond = Session.teacher_id == me.user_id
    elif role == "learner":
        cond = Session.learner_id == me.user_id
    else:
        cond = or_(Session.teacher_id == me.user_id, Session.learner_id == me.user_id)
    stmt = select(Session).where(cond)
    if status:
        stmt = stmt.where(Session.status == status)
    rows = db.scalars(
        stmt.order_by(Session.session_date.desc(), Session.start_time.desc()).limit(limit).offset(offset)
    ).all()
    return [_out(s, me) for s in rows]


@router.get("/sessions/{session_id}", response_model=SessionOut)
def get_session(session_id: int, me: User = Depends(get_current_user), db: DbSession = Depends(get_db)):
    return _out(_session_for(db, session_id, me), me)


@router.post("/sessions/{session_id}/accept", response_model=SessionOut)
def accept_session(session_id: int, me: User = Depends(get_current_user), db: DbSession = Depends(get_db)):
    s = _session_for(db, session_id, me)
    _require(s, me, who="learner", status="Pending")
    s.status = "Scheduled"
    db.flush()
    notify(
        db, s.teacher_id, "session_accepted", "Session accepted",
        f"{me.full_name} accepted your session: {s.topic}.", "Session", s.session_id,
    )
    db.commit()
    db.refresh(s)
    return _out(s, me)


@router.post("/sessions/{session_id}/reject", response_model=SessionOut)
def reject_session(session_id: int, me: User = Depends(get_current_user), db: DbSession = Depends(get_db)):
    s = _session_for(db, session_id, me)
    _require(s, me, who="learner", status="Pending")
    s.status = "Rejected"
    db.flush()
    notify(
        db, s.teacher_id, "session_rejected", "Session rejected",
        f"{me.full_name} rejected your session: {s.topic}.", "Session", s.session_id,
    )
    db.commit()
    db.refresh(s)
    return _out(s, me)


@router.post("/sessions/{session_id}/cancel", response_model=SessionOut)
def cancel_session(session_id: int, me: User = Depends(get_current_user), db: DbSession = Depends(get_db)):
    """Teacher only, and only a Scheduled session. Learners cannot cancel in the MVP."""
    s = _session_for(db, session_id, me)
    _require(s, me, who="teacher", status="Scheduled")
    s.status = "Cancelled"
    db.flush()
    notify(
        db, s.learner_id, "session_cancelled", "Session cancelled",
        f"{me.full_name} cancelled the session: {s.topic}.", "Session", s.session_id,
    )
    db.commit()
    db.refresh(s)
    return _out(s, me)


@router.post("/sessions/{session_id}/complete", response_model=SessionOut)
def complete_session(session_id: int, me: User = Depends(get_current_user), db: DbSession = Depends(get_db)):
    """Teacher manually marks a Scheduled session Completed (the database refuses this before the session date)."""
    s = _session_for(db, session_id, me)
    _require(s, me, who="teacher", status="Scheduled")
    s.status = "Completed"
    db.commit()
    db.refresh(s)
    return _out(s, me)


# ---------- Reviews (learner reviews teacher; no editing, no deleting) ----------
@router.post("/sessions/{session_id}/review", response_model=ReviewOut, status_code=201)
def create_review(
    session_id: int, data: ReviewIn, me: User = Depends(get_current_user), db: DbSession = Depends(get_db)
):
    s = _session_for(db, session_id, me)
    if s.learner_id != me.user_id:
        raise HTTPException(status_code=403, detail="Only the learner can review a session")
    if s.status != "Completed":
        raise HTTPException(status_code=400, detail="You can only review a completed session")
    if db.scalar(select(Review.review_id).where(Review.session_id == s.session_id)):
        raise HTTPException(status_code=409, detail="This session has already been reviewed")
    review = Review(
        session_id=s.session_id,
        learner_id=me.user_id,
        teacher_id=s.teacher_id,
        rating=data.rating,
        written_review=data.written_review,
    )
    db.add(review)
    db.flush()
    notify(
        db, s.teacher_id, "new_review_received", "New review",
        f"{me.full_name} rated you {data.rating} star(s).", "Review", review.review_id,
    )
    db.commit()
    db.refresh(review)
    return review


@router.get("/sessions/{session_id}/review", response_model=ReviewOut)
def get_session_review(session_id: int, me: User = Depends(get_current_user), db: DbSession = Depends(get_db)):
    s = _session_for(db, session_id, me)
    review = db.scalar(select(Review).where(Review.session_id == s.session_id))
    if review is None:
        raise HTTPException(status_code=404, detail="No review for this session yet")
    return review
