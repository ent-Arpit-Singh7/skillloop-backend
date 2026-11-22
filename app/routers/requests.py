"""Learning requests and mutual connection requests."""
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.deps import get_current_user
from app.models import (
    Connection,
    ConnectionRequest,
    NetworkConnectionRequest,
    TeachingSkill,
    User,
    UserBlock,
)
from app.notifications import mark_notification_entity_read, notify
from app.schemas import (
    ConnectionOut,
    NetworkConnectionRequestIn,
    NetworkConnectionRequestOut,
    PublicUserOut,
    RequestIn,
    RequestOut,
)

router = APIRouter(prefix="/api", tags=["requests-and-connections"])


# ---------- helpers ----------
def _request_for(db: Session, request_id: int, me: User) -> ConnectionRequest:
    req = db.get(ConnectionRequest, request_id)
    if req is None or me.user_id not in (req.sender_id, req.receiver_id):
        raise HTTPException(status_code=404, detail="Request not found")
    return req


def _connection_for(db: Session, connection_id: int, me: User) -> Connection:
    conn = db.get(Connection, connection_id)
    if (
        conn is None
        or conn.status != "Active"
        or me.user_id not in (conn.user_one_id, conn.user_two_id)
    ):
        raise HTTPException(status_code=404, detail="Connection not found")
    other_id = conn.user_two_id if conn.user_one_id == me.user_id else conn.user_one_id
    blocked = db.scalar(select(UserBlock.block_id).where(
        or_(
            (UserBlock.blocker_id == me.user_id) & (UserBlock.blocked_id == other_id),
            (UserBlock.blocker_id == other_id) & (UserBlock.blocked_id == me.user_id),
        )
    ))
    if blocked:
        raise HTTPException(status_code=403, detail="This connection is unavailable because one user has blocked the other")
    return conn


def _other_id(conn: Connection, me: User) -> int:
    return conn.user_two_id if conn.user_one_id == me.user_id else conn.user_one_id


def _connection_out(db: Session, conn: Connection, me: User) -> ConnectionOut:
    other = db.get(User, _other_id(conn, me))
    if other is None or other.account_status != "Active":
        raise HTTPException(status_code=404, detail="Connection not found")
    return ConnectionOut(
        connection_id=conn.connection_id,
        created_at=conn.created_at,
        status=conn.status,
        other_user=PublicUserOut.model_validate(other),
    )


def _pending_by(db: Session, request_id: int, me: User, *, who: Literal["receiver", "sender"]) -> ConnectionRequest:
    req = _request_for(db, request_id, me)
    owner = req.receiver_id if who == "receiver" else req.sender_id
    if owner != me.user_id:
        raise HTTPException(status_code=403, detail=f"Only the {who} can do this")
    if req.status != "Pending":
        raise HTTPException(status_code=409, detail=f"Request is already {req.status}")
    return req


def _network_request_for(
    db: Session,
    request_id: int,
    me: User,
) -> NetworkConnectionRequest:
    req = db.scalar(
        select(NetworkConnectionRequest)
        .where(NetworkConnectionRequest.request_id == request_id)
        .with_for_update()
    )
    if req is None or me.user_id not in (req.sender_id, req.receiver_id):
        raise HTTPException(status_code=404, detail="Connection request not found")
    return req


def _network_pending_by(
    db: Session,
    request_id: int,
    me: User,
    *,
    who: Literal["receiver", "sender"],
) -> NetworkConnectionRequest:
    req = _network_request_for(db, request_id, me)
    owner = req.receiver_id if who == "receiver" else req.sender_id
    if owner != me.user_id:
        raise HTTPException(status_code=403, detail=f"Only the {who} can do this")
    if req.status != "Pending":
        raise HTTPException(status_code=409, detail=f"Connection request is already {req.status}")
    return req


def _network_request_out(req: NetworkConnectionRequest) -> NetworkConnectionRequestOut:
    return NetworkConnectionRequestOut(
        request_id=req.request_id,
        sender_id=req.sender_id,
        receiver_id=req.receiver_id,
        sender=PublicUserOut.model_validate(req.sender),
        receiver=PublicUserOut.model_validate(req.receiver),
        optional_message=req.optional_message,
        request_date=req.request_date,
        status=req.status,
        connection_id=req.connection_id,
    )


