"""SQLAlchemy descriptions of the team's PostgreSQL tables (docs/skillloop_database.sql).

IMPORTANT: this file does NOT create or change tables. The database teammate owns the schema.
These classes only tell Python which tables and columns exist, so it can read and write them.
Names must match the real database exactly.

PostgreSQL folds unquoted names to lower case, so "ConnectionRequest" in the SQL file is really the
table `connectionrequest`. Only "User" was created with quotes, so it keeps its capital U.
"""
from datetime import date, datetime, time

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base

NOW = func.now()


# ---------- Accounts ----------
class User(Base):
    __tablename__ = "User"

    user_id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255))
    profile_photo: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(String(100))
    country: Mapped[str | None] = mapped_column(String(100))
    bio: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    account_status: Mapped[str] = mapped_column(String(20), server_default=text("'Active'"))  # Active/Suspended/Blocked/Deleted
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Admin(Base):
    """Created internally (python -m app.scripts.create_admin), never by registration."""

    __tablename__ = "admin"

    admin_id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


# ---------- Skills, languages, availability ----------
class SkillCategory(Base):
    __tablename__ = "skillcategory"

    category_id: Mapped[int] = mapped_column(primary_key=True)
    category_name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    created_by_admin_id: Mapped[int] = mapped_column(ForeignKey("admin.admin_id"))


class TeachingSkill(Base):
    __tablename__ = "teachingskill"

    teaching_skill_id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("User.user_id"))
    category_id: Mapped[int] = mapped_column(ForeignKey("skillcategory.category_id"))
    skill_name: Mapped[str] = mapped_column(String(100))
    skill_level: Mapped[str] = mapped_column(String(20))  # Beginner / Intermediate / Advanced
    short_description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))

    user: Mapped[User] = relationship()
    category: Mapped[SkillCategory] = relationship()

    @property
    def category_name(self) -> str:
        return self.category.category_name


class LearningSkill(Base):
    __tablename__ = "learningskill"

    learning_skill_id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("User.user_id"))
    category_id: Mapped[int] = mapped_column(ForeignKey("skillcategory.category_id"))
    skill_name: Mapped[str] = mapped_column(String(100))
    current_level: Mapped[str] = mapped_column(String(20))  # Beginner / Intermediate / Advanced
    learning_goal: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))

    category: Mapped[SkillCategory] = relationship()

    @property
    def category_name(self) -> str:
        return self.category.category_name


class Language(Base):
    __tablename__ = "language"

    language_id: Mapped[int] = mapped_column(primary_key=True)
    language_name: Mapped[str] = mapped_column(String(80))


class UserLanguage(Base):
    __tablename__ = "userlanguage"

    user_id: Mapped[int] = mapped_column(ForeignKey("User.user_id"), primary_key=True)
    language_id: Mapped[int] = mapped_column(ForeignKey("language.language_id"), primary_key=True)


class Availability(Base):
    __tablename__ = "availability"

    availability_id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("User.user_id"))
    day_of_week: Mapped[str] = mapped_column(String(15))  # 'Monday' ... 'Sunday'
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)


