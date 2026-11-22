"""Authenticated one-to-one text conversations for accepted connections."""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func, or_, select, update
from sqlalchemy.orm import Session as DbSession, aliased

from app.db.session import get_db
from app.deps import get_current_user
from app.models import Connection, Conversation, Message, User, UserBlock
from app.notifications import notify
from app.schemas import (
    ChatMessageIn,
    ChatMessageOut,
    ConversationCreateIn,
    ConversationOut,
    ConversationReadOut,
    PublicUserOut,
)

router = APIRouter(prefix="/api", tags=["chat"])


def _is_blocked(db: DbSession, first_user_id: int, second_user_id: int) -> bool:
    blocked = db.scalar(
        select(UserBlock.block_id).where(
            or_(
                (UserBlock.blocker_id == first_user_id) & (UserBlock.blocked_id == second_user_id),
                (UserBlock.blocker_id == second_user_id) & (UserBlock.blocked_id == first_user_id),
            )
        )
    )
    return blocked is not None


def _conversation_context(
    db: DbSession,
    conversation_id: int,
    me: User,
) -> tuple[Conversation, Connection, User]:
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    connection = db.get(Connection, conversation.connection_id)
    if connection is None or me.user_id not in (connection.user_one_id, connection.user_two_id):
        raise HTTPException(status_code=404, detail="Conversation not found")

    other_user_id = (
        connection.user_two_id if connection.user_one_id == me.user_id else connection.user_one_id
    )
    other_user = db.get(User, other_user_id)
    if other_user is None or other_user.account_status != "Active":
        raise HTTPException(status_code=404, detail="Conversation not found")
    if _is_blocked(db, me.user_id, other_user_id):
        raise HTTPException(status_code=403, detail="This conversation is unavailable because one user has blocked the other")
    return conversation, connection, other_user


def _conversation_out(
    db: DbSession,
    conversation: Conversation,
    connection: Connection,
    other_user: User,
) -> ConversationOut:
    latest_message = db.scalar(
        select(Message)
        .where(Message.conversation_id == conversation.conversation_id)
        .order_by(Message.sent_at.desc(), Message.message_id.desc())
        .limit(1)
    )
    unread_count = db.scalar(
        select(func.count(Message.message_id)).where(
            Message.conversation_id == conversation.conversation_id,
            Message.sender_id == other_user.user_id,
            Message.is_read.is_(False),
        )
    ) or 0
    return ConversationOut(
        conversation_id=conversation.conversation_id,
        connection_id=connection.connection_id,
        created_at=conversation.created_at,
        other_user=PublicUserOut.model_validate(other_user),
        latest_message=ChatMessageOut.model_validate(latest_message) if latest_message else None,
        unread_count=unread_count,
    )


def _authorized_connection(
    db: DbSession,
    connection_id: int,
    me: User,
) -> tuple[Connection, User]:
    connection = db.scalar(
        select(Connection)
        .where(Connection.connection_id == connection_id)
        .with_for_update()
    )
    if (
        connection is None
        or connection.status != "Active"
        or me.user_id not in (connection.user_one_id, connection.user_two_id)
    ):
        raise HTTPException(status_code=404, detail="Connection not found")

    other_user_id = (
        connection.user_two_id if connection.user_one_id == me.user_id else connection.user_one_id
    )
    other_user = db.get(User, other_user_id)
    if other_user is None or other_user.account_status != "Active":
        raise HTTPException(status_code=404, detail="Connection not found")
    if _is_blocked(db, me.user_id, other_user_id):
        raise HTTPException(status_code=403, detail="This connection is unavailable because one user has blocked the other")

    return connection, other_user


