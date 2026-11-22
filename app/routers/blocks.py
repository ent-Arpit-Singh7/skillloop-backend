"""User blocking controls. Blocks prevent new learning requests and hide the connection from both users."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.deps import get_current_user
from app.models import User, UserBlock
from app.schemas import BlockOut

router = APIRouter(prefix="/api", tags=["user-safety"])


@router.post("/users/{user_id}/block", response_model=BlockOut, status_code=201)
def block_user(user_id: int, me: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user_id == me.user_id:
        raise HTTPException(status_code=400, detail="You cannot block yourself")
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    existing = db.scalar(select(UserBlock).where(
        UserBlock.blocker_id == me.user_id, UserBlock.blocked_id == user_id
    ))
    if existing:
        raise HTTPException(status_code=409, detail="User is already blocked")
    row = UserBlock(blocker_id=me.user_id, blocked_id=user_id)
    db.add(row)
    db.commit()
    db.refresh(row)
    return BlockOut(
        block_id=row.block_id, blocked_id=target.user_id,
        blocked_user_name=target.full_name, created_at=row.created_at,
    )


@router.delete("/users/{user_id}/block", status_code=status.HTTP_204_NO_CONTENT)
def unblock_user(user_id: int, me: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.scalar(select(UserBlock).where(
        UserBlock.blocker_id == me.user_id, UserBlock.blocked_id == user_id
    ))
    if row is None:
        raise HTTPException(status_code=404, detail="Blocked user not found")
    db.delete(row)
    db.commit()


@router.get("/me/blocked-users", response_model=list[BlockOut])
def list_blocked_users(me: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(UserBlock).where(UserBlock.blocker_id == me.user_id).order_by(UserBlock.block_id.desc())).all()
    result = []
    for row in rows:
        target = db.get(User, row.blocked_id)
        if target:
            result.append(BlockOut(
                block_id=row.block_id, blocked_id=target.user_id,
                blocked_user_name=target.full_name, created_at=row.created_at,
            ))
    return result
