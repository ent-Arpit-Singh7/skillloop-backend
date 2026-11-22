"""Admin: dashboard overview, manage users, manage skill categories, review reports.

Needs an ADMIN token (from /api/auth/admin/login). Normal user tokens are refused.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.deps import get_current_admin
from app.models import (
    Admin,
    LearningSkill,
    Message,
    Report,
    Review,
    SkillCategory,
    TeachingSkill,
    User,
)
from app.schemas import (
    AdminOverviewOut,
    AdminUserOut,
    AdminUserUpdateIn,
    CategoryIn,
    CategoryOut,
    CategoryUpdateIn,
    ReportOut,
    ReportUpdateIn,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---------- Dashboard ----------
@router.get("/overview", response_model=AdminOverviewOut)
def overview(_: Admin = Depends(get_current_admin), db: Session = Depends(get_db)):
    def count(model, *conds):
        return db.scalar(select(func.count()).select_from(model).where(*conds)) or 0

    return AdminOverviewOut(
        total_users=count(User),
        active_users=count(User, User.account_status == "Active"),
        suspended_users=count(User, User.account_status == "Suspended"),
        blocked_users=count(User, User.account_status == "Blocked"),
        categories=count(SkillCategory),
        pending_reports=count(Report, Report.status == "Pending"),
        under_review_reports=count(Report, Report.status == "Under Review"),
    )


# ---------- Users ----------
@router.get("/users", response_model=list[AdminUserOut])
def list_users(
    q: str | None = None,
    account_status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: Admin = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    stmt = select(User)
    if q and q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(User.email.ilike(like), User.full_name.ilike(like)))
    if account_status:
        stmt = stmt.where(User.account_status == account_status)
    return db.scalars(stmt.order_by(User.user_id).limit(limit).offset(offset)).all()


@router.patch("/users/{user_id}", response_model=AdminUserOut)
def update_user_status(
    user_id: int, data: AdminUserUpdateIn, _: Admin = Depends(get_current_admin), db: Session = Depends(get_db)
):
    """Suspend, block, or re-activate a user."""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.account_status == "Deleted":
        raise HTTPException(status_code=409, detail="A deleted account cannot be reactivated")
    user.account_status = data.account_status
    db.commit()
    db.refresh(user)
    return user


# ---------- Skill categories (only admins can create them) ----------
@router.get("/categories", response_model=list[CategoryOut])
def list_categories(_: Admin = Depends(get_current_admin), db: Session = Depends(get_db)):
    return db.scalars(select(SkillCategory).order_by(SkillCategory.category_name)).all()


@router.post("/categories", response_model=CategoryOut, status_code=201)
def create_category(data: CategoryIn, admin: Admin = Depends(get_current_admin), db: Session = Depends(get_db)):
    if db.scalar(select(SkillCategory.category_id).where(SkillCategory.category_name == data.category_name)):
        raise HTTPException(status_code=409, detail="Category already exists")
    cat = SkillCategory(
        category_name=data.category_name.strip(), description=data.description, created_by_admin_id=admin.admin_id
    )
    db.add(cat)
    db.commit()
    db.refresh(cat)
    return cat


@router.patch("/categories/{category_id}", response_model=CategoryOut)
def update_category(
    category_id: int, data: CategoryUpdateIn, _: Admin = Depends(get_current_admin), db: Session = Depends(get_db)
):
    cat = db.get(SkillCategory, category_id)
    if cat is None:
        raise HTTPException(status_code=404, detail="Category not found")
    changes = data.model_dump(exclude_unset=True)
    name = changes.get("category_name")
    if name and name != cat.category_name and db.scalar(
        select(SkillCategory.category_id).where(SkillCategory.category_name == name)
    ):
        raise HTTPException(status_code=409, detail="Category name already used")
    for field, value in changes.items():
        if field == "category_name" and not value:
            continue
        setattr(cat, field, value)
    db.commit()
    db.refresh(cat)
    return cat


@router.delete("/categories/{category_id}", status_code=204)
def delete_category(category_id: int, _: Admin = Depends(get_current_admin), db: Session = Depends(get_db)):
    """A category that still has skills cannot be deleted (the database protects it too)."""
    cat = db.get(SkillCategory, category_id)
    if cat is None:
        raise HTTPException(status_code=404, detail="Category not found")
    used = db.scalar(select(TeachingSkill.teaching_skill_id).where(TeachingSkill.category_id == category_id).limit(1))
    used = used or db.scalar(select(LearningSkill.learning_skill_id).where(LearningSkill.category_id == category_id).limit(1))
    if used:
        raise HTTPException(status_code=409, detail="This category is still used by skills and cannot be deleted")
    db.delete(cat)
    db.commit()


# ---------- Reports ----------
def _content_owner(db: Session, report: Report) -> int | None:
    """Who owns the reported content (used when the admin suspends/blocks 'the user' of a content report)."""
    if report.reported_user_id is not None:
        return report.reported_user_id
    model_attr = {
        "TeachingSkill": (TeachingSkill, TeachingSkill.user_id),
        "LearningSkill": (LearningSkill, LearningSkill.user_id),
        "Message": (Message, Message.sender_id),
        "Review": (Review, Review.learner_id),
    }[report.content_type]
    model, owner_col = model_attr
    pk_col = list(model.__table__.primary_key.columns)[0]
    return db.scalar(select(owner_col).where(pk_col == report.content_id))


def _apply_action(db: Session, report: Report, action: str) -> None:
    if action in ("User Suspended", "User Blocked"):
        owner_id = _content_owner(db, report)
        user = db.get(User, owner_id) if owner_id else None
        if user is None:
            raise HTTPException(status_code=400, detail="The user for this report no longer exists")
        user.account_status = "Suspended" if action == "User Suspended" else "Blocked"
    elif action == "Content Removed":
        if report.content_type is None:
            raise HTTPException(status_code=400, detail="'Content Removed' only applies to reports about content")
        if report.content_type == "Message":
            raise HTTPException(status_code=400, detail="Messages cannot be deleted (project rule). Choose another action.")
        if report.content_type == "Review":
            # The database allows deleting a review only inside a transaction flagged as an admin removal.
            db.execute(text("SET LOCAL app.admin_removal = 'on'"))
            db.execute(delete(Review).where(Review.review_id == report.content_id))
        else:  # skills are hidden, never physically deleted
            model = TeachingSkill if report.content_type == "TeachingSkill" else LearningSkill
            pk_col = list(model.__table__.primary_key.columns)[0]
            skill = db.scalar(select(model).where(pk_col == report.content_id))
            if skill is not None:
                skill.is_active = False


@router.get("/reports", response_model=list[ReportOut])
def list_reports(
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: Admin = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    stmt = select(Report)
    if status:
        stmt = stmt.where(Report.status == status)
    return db.scalars(stmt.order_by(Report.report_id.desc()).limit(limit).offset(offset)).all()


@router.get("/reports/{report_id}", response_model=ReportOut)
def get_report(report_id: int, _: Admin = Depends(get_current_admin), db: Session = Depends(get_db)):
    report = db.get(Report, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return report


@router.patch("/reports/{report_id}", response_model=ReportOut)
def update_report(
    report_id: int, data: ReportUpdateIn, admin: Admin = Depends(get_current_admin), db: Session = Depends(get_db)
):
    """Admin reviews a user/content report. Resolving with 'User Blocked' locks the reported
    user account; 'User Suspended' temporarily disables it. The reporter cannot lock users directly.
    """
    report = db.get(Report, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    if report.status in ("Resolved", "Dismissed"):
        raise HTTPException(status_code=409, detail=f"Report is already {report.status}")
    if data.status == "Resolved" and data.admin_action:
        _apply_action(db, report, data.admin_action)
    report.status = data.status
    report.reviewed_by_admin_id = admin.admin_id
    report.admin_action = data.admin_action
    report.reviewed_at = func.now()
    db.commit()
    db.refresh(report)
    return report