@router.get("/conversations", response_model=list[ConversationOut])
def list_conversations(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    me: User = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    other_user = aliased(User)
    other_user_id = case(
        (Connection.user_one_id == me.user_id, Connection.user_two_id),
        else_=Connection.user_one_id,
    )
    blocked = (
        select(UserBlock.block_id)
        .where(
            or_(
                (UserBlock.blocker_id == me.user_id) & (UserBlock.blocked_id == other_user_id),
                (UserBlock.blocker_id == other_user_id) & (UserBlock.blocked_id == me.user_id),
            )
        )
        .exists()
    )
    latest_message_date = (
        select(Message.sent_at)
        .where(Message.conversation_id == Conversation.conversation_id)
        .order_by(Message.sent_at.desc(), Message.message_id.desc())
        .limit(1)
        .correlate(Conversation)
        .scalar_subquery()
    )
    conversations = db.scalars(
        select(Conversation)
        .join(Connection, Conversation.connection_id == Connection.connection_id)
        .join(other_user, other_user.user_id == other_user_id)
        .where(
            or_(Connection.user_one_id == me.user_id, Connection.user_two_id == me.user_id),
            other_user.account_status == "Active",
            ~blocked,
        )
        .order_by(
            func.coalesce(latest_message_date, Conversation.created_at).desc(),
            Conversation.conversation_id.desc(),
        )
        .limit(limit)
        .offset(offset)
    ).all()

    output = []
    for conversation in conversations:
        _, connection, participant = _conversation_context(
            db, conversation.conversation_id, me
        )
        output.append(_conversation_out(db, conversation, connection, participant))
    return output


@router.post(
    "/conversations",
    response_model=ConversationOut,
    status_code=status.HTTP_201_CREATED,
)
def create_or_open_conversation(
    data: ConversationCreateIn,
    me: User = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    """Create on first use, or return the existing conversation for a connection."""
    connection, other_user = _authorized_connection(db, data.connection_id, me)
    conversation = db.scalar(
        select(Conversation).where(Conversation.connection_id == connection.connection_id)
    )
    if conversation is None:
        conversation = Conversation(connection_id=connection.connection_id)
        db.add(conversation)
        db.flush()
    db.commit()
    db.refresh(conversation)
    return _conversation_out(db, conversation, connection, other_user)


@router.get("/conversations/{conversation_id}", response_model=ConversationOut)
def get_conversation(
    conversation_id: int,
    me: User = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    conversation, connection, other_user = _conversation_context(db, conversation_id, me)
    return _conversation_out(db, conversation, connection, other_user)


@router.get("/conversations/{conversation_id}/messages", response_model=list[ChatMessageOut])
def list_messages(
    conversation_id: int,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    me: User = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    conversation, _, _ = _conversation_context(db, conversation_id, me)
    newest_first = db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation.conversation_id)
        .order_by(Message.sent_at.desc(), Message.message_id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return [ChatMessageOut.model_validate(message) for message in reversed(newest_first)]


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=ChatMessageOut,
    status_code=status.HTTP_201_CREATED,
)
def send_message(
    conversation_id: int,
    data: ChatMessageIn,
    me: User = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    conversation, connection, other_user = _conversation_context(db, conversation_id, me)
    if connection.status != "Active":
        raise HTTPException(status_code=409, detail="Cannot send messages to a removed connection")
    message = Message(
        conversation_id=conversation.conversation_id,
        sender_id=me.user_id,
        message_text=data.message_text,
    )
    db.add(message)
    db.flush()
    notify(
        db,
        other_user.user_id,
        "new_message",
        "New message",
        f"{me.full_name} sent you a message.",
        "Message",
        message.message_id,
    )
    db.commit()
    db.refresh(message)
    return message


@router.post(
    "/conversations/{conversation_id}/read",
    response_model=ConversationReadOut,
)
def mark_received_messages_read(
    conversation_id: int,
    me: User = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    conversation, _, other_user = _conversation_context(db, conversation_id, me)
    result = db.execute(
        update(Message)
        .where(
            Message.conversation_id == conversation.conversation_id,
            Message.sender_id == other_user.user_id,
            Message.is_read.is_(False),
        )
        .values(is_read=True)
    )
    db.commit()
    return ConversationReadOut(
        conversation_id=conversation.conversation_id,
        marked_count=result.rowcount or 0,
    )
