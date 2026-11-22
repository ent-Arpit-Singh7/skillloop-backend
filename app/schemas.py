"""Pydantic schemas: what React may send (...In) and what the API returns (...Out).

Field names match the database columns so React sees the same names the database teammate uses.
"""
from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

Level = Literal["Beginner", "Intermediate", "Advanced"]
Day = Literal["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
AccountStatus = Literal["Active", "Suspended", "Blocked"]
ReportStatus = Literal["Pending", "Under Review", "Resolved", "Dismissed"]
AdminAction = Literal[
    "No Action Required", "Warning Issued", "Content Removed", "User Suspended", "User Blocked", "Other Action"
]
ContentType = Literal["TeachingSkill", "LearningSkill", "Message", "Review"]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- Auth ----------
class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=100)
    city: str | None = Field(default=None, max_length=100)
    country: str | None = Field(default=None, max_length=100)

    @field_validator("email")
    @classmethod
    def lower_email(cls, v: str) -> str:
        return v.lower()

    @field_validator("full_name")
    @classmethod
    def name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("full_name cannot be blank")
        return v


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def lower_email(cls, v: str) -> str:
        return v.lower()


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=20, max_length=4096)


class MessageOut(BaseModel):
    detail: str


class UnreadCountOut(BaseModel):
    unread_count: int


# ---------- Users / profile ----------
class UserOut(ORM):
    """The logged-in user's own profile."""

    user_id: int
    full_name: str
    email: EmailStr
    profile_photo: str | None
    city: str | None
    country: str | None
    bio: str | None
    account_status: str
    created_at: datetime
    deleted_at: datetime | None = None


class PublicUserOut(ORM):
    """What other users may see (no email)."""

    user_id: int
    full_name: str
    profile_photo: str | None
    city: str | None
    country: str | None
    bio: str | None


class BlockOut(ORM):
    block_id: int
    blocked_id: int
    blocked_user_name: str
    created_at: datetime


class UserUpdateIn(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=100)
    profile_photo: str | None = Field(default=None, max_length=1000, description="URL or path of the photo")
    city: str | None = Field(default=None, max_length=100)
    country: str | None = Field(default=None, max_length=100)
    bio: str | None = Field(default=None, max_length=2000)


# ---------- Categories / languages ----------
class CategoryOut(ORM):
    category_id: int
    category_name: str
    description: str | None


class CategoryIn(BaseModel):
    category_name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)

    @field_validator("category_name")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("category_name cannot be blank")
        return v.strip()


class CategoryUpdateIn(BaseModel):
    category_name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)


class LanguageOut(ORM):
    language_id: int
    language_name: str


class UserLanguageIn(BaseModel):
    language_id: int


# ---------- Skills ----------
class TeachingSkillIn(BaseModel):
    category_id: int
    skill_name: str = Field(min_length=1, max_length=100)
    skill_level: Level
    short_description: str | None = Field(default=None, max_length=2000)

    @field_validator("skill_name")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("skill_name cannot be blank")
        return v.strip()


class TeachingSkillUpdateIn(BaseModel):
    category_id: int | None = None
    skill_name: str | None = Field(default=None, min_length=1, max_length=100)
    skill_level: Level | None = None
    short_description: str | None = Field(default=None, max_length=2000)


class TeachingSkillOut(ORM):
    teaching_skill_id: int
    user_id: int
    category_id: int
    category_name: str
    skill_name: str
    skill_level: str
    short_description: str | None
    is_active: bool


class LearningSkillIn(BaseModel):
    category_id: int
    skill_name: str = Field(min_length=1, max_length=100)
    current_level: Level
    learning_goal: str | None = Field(default=None, max_length=2000)

    @field_validator("skill_name")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("skill_name cannot be blank")
        return v.strip()


class LearningSkillUpdateIn(BaseModel):
    category_id: int | None = None
    skill_name: str | None = Field(default=None, min_length=1, max_length=100)
    current_level: Level | None = None
    learning_goal: str | None = Field(default=None, max_length=2000)


class LearningSkillOut(ORM):
    learning_skill_id: int
    category_id: int
    category_name: str
    skill_name: str
    current_level: str
    learning_goal: str | None
    is_active: bool


# ---------- Availability ----------
class AvailabilityIn(BaseModel):
    day_of_week: Day
    start_time: time
    end_time: time

    @model_validator(mode="after")
    def check_order(self):
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class AvailabilityUpdateIn(BaseModel):
    day_of_week: Day | None = None
    start_time: time | None = None
    end_time: time | None = None


class AvailabilityOut(ORM):
    availability_id: int
    day_of_week: str
    start_time: time
    end_time: time


# ---------- Search / public profile ----------
class PersonCardOut(BaseModel):
    """One search result and also the public profile page."""

    user: PublicUserOut
    skills: list[TeachingSkillOut]
    languages: list[LanguageOut]
    availability: list[AvailabilityOut]
    average_rating: float | None
    review_count: int
    connection_count: int


class PublicProfileOut(PersonCardOut):
    pass


# ---------- Requests / connections (no active chat API) ----------
class RequestIn(BaseModel):
    teaching_skill_id: int
    optional_message: str | None = Field(default=None, max_length=1000)