# ---------- Learning requests ----------
@router.post("/requests", response_model=RequestOut, status_code=201)
def create_request(data: RequestIn, me: User = Depends(get_current_user), db: Session = Depends(get_db)):
    skill = db.get(TeachingSkill, data.teaching_skill_id)
    if skill is None or not skill.is_active or skill.user.account_status != "Active":
        raise HTTPException(status_code=404, detail="Teaching skill not found")
    if skill.user_id == me.user_id:
        raise HTTPException(status_code=400, detail="You cannot request your own teaching skill")
    blocked = db.scalar(select(UserBlock.block_id).where(
        or_(
            (UserBlock.blocker_id == me.user_id) & (UserBlock.blocked_id == skill.user_id),
            (UserBlock.blocker_id == skill.user_id) & (UserBlock.blocked_id == me.user_id),
        )
    ))
    if blocked:
        raise HTTPException(status_code=403, detail="You cannot send a request because one user has blocked the other")
    duplicate = db.scalar(
        select(ConnectionRequest.request_id).where(
            ConnectionRequest.sender_id == me.user_id,
            ConnectionRequest.receiver_id == skill.user_id,
            ConnectionRequest.teaching_skill_id == skill.teaching_skill_id,
            ConnectionRequest.status == "Pending",
        )
    )
    if duplicate:
        raise HTTPException(status_code=409, detail="You already have a pending request for this skill")
    req = ConnectionRequest(
        sender_id=me.user_id,
        receiver_id=skill.user_id,
        teaching_skill_id=skill.teaching_skill_id,
        optional_message=data.optional_message,
    )
    db.add(req)
    db.flush()
    notify(
        db, skill.user_id, "request_received", "New learning request",
        f"{me.full_name} wants to learn {skill.skill_name} from you.",
        "ConnectionRequest", req.request_id,
    )
    db.commit()
    db.refresh(req)
    return req