# ---------- Requests, connections, and one-to-one chat ----------
class Connection(Base):
    """Created by the database function fn_accept_connection_request. user_one_id < user_two_id always."""

    __tablename__ = "connection"

    connection_id: Mapped[int] = mapped_column(primary_key=True)
    user_one_id: Mapped[int] = mapped_column(ForeignKey("User.user_id"))
    user_two_id: Mapped[int] = mapped_column(ForeignKey("User.user_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    status: Mapped[str] = mapped_column(String(20), server_default=text("'Active'"))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    removed_by_id: Mapped[int | None] = mapped_column(ForeignKey("User.user_id"), nullable=True)


class NetworkConnectionRequest(Base):
    __tablename__ = "networkconnectionrequest"

    request_id: Mapped[int] = mapped_column(primary_key=True)
    sender_id: Mapped[int] = mapped_column(ForeignKey("User.user_id"))
    receiver_id: Mapped[int] = mapped_column(ForeignKey("User.user_id"))
    optional_message: Mapped[str | None] = mapped_column(Text)
    request_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    status: Mapped[str] = mapped_column(String(20), server_default=text("'Pending'"))
    connection_id: Mapped[int | None] = mapped_column(ForeignKey("connection.connection_id"), nullable=True)

    sender: Mapped[User] = relationship(foreign_keys=[sender_id])
    receiver: Mapped[User] = relationship(foreign_keys=[receiver_id])


class ConnectionRequest(Base):
    __tablename__ = "connectionrequest"

    request_id: Mapped[int] = mapped_column(primary_key=True)
    sender_id: Mapped[int] = mapped_column(ForeignKey("User.user_id"))
    receiver_id: Mapped[int] = mapped_column(ForeignKey("User.user_id"))
    teaching_skill_id: Mapped[int] = mapped_column(ForeignKey("teachingskill.teaching_skill_id"))
    optional_message: Mapped[str | None] = mapped_column(Text)
    request_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    status: Mapped[str] = mapped_column(String(20), server_default=text("'Pending'"))  # Pending/Accepted/Rejected/Cancelled
    connection_id: Mapped[int | None] = mapped_column(ForeignKey("connection.connection_id"))

    sender: Mapped[User] = relationship(foreign_keys=[sender_id])
    receiver: Mapped[User] = relationship(foreign_keys=[receiver_id])
    teaching_skill: Mapped[TeachingSkill] = relationship()

    @property
    def skill_name(self) -> str:
        return self.teaching_skill.skill_name


class Conversation(Base):
    __tablename__ = "conversation"

    conversation_id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connection.connection_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


class Message(Base):
    __tablename__ = "message"

    message_id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversation.conversation_id"))
    sender_id: Mapped[int] = mapped_column(ForeignKey("User.user_id"))
    message_text: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    is_read: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


# ---------- Sessions and reviews ----------
class Session(Base):
    """An online session. The meeting happens outside SkillLoop; only the link is stored."""

    __tablename__ = "session"

    session_id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connection.connection_id"))
    teaching_skill_id: Mapped[int] = mapped_column(ForeignKey("teachingskill.teaching_skill_id"))
    teacher_id: Mapped[int] = mapped_column(ForeignKey("User.user_id"))
    learner_id: Mapped[int] = mapped_column(ForeignKey("User.user_id"))
    topic: Mapped[str] = mapped_column(String(200))
    session_date: Mapped[date] = mapped_column(Date)
    start_time: Mapped[time] = mapped_column(Time)
    duration_minutes: Mapped[int] = mapped_column(Integer)
    meeting_link: Mapped[str] = mapped_column(Text)
    optional_note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), server_default=text("'Pending'"))  # Pending/Scheduled/Rejected/Cancelled/Completed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)

    teacher: Mapped[User] = relationship(foreign_keys=[teacher_id])
    learner: Mapped[User] = relationship(foreign_keys=[learner_id])
    teaching_skill: Mapped[TeachingSkill] = relationship()

    @property
    def teacher_name(self) -> str:
        return self.teacher.full_name

    @property
    def learner_name(self) -> str:
        return self.learner.full_name

    @property
    def skill_name(self) -> str:
        return self.teaching_skill.skill_name


class Review(Base):
    __tablename__ = "review"

    review_id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("session.session_id"))
    learner_id: Mapped[int] = mapped_column(ForeignKey("User.user_id"))
    teacher_id: Mapped[int] = mapped_column(ForeignKey("User.user_id"))
    rating: Mapped[int] = mapped_column(Integer)
    written_review: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)

    learner: Mapped[User] = relationship(foreign_keys=[learner_id])

    @property
    def learner_name(self) -> str:
        return self.learner.full_name


# ---------- Notifications and reports ----------
class Notification(Base):
    __tablename__ = "notification"

    notification_id: Mapped[int] = mapped_column(primary_key=True)
    recipient_id: Mapped[int] = mapped_column(ForeignKey("User.user_id"))
    notification_type: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(150))
    message: Mapped[str] = mapped_column(Text)
    related_entity_type: Mapped[str | None] = mapped_column(String(30))
    related_entity_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # NULL = unread

    @property
    def is_read(self) -> bool:
        return self.read_at is not None


class Report(Base):
    __tablename__ = "report"

    report_id: Mapped[int] = mapped_column(primary_key=True)
    reporter_user_id: Mapped[int] = mapped_column(ForeignKey("User.user_id"))
    reported_user_id: Mapped[int | None] = mapped_column(ForeignKey("User.user_id"))
    content_type: Mapped[str | None] = mapped_column(String(30))  # TeachingSkill/LearningSkill/Message/Review
    content_id: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(150))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), server_default=text("'Pending'"))  # Pending/Under Review/Resolved/Dismissed
    reviewed_by_admin_id: Mapped[int | None] = mapped_column(ForeignKey("admin.admin_id"))
    admin_action: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserBlock(Base):
    """A user's block list. Blocking is reversible and does not delete history."""

    __tablename__ = "userblock"

    block_id: Mapped[int] = mapped_column(primary_key=True)
    blocker_id: Mapped[int] = mapped_column(ForeignKey("User.user_id"))
    blocked_id: Mapped[int] = mapped_column(ForeignKey("User.user_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
