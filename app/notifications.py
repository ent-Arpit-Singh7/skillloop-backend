"""In-app notifications are created by backend events, never by the client.

The database accepts exactly these 9 types (see the CHECK on Notification.notification_type):
request_received, request_accepted, request_rejected, session_requested, session_accepted,
session_rejected, session_cancelled, new_message, new_review_received.
"""
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.models import Notification


def notify(
    db: Session,
    recipient_id: int,
    notification_type: str,
    title: str,
    message: str,
    related_entity_type: str | None = None,
    related_entity_id: int | None = None,
) -> None:
    """Adds a notification to the current transaction (the caller commits).

    related_entity_type is one of ConnectionRequest / Session / Message / Review.
    Flush the related row first: the database checks that it exists.
    """
    db.add(
        Notification(
            recipient_id=recipient_id,
            notification_type=notification_type,
            title=title,
            message=message,
            related_entity_type=related_entity_type,
            related_entity_id=related_entity_id,
        )
    )


def mark_notification_entity_read(
    db: Session,
    related_entity_type: str,
    related_entity_id: int,
    recipient_id: int,
) -> int:
    """Mark matching unread notifications read, preserving notification history."""
    result = db.execute(
        update(Notification)
        .where(
            Notification.recipient_id == recipient_id,
            Notification.related_entity_type == related_entity_type,
            Notification.related_entity_id == related_entity_id,
            Notification.read_at.is_(None),
        )
        .values(read_at=func.now())
    )
    return result.rowcount or 0
