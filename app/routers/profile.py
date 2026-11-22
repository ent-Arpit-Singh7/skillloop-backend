"""Profile, languages, teaching skills, learning skills, availability (always the logged-in user's own)."""
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from app.core.config import settings
from app.core import profile_storage
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.deps import get_current_user
from app.models import (
    Availability,
    Language,
    LearningSkill,
    SkillCategory,
    TeachingSkill,
    User,
    UserLanguage,
)
from app.schemas import (
    AvailabilityIn,
    AvailabilityOut,
    AvailabilityUpdateIn,
    CategoryOut,
    LanguageOut,
    LearningSkillIn,
    LearningSkillOut,
    MessageOut,
    LearningSkillUpdateIn,
    TeachingSkillIn,
    TeachingSkillOut,
    TeachingSkillUpdateIn,
    UserLanguageIn,
    UserOut,
    UserUpdateIn,
)

router = APIRouter(prefix="/api", tags=["profile"])


def _check_category(db: Session, category_id: int) -> None:
    if db.get(SkillCategory, category_id) is None:
        raise HTTPException(status_code=400, detail="Unknown category (choose one from /api/categories)")


def _own_active_skill(db: Session, model, pk_name: str, item_id: int, user: User):
    """Fetch a skill only if it is yours and still active (else 404, so ids of others aren't revealed)."""
    item = db.get(model, item_id)
    if item is None or item.user_id != user.user_id or not item.is_active:
        raise HTTPException(status_code=404, detail="Not found")
    return item


def _own_availability(db: Session, item_id: int, user: User) -> Availability:
    item = db.get(Availability, item_id)
    if item is None or item.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="Not found")
    return item


# ---------- Profile ----------
@router.get("/me", response_model=UserOut)
def get_me(user: User = Depends(get_current_user)):
    return user