@router.get("/requests", response_model=list[RequestOut])
def list_requests(
    box: Literal["sent", "received", "all"] = "all",
    status: Literal["Pending", "Accepted", "Rejected", "Cancelled"] | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    me: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if box == "sent":
        cond = ConnectionRequest.sender_id == me.user_id
    elif box == "received":
        cond = ConnectionRequest.receiver_id == me.user_id
    else:
        cond = or_(ConnectionRequest.sender_id == me.user_id, ConnectionRequest.receiver_id == me.user_id)
    stmt = select(ConnectionRequest).where(cond)
    if status:
        stmt = stmt.where(ConnectionRequest.status == status)
    return db.scalars(stmt.order_by(ConnectionRequest.request_id.desc()).limit(limit).offset(offset)).all()


@router.get("/requests/{request_id}", response_model=RequestOut)
def get_request(request_id: int, me: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _request_for(db, request_id, me)


@router.post("/requests/{request_id}/accept", response_model=ConnectionOut)
def accept_request(request_id: int, me: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Receiver only. Uses the database function that creates or reuses a Connection for session scheduling."""
    req = _pending_by(db, request_id, me, who="receiver")
    conn_id = db.execute(
        text("SELECT fn_accept_connection_request(:rid, :uid)"), {"rid": req.request_id, "uid": me.user_id}
    ).scalar_one()
    notify(
        db, req.sender_id, "request_accepted", "Request accepted",
        f"{me.full_name} accepted your learning request.",
        "ConnectionRequest", req.request_id,
    )
    db.commit()
    return _connection_out(db, db.get(Connection, conn_id), me)


@router.post("/requests/{request_id}/reject", response_model=RequestOut)
def reject_request(request_id: int, me: User = Depends(get_current_user), db: Session = Depends(get_db)):
    req = _pending_by(db, request_id, me, who="receiver")
    req.status = "Rejected"
    db.flush()
    notify(
        db, req.sender_id, "request_rejected", "Request rejected",
        f"{me.full_name} rejected your request.", "ConnectionRequest", req.request_id,
    )
    db.commit()
    db.refresh(req)
    return req


@router.post("/requests/{request_id}/cancel", response_model=RequestOut)
def cancel_request(request_id: int, me: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Sender only, while the request is still Pending."""
    req = _pending_by(db, request_id, me, who="sender")
    req.status = "Cancelled"
    db.commit()
    db.refresh(req)
    return req


# ---------- User connections (for learning sessions and chat) ----------
@router.get("/connections", response_model=list[ConnectionOut])
def list_connections(me: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """List active connections for arranging learning sessions and starting chats."""
    conns = db.scalars(
        select(Connection).where(
            or_(Connection.user_one_id == me.user_id, Connection.user_two_id == me.user_id),
            Connection.status == "Active",
            ~select(UserBlock.block_id).where(
                or_(
                    (UserBlock.blocker_id == me.user_id) & (UserBlock.blocked_id == Connection.user_one_id + Connection.user_two_id - me.user_id),
                    (UserBlock.blocker_id == Connection.user_one_id + Connection.user_two_id - me.user_id) & (UserBlock.blocked_id == me.user_id),
                )
            ).exists(),
        )
    ).all()
    out = []
    for conn in conns:
        other_id = _other_id(conn, me)
        other = db.get(User, other_id)
        if other is not None and other.account_status == "Active":
            out.append(_connection_out(db, conn, me))
    out.sort(key=lambda c: c.created_at, reverse=True)
    return out


@router.get("/connections/{connection_id}", response_model=ConnectionOut)
def get_connection(connection_id: int, me: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _connection_out(db, _connection_for(db, connection_id, me), me)


# ---------- Independent mutual connection requests ----------
@router.post(
    "/connection-requests",
    response_model=NetworkConnectionRequestOut,
    status_code=201,
)
def create_connection_request(
    data: NetworkConnectionRequestIn,
    me: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if data.receiver_id == me.user_id:
        raise HTTPException(status_code=400, detail="You cannot connect with yourself")

    receiver = db.get(User, data.receiver_id)
    if receiver is None or receiver.account_status != "Active":
        raise HTTPException(status_code=404, detail="User not found")

    blocked = db.scalar(
        select(UserBlock.block_id).where(
            or_(
                (UserBlock.blocker_id == me.user_id)
                & (UserBlock.blocked_id == receiver.user_id),
                (UserBlock.blocker_id == receiver.user_id)
                & (UserBlock.blocked_id == me.user_id),
            )
        )
    )
    if blocked:
        raise HTTPException(
            status_code=403,
            detail="Cannot send a connection request because one user has blocked the other",
        )

    low_id = min(me.user_id, receiver.user_id)
    high_id = max(me.user_id, receiver.user_id)
    existing_connection = db.scalar(
        select(Connection).where(
            Connection.user_one_id == low_id,
            Connection.user_two_id == high_id,
            Connection.status == "Active",
        )
    )
    if existing_connection is not None:
        raise HTTPException(status_code=409, detail="You are already connected")

    duplicate = db.scalar(
        select(NetworkConnectionRequest.request_id).where(
            NetworkConnectionRequest.status == "Pending",
            or_(
                (
                    (NetworkConnectionRequest.sender_id == me.user_id)
                    & (NetworkConnectionRequest.receiver_id == receiver.user_id)
                ),
                (
                    (NetworkConnectionRequest.sender_id == receiver.user_id)
                    & (NetworkConnectionRequest.receiver_id == me.user_id)
                ),
            ),
        )
    )
    if duplicate:
        raise HTTPException(status_code=409, detail="A pending connection request already exists")

    req = NetworkConnectionRequest(
        sender_id=me.user_id,
        receiver_id=receiver.user_id,
        optional_message=data.optional_message,
    )
    db.add(req)
    db.flush()
    notify(
        db,
        receiver.user_id,
        "request_received",
        "New connection request",
        f"{me.full_name} wants to connect with you.",
        "NetworkConnectionRequest",
        req.request_id,
    )
    db.commit()
    db.refresh(req)
    return _network_request_out(req)


@router.get(
    "/connection-requests",
    response_model=list[NetworkConnectionRequestOut],
)
def list_connection_requests(
    box: Literal["sent", "received", "all"] = "all",
    status: Literal["Pending", "Accepted", "Rejected", "Cancelled"] | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    me: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if box == "sent":
        condition = NetworkConnectionRequest.sender_id == me.user_id
    elif box == "received":
        condition = NetworkConnectionRequest.receiver_id == me.user_id
    else:
        condition = or_(
            NetworkConnectionRequest.sender_id == me.user_id,
            NetworkConnectionRequest.receiver_id == me.user_id,
        )

    stmt = select(NetworkConnectionRequest).where(condition)
    if status:
        stmt = stmt.where(NetworkConnectionRequest.status == status)
    rows = db.scalars(
        stmt.order_by(NetworkConnectionRequest.request_id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return [_network_request_out(req) for req in rows]


@router.post(
    "/connection-requests/{request_id}/accept",
    response_model=ConnectionOut,
)
def accept_connection_request(
    request_id: int,
    me: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    req = _network_pending_by(db, request_id, me, who="receiver")

    sender = db.get(User, req.sender_id)
    if sender is None or sender.account_status != "Active":
        raise HTTPException(status_code=404, detail="Sender not found")
    blocked = db.scalar(
        select(UserBlock.block_id).where(
            or_(
                (UserBlock.blocker_id == sender.user_id) & (UserBlock.blocked_id == me.user_id),
                (UserBlock.blocker_id == me.user_id) & (UserBlock.blocked_id == sender.user_id),
            )
        )
    )
    if blocked:
        raise HTTPException(status_code=403, detail="Cannot accept a connection request from this user")

    low_id = min(req.sender_id, req.receiver_id)
    high_id = max(req.sender_id, req.receiver_id)
    conn = db.scalar(
        select(Connection)
        .where(
            Connection.user_one_id == low_id,
            Connection.user_two_id == high_id,
        )
        .with_for_update()
    )
    if conn is None:
        conn = Connection(
            user_one_id=low_id,
            user_two_id=high_id,
            status="Active",
            removed_at=None,
            removed_by_id=None,
        )
        db.add(conn)
        db.flush()
    else:
        conn.status = "Active"
        conn.removed_at = None
        conn.removed_by_id = None

    req.status = "Accepted"
    req.connection_id = conn.connection_id
    db.flush()
    mark_notification_entity_read(
        db,
        "NetworkConnectionRequest",
        req.request_id,
        recipient_id=req.receiver_id,
    )
    notify(
        db,
        req.sender_id,
        "request_accepted",
        "Connection request accepted",
        f"{me.full_name} accepted your connection request.",
        "NetworkConnectionRequest",
        req.request_id,
    )
    db.commit()
    db.refresh(conn)
    return _connection_out(db, conn, me)


@router.post(
    "/connection-requests/{request_id}/reject",
    response_model=NetworkConnectionRequestOut,
)
def reject_connection_request(
    request_id: int,
    me: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    req = _network_pending_by(db, request_id, me, who="receiver")
    req.status = "Rejected"
    db.flush()
    mark_notification_entity_read(
        db,
        "NetworkConnectionRequest",
        req.request_id,
        recipient_id=req.receiver_id,
    )
    notify(
        db,
        req.sender_id,
        "request_rejected",
        "Connection request rejected",
        f"{me.full_name} rejected your connection request.",
        "NetworkConnectionRequest",
        req.request_id,
    )
    db.commit()
    db.refresh(req)
    return _network_request_out(req)


@router.post(
    "/connection-requests/{request_id}/cancel",
    response_model=NetworkConnectionRequestOut,
)
def cancel_connection_request(
    request_id: int,
    me: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    req = _network_pending_by(db, request_id, me, who="sender")
    req.status = "Cancelled"
    db.commit()
    db.refresh(req)
    return _network_request_out(req)


@router.delete(
    "/connections/{connection_id}",
    status_code=204,
)
def remove_connection(
    connection_id: int,
    me: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conn = db.scalar(
        select(Connection)
        .where(Connection.connection_id == connection_id)
        .with_for_update()
    )
    if (
        conn is None
        or conn.status != "Active"
        or me.user_id not in (conn.user_one_id, conn.user_two_id)
    ):
        raise HTTPException(status_code=404, detail="Connection not found")

    conn.status = "Removed"
    conn.removed_at = datetime.now(timezone.utc)
    conn.removed_by_id = me.user_id
    db.commit()


