"""Search & Discover. Plain search and filters only: no AI matching, no recommendations."""
from datetime import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.deps import get_current_user
from app.models import (
    Availability,
    Connection,
    Language,
    Review,
    TeachingSkill,
    User,
    UserLanguage,
    UserBlock,
)
from app.schemas import (
    AvailabilityOut,
    Day,
    LanguageOut,
    Level,
    PersonCardOut,
    PublicProfileOut,
    PublicUserOut,
    ReviewOut,
    TeachingSkillOut,
)

router = APIRouter(prefix="/api", tags=["search"])


def _ratings(db: Session, user_ids: list[int]) -> dict[int, tuple[float, int]]:
    if not user_ids:
        return {}
    rows = db.execute(
        select(Review.teacher_id, func.avg(Review.rating), func.count(Review.review_id))
        .where(Review.teacher_id.in_(user_ids))
        .group_by(Review.teacher_id)
    ).all()
    return {tid: (round(float(avg), 2), int(cnt)) for tid, avg, cnt in rows}


def _build_cards(db: Session, users: list[User], skill_filters: list) -> list[PersonCardOut]:
    ids = [u.user_id for u in users]
    if not ids:
        return []
    skills = db.scalars(
        select(TeachingSkill)
        .where(TeachingSkill.user_id.in_(ids), TeachingSkill.is_active.is_(True), *skill_filters)
        .order_by(TeachingSkill.teaching_skill_id)
    ).all()
    lang_rows = db.execute(
        select(UserLanguage.user_id, Language)
        .join(Language, Language.language_id == UserLanguage.language_id)
        .where(UserLanguage.user_id.in_(ids))
        .order_by(Language.language_name)
    ).all()
    slots = db.scalars(select(Availability).where(Availability.user_id.in_(ids))).all()
    ratings = _ratings(db, ids)
    connection_counts = {user_id: 0 for user_id in ids}
    connection_rows = db.execute(
        select(Connection.user_one_id, Connection.user_two_id).where(
            Connection.status == "Active",
            or_(Connection.user_one_id.in_(ids), Connection.user_two_id.in_(ids)),
        )
    ).all()
    for user_one_id, user_two_id in connection_rows:
        if user_one_id in connection_counts:
            connection_counts[user_one_id] += 1
        if user_two_id in connection_counts:
            connection_counts[user_two_id] += 1
    cards = []
    for u in users:
        avg, cnt = ratings.get(u.user_id, (None, 0))
        cards.append(
            PersonCardOut(
                user=PublicUserOut.model_validate(u),
                skills=[TeachingSkillOut.model_validate(s) for s in skills if s.user_id == u.user_id],
                languages=[LanguageOut.model_validate(lang) for uid, lang in lang_rows if uid == u.user_id],
                availability=[AvailabilityOut.model_validate(a) for a in slots if a.user_id == u.user_id],
                average_rating=avg,
                review_count=cnt,
                connection_count=connection_counts[u.user_id],
            )
        )
    return cards


@router.get("/search", response_model=list[PersonCardOut])
def search(
    mode: Literal["skill", "person"] = Query(default="skill", description="Search by skill name or by person name"),
    q: str | None = Query(default=None, description="Text to look for (case-insensitive, partial match)"),
    category_id: int | None = None,
    skill_level: Level | None = None,
    language_id: int | None = None,
    day_of_week: Day | None = None,
    start_time: time | None = Query(default=None, description="With day_of_week: slot must overlap this window"),
    end_time: time | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    me: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """People who can teach something. Never includes yourself or suspended/blocked users."""
    if (start_time or end_time) and not day_of_week:
        raise HTTPException(status_code=422, detail="start_time / end_time need day_of_week")
    like = f"%{q.strip()}%" if q and q.strip() else None

    skill_filters = []
    if mode == "skill" and like:
        skill_filters.append(TeachingSkill.skill_name.ilike(like))
    if category_id:
        skill_filters.append(TeachingSkill.category_id == category_id)
    if skill_level:
        skill_filters.append(TeachingSkill.skill_level == skill_level)

    stmt = select(User).where(User.account_status == "Active", User.user_id != me.user_id)
    stmt = stmt.where(~exists().where(or_(
        and_(UserBlock.blocker_id == me.user_id, UserBlock.blocked_id == User.user_id),
        and_(UserBlock.blocker_id == User.user_id, UserBlock.blocked_id == me.user_id),
    )))
    stmt = stmt.where(
        exists().where(
            and_(
                TeachingSkill.user_id == User.user_id,
                TeachingSkill.is_active.is_(True),
                *skill_filters,
            )
        )
    )
    if mode == "person" and like:
        stmt = stmt.where(User.full_name.ilike(like))
    if language_id:
        stmt = stmt.where(
            exists().where(and_(UserLanguage.user_id == User.user_id, UserLanguage.language_id == language_id))
        )
    if day_of_week:
        slot_cond = [Availability.user_id == User.user_id, Availability.day_of_week == day_of_week]
        if start_time:
            slot_cond.append(Availability.end_time > start_time)
        if end_time:
            slot_cond.append(Availability.start_time < end_time)
        stmt = stmt.where(exists().where(and_(*slot_cond)))

    users = db.scalars(stmt.order_by(User.user_id).limit(limit).offset(offset)).all()
    return _build_cards(db, list(users), skill_filters)


@router.get("/users/{user_id}", response_model=PublicProfileOut)
def get_profile(user_id: int, _: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """A user's profile page: info, skills they teach, languages, availability, rating."""
    user = db.get(User, user_id)
    if user is None or user.account_status != "Active":
        raise HTTPException(status_code=404, detail="User not found")
    return _build_cards(db, [user], [])[0]


@router.get("/users/{user_id}/reviews", response_model=list[ReviewOut])
def get_user_reviews(
    user_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Reviews shown on the active teacher's public profile."""
    teacher = db.get(User, user_id)
    if teacher is None or teacher.account_status != "Active":
        raise HTTPException(status_code=404, detail="User not found")
    return db.scalars(
        select(Review).where(Review.teacher_id == user_id).order_by(Review.review_id.desc()).limit(limit).offset(offset)
    ).all()
