"""Message save/unsave endpoints + saved-messages list (T4, m3-agent-chat-essential-feature).

Endpoints:
  POST   /messages/{message_id}/save   — save a message for the current user
  DELETE /messages/{message_id}/save   — unsave (idempotent)
  GET    /messages/saved               — list current user's saved messages
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, Response
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db
from src.api.identity import Identity, require_identity
from src.db.models import Message, MessageSave, Session

router = APIRouter()


async def _get_message(db: AsyncSession, message_id: int) -> Message:
    """Fetch a message by PK or raise 404."""
    result = await db.execute(select(Message).where(Message.id == message_id))
    msg = result.scalar_one_or_none()
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found.")
    return msg


@router.post("/messages/{message_id}/save", status_code=201)
async def save_message(
    message_id: int,
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Save a message for the current user.

    Idempotent: if the user has already saved this message, returns 200 instead
    of 201 — the save state is unchanged.
    """
    if not identity.user_id:
        raise HTTPException(status_code=400, detail="Missing caller identity.")

    await _get_message(db, message_id)

    existing = await db.get(MessageSave, (message_id, identity.user_id))
    if existing is not None:
        return JSONResponse(
            {
                "saved": True,
                "message_id": str(message_id),
                "saved_at": existing.saved_at,
            },
            status_code=200,
        )

    now = time.time()
    try:
        db.add(MessageSave(message_id=message_id, user_id=identity.user_id, saved_at=now))
        await db.commit()
        return JSONResponse(
            {"saved": True, "message_id": str(message_id), "saved_at": now},
            status_code=201,
        )
    except IntegrityError:
        await db.rollback()
        existing = await db.get(MessageSave, (message_id, identity.user_id))
        return JSONResponse(
            {
                "saved": True,
                "message_id": str(message_id),
                "saved_at": existing.saved_at if existing else now,
            },
            status_code=200,
        )


@router.delete("/messages/{message_id}/save", status_code=204)
async def unsave_message(
    message_id: int,
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Remove a user's saved bookmark on a message.

    Idempotent: no-op if the message was not saved.
    """
    if not identity.user_id:
        raise HTTPException(status_code=400, detail="Missing caller identity.")

    await _get_message(db, message_id)

    await db.execute(
        delete(MessageSave).where(
            MessageSave.message_id == message_id,
            MessageSave.user_id == identity.user_id,
        )
    )
    await db.commit()

    return Response(status_code=204)


@router.get("/messages/saved")
async def list_saved_messages(
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List all messages saved by the current user, newest-saved-first.

    Each entry embeds the message content and its session context (id, title)
    for use by the frontend Saved-items view.
    """
    if not identity.user_id:
        raise HTTPException(status_code=400, detail="Missing caller identity.")

    result = await db.execute(
        select(
            Message.id,
            Message.content,
            Message.role,
            Message.author_id,
            Message.created_at,
            Message.edited_at,
            Message.session_id,
            MessageSave.saved_at,
            Session.title.label("session_title"),
            Session.kind.label("session_kind"),
        )
        .join(MessageSave, MessageSave.message_id == Message.id)
        .join(Session, Session.id == Message.session_id)
        .where(MessageSave.user_id == identity.user_id)
        .order_by(MessageSave.saved_at.desc())
    )

    rows = result.all()
    messages: list[dict[str, Any]] = []
    for row in rows:
        entry: dict[str, Any] = {
            "id": str(row.id),
            "content": row.content or "",
            "role": row.role,
            "author_id": row.author_id,
            "created_at": row.created_at,
            "session_id": row.session_id,
            "session_title": row.session_title or "(untitled)",
            "session_kind": row.session_kind,
            "saved_at": row.saved_at,
        }
        if row.edited_at is not None:
            entry["edited_at"] = row.edited_at
        messages.append(entry)

    return JSONResponse({"messages": messages})