@router.delete("/me", response_model=MessageOut)
def delete_my_account(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Soft-delete the account; preserve linked records and immediately revoke API access."""
    user.account_status = "Deleted"
    user.deleted_at = datetime.now(timezone.utc)
    db.commit()
    return MessageOut(detail="Account deleted. Your data is retained for linked records, but access is disabled.")


@router.patch("/me", response_model=UserOut)
def update_me(data: UserUpdateIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    for field, value in data.model_dump(exclude_unset=True).items():
        if field == "full_name" and not value:
            continue
        setattr(user, field, value)
    db.commit()
    db.refresh(user)
    return user


@router.post("/me/avatar", response_model=UserOut)
async def upload_avatar(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Upload a JPEG, PNG or WebP image as the raw request body (not multipart).

    Send Content-Type: image/jpeg (or image/png / image/webp). Max size is
    controlled by MAX_PROFILE_IMAGE_BYTES. SVG is deliberately not accepted.
    """
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    allowed = {
        "image/jpeg": ("jpg", lambda b: b.startswith(b"\xff\xd8\xff")),
        "image/png": ("png", lambda b: b.startswith(b"\x89PNG\r\n\x1a\n")),
        "image/webp": ("webp", lambda b: len(b) >= 12 and b[:4] == b"RIFF" and b[8:12] == b"WEBP"),
    }
    if content_type not in allowed:
        raise HTTPException(status_code=415, detail="Upload a JPEG, PNG or WebP image")
    max_bytes = settings.max_profile_image_bytes
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > max_bytes:
        raise HTTPException(status_code=413, detail="Profile image is too large")
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > max_bytes:
            raise HTTPException(status_code=413, detail="Profile image is too large")
    extension, validator = allowed[content_type]
    if not validator(chunks):
        raise HTTPException(status_code=415, detail="Image content does not match its Content-Type")
    filename = f"{user.user_id}_{uuid4().hex}.{extension}"
    old_photo = user.profile_photo
    try:
        public_url = await profile_storage.upload_profile_image(filename, bytes(chunks), content_type)
    except profile_storage.ProfileStorageError:
        raise HTTPException(status_code=502, detail="Profile image storage is unavailable") from None

    user.profile_photo = public_url
    try:
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        try:
            await profile_storage.delete_profile_image(filename)
        except profile_storage.ProfileStorageError:
            pass
        raise HTTPException(status_code=500, detail="Could not save profile image") from None

    db.refresh(user)
    old_object = profile_storage.profile_object_from_public_url(old_photo, user.user_id)
    if old_object and old_object != filename:
        try:
            await profile_storage.delete_profile_image(old_object)
        except profile_storage.ProfileStorageError:
            # Database state is already committed; keep the new image working.
            pass
    return user


# ---------- Reference lists (dropdowns) ----------
@router.get("/categories", response_model=list[CategoryOut])
def list_categories(_: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Categories are managed by Admin only. Users just pick from this list."""
    return db.scalars(select(SkillCategory).order_by(SkillCategory.category_name)).all()


@router.get("/languages", response_model=list[LanguageOut])
def list_languages(_: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.scalars(select(Language).order_by(Language.language_name)).all()


# ---------- My languages ----------
@router.get("/me/languages", response_model=list[LanguageOut])
def my_languages(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.scalars(
        select(Language)
        .join(UserLanguage, UserLanguage.language_id == Language.language_id)
        .where(UserLanguage.user_id == user.user_id)
        .order_by(Language.language_name)
    ).all()


@router.post("/me/languages", response_model=LanguageOut, status_code=201)
def add_language(data: UserLanguageIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    language = db.get(Language, data.language_id)
    if language is None:
        raise HTTPException(status_code=400, detail="Unknown language")
    if db.get(UserLanguage, (user.user_id, data.language_id)):
        raise HTTPException(status_code=409, detail="Language already added")
    db.add(UserLanguage(user_id=user.user_id, language_id=data.language_id))
    db.commit()
    return language


@router.delete("/me/languages/{language_id}", status_code=204)
def remove_language(language_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    link = db.get(UserLanguage, (user.user_id, language_id))
    if link is None:
        raise HTTPException(status_code=404, detail="Not found")
    db.delete(link)
    db.commit()


# ---------- Skills I can teach (removal = is_active False) ----------
@router.get("/me/teaching-skills", response_model=list[TeachingSkillOut])
def my_teaching_skills(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.scalars(
        select(TeachingSkill)
        .where(TeachingSkill.user_id == user.user_id, TeachingSkill.is_active.is_(True))
        .order_by(TeachingSkill.teaching_skill_id)
    ).all()


@router.post("/me/teaching-skills", response_model=TeachingSkillOut, status_code=201)
def add_teaching_skill(data: TeachingSkillIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _check_category(db, data.category_id)
    item = TeachingSkill(user_id=user.user_id, **data.model_dump())
    db.add(item)
    db.commit()  # a duplicate active skill name is rejected by the database -> 409
    db.refresh(item)
    return item


@router.patch("/me/teaching-skills/{item_id}", response_model=TeachingSkillOut)
def update_teaching_skill(
    item_id: int, data: TeachingSkillUpdateIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    item = _own_active_skill(db, TeachingSkill, "teaching_skill_id", item_id, user)
    changes = data.model_dump(exclude_unset=True)
    if changes.get("category_id") is not None:
        _check_category(db, changes["category_id"])
    for field, value in changes.items():
        if value is None and field != "short_description":
            continue
        setattr(item, field, value)
    db.commit()
    db.refresh(item)
    return item


@router.delete("/me/teaching-skills/{item_id}", status_code=204)
def delete_teaching_skill(item_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Skills are never really deleted (the database blocks it): the skill is hidden with is_active = FALSE."""
    item = _own_active_skill(db, TeachingSkill, "teaching_skill_id", item_id, user)
    item.is_active = False
    db.commit()


# ---------- Skills I want to learn ----------
@router.get("/me/learning-skills", response_model=list[LearningSkillOut])
def my_learning_skills(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.scalars(
        select(LearningSkill)
        .where(LearningSkill.user_id == user.user_id, LearningSkill.is_active.is_(True))
        .order_by(LearningSkill.learning_skill_id)
    ).all()


@router.post("/me/learning-skills", response_model=LearningSkillOut, status_code=201)
def add_learning_skill(data: LearningSkillIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _check_category(db, data.category_id)
    item = LearningSkill(user_id=user.user_id, **data.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.patch("/me/learning-skills/{item_id}", response_model=LearningSkillOut)
def update_learning_skill(
    item_id: int, data: LearningSkillUpdateIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    item = _own_active_skill(db, LearningSkill, "learning_skill_id", item_id, user)
    changes = data.model_dump(exclude_unset=True)
    if changes.get("category_id") is not None:
        _check_category(db, changes["category_id"])
    for field, value in changes.items():
        if value is None and field != "learning_goal":
            continue
        setattr(item, field, value)
    db.commit()
    db.refresh(item)
    return item


@router.delete("/me/learning-skills/{item_id}", status_code=204)
def delete_learning_skill(item_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = _own_active_skill(db, LearningSkill, "learning_skill_id", item_id, user)
    item.is_active = False
    db.commit()


# ---------- Availability (weekly slots; overlaps on the same day are refused by the database) ----------
@router.get("/me/availability", response_model=list[AvailabilityOut])
def my_availability(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.scalars(select(Availability).where(Availability.user_id == user.user_id)).all()


@router.post("/me/availability", response_model=AvailabilityOut, status_code=201)
def add_availability(data: AvailabilityIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = Availability(user_id=user.user_id, **data.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.patch("/me/availability/{item_id}", response_model=AvailabilityOut)
def update_availability(
    item_id: int, data: AvailabilityUpdateIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    item = _own_availability(db, item_id, user)
    for field, value in data.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(item, field, value)
    if item.end_time <= item.start_time:
        raise HTTPException(status_code=422, detail="end_time must be after start_time")
    db.commit()
    db.refresh(item)
    return item


@router.delete("/me/availability/{item_id}", status_code=204)
def delete_availability(item_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    db.delete(_own_availability(db, item_id, user))
    db.commit()