class RequestOut(ORM):
    request_id: int
    sender_id: int
    receiver_id: int
    sender: PublicUserOut
    receiver: PublicUserOut
    teaching_skill_id: int
    skill_name: str
    optional_message: str | None
    request_date: datetime
    status: str
    connection_id: int | None


class ConnectionOut(BaseModel):
    """A mutual user-to-user connection."""

    connection_id: int
    created_at: datetime
    status: str
    other_user: PublicUserOut


class NetworkConnectionRequestIn(BaseModel):
    receiver_id: int = Field(gt=0)
    optional_message: str | None = Field(default=None, max_length=1000)


class NetworkConnectionRequestOut(ORM):
    request_id: int
    sender_id: int
    receiver_id: int
    sender: PublicUserOut
    receiver: PublicUserOut
    optional_message: str | None
    request_date: datetime
    status: str
    connection_id: int | None


# ---------- One-to-one chat ----------
class ConversationCreateIn(BaseModel):
    connection_id: int = Field(gt=0)


class ChatMessageIn(BaseModel):
    message_text: str = Field(min_length=1, max_length=5000)

    @field_validator("message_text")
    @classmethod
    def message_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message_text cannot be blank")
        return value


class ChatMessageOut(ORM):
    message_id: int
    conversation_id: int
    sender_id: int
    message_text: str
    sent_at: datetime
    is_read: bool


class ConversationOut(BaseModel):
    conversation_id: int
    connection_id: int
    created_at: datetime
    other_user: PublicUserOut
    latest_message: ChatMessageOut | None
    unread_count: int


class ConversationReadOut(BaseModel):
    conversation_id: int
    marked_count: int


# ---------- Sessions / reviews ----------
class SessionProposeIn(BaseModel):
    connection_id: int
    teaching_skill_id: int
    topic: str = Field(min_length=1, max_length=200)
    session_date: date
    start_time: time
    duration_minutes: int = Field(gt=0, le=480)
    meeting_link: str = Field(min_length=1, max_length=2000)
    optional_note: str | None = Field(default=None, max_length=1000)

    @field_validator("topic")
    @classmethod
    def topic_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("topic cannot be blank")
        return v.strip()

    @field_validator("meeting_link")
    @classmethod
    def link_is_url(cls, v: str) -> str:
        v = v.strip()
        if not (v.startswith("https://") or v.startswith("http://")):
            raise ValueError("meeting_link must start with http:// or https://")
        return v


class SessionOut(ORM):
    session_id: int
    connection_id: int
    teaching_skill_id: int
    skill_name: str
    teacher_id: int
    teacher_name: str
    learner_id: int
    learner_name: str
    topic: str
    session_date: date
    start_time: time
    duration_minutes: int
    meeting_link: str | None  # hidden from the learner until the session is Scheduled
    optional_note: str | None
    status: str
    created_at: datetime


class ReviewIn(BaseModel):
    rating: int = Field(ge=1, le=5)
    written_review: str | None = Field(default=None, max_length=2000)


class ReviewOut(ORM):
    review_id: int
    session_id: int
    learner_id: int
    learner_name: str
    teacher_id: int
    rating: int
    written_review: str | None
    created_at: datetime


# ---------- Notifications / reports ----------
class NotificationOut(ORM):
    notification_id: int
    notification_type: str
    title: str
    message: str
    related_entity_type: str | None
    related_entity_id: int | None
    is_read: bool
    created_at: datetime
    read_at: datetime | None


class ReportIn(BaseModel):
    """Report ONE target: either a user (reported_user_id) or one piece of content (content_type + content_id)."""

    reported_user_id: int | None = None
    content_type: ContentType | None = None
    content_id: int | None = None
    reason: str = Field(min_length=1, max_length=150)
    description: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def exactly_one_target(self):
        user_target = self.reported_user_id is not None
        content_target = self.content_type is not None or self.content_id is not None
        if user_target == content_target:
            raise ValueError("Report either a user (reported_user_id) or one content item (content_type + content_id)")
        if content_target and (self.content_type is None or self.content_id is None):
            raise ValueError("content_type and content_id must be given together")
        return self


class ReportOut(ORM):
    report_id: int
    reporter_user_id: int
    reported_user_id: int | None
    content_type: str | None
    content_id: int | None
    reason: str
    description: str | None
    status: str
    reviewed_by_admin_id: int | None
    admin_action: str | None
    created_at: datetime
    reviewed_at: datetime | None


class ReportUpdateIn(BaseModel):
    status: Literal["Under Review", "Resolved", "Dismissed"]
    admin_action: AdminAction | None = None

    @model_validator(mode="after")
    def closed_needs_action(self):
        if self.status in ("Resolved", "Dismissed") and self.admin_action is None:
            raise ValueError("A Resolved or Dismissed report needs an admin_action")
        return self


# ---------- Admin ----------
class AdminUserOut(ORM):
    user_id: int
    full_name: str
    email: EmailStr
    city: str | None
    country: str | None
    account_status: str
    created_at: datetime
    deleted_at: datetime | None = None


class AdminUserUpdateIn(BaseModel):
    account_status: AccountStatus


class AdminOverviewOut(BaseModel):
    total_users: int
    active_users: int
    suspended_users: int
    blocked_users: int
    categories: int
    pending_reports: int
    under_review_reports: int


class BlockIn(BaseModel):
    blocked_user_id: int = Field(gt=0)
