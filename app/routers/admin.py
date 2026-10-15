from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete, func, select

from app.deps import AdminUser, RedisDep, SessionDep, SettingsDep, require_admin
from app.logging import get_logger
from app.models import Link, User
from app.routers.links import link_out
from app.schemas import LinkOut, Page, UserOut
from app.services import links as link_service

router = APIRouter(prefix="/api/v1/admin", tags=["admin"], dependencies=[Depends(require_admin)])
log = get_logger(__name__)


@router.get("/users", response_model=Page[UserOut], summary="List users")
async def list_users(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    q: Annotated[str | None, Query(max_length=100, description="Filter by email substring")] = None,
) -> Page[UserOut]:
    where = []
    if q:
        escaped = q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append(User.email.like(f"%{escaped}%", escape="\\"))
    total = await session.scalar(select(func.count()).select_from(User).where(*where))
    rows = (
        await session.scalars(
            select(User).where(*where).order_by(User.created_at.desc(), User.id).limit(limit).offset(offset)
        )
    ).all()
    return Page(items=[UserOut.model_validate(u) for u in rows], total=total or 0, limit=limit, offset=offset)


@router.get("/users/{user_id}", response_model=UserOut, summary="Get a user")
async def get_user(user_id: uuid.UUID, session: SessionDep) -> UserOut:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return UserOut.model_validate(user)


@router.delete(
    "/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a user and all of their links",
)
async def delete_user(user_id: uuid.UUID, admin: AdminUser, session: SessionDep, redis: RedisDep) -> Response:
    if user_id == admin.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "Administrators cannot delete their own account")
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if user.is_admin:
        other_admins = await session.scalar(
            select(func.count()).select_from(User).where(User.role == "admin", User.is_active, User.id != user.id)
        )
        if not other_admins:
            raise HTTPException(status.HTTP_409_CONFLICT, "Cannot delete the last administrator")

    codes = list((await session.scalars(select(Link.code).where(Link.owner_id == user.id))).all())
    await session.execute(delete(User).where(User.id == user.id))  # links/tokens go via ON DELETE CASCADE
    await session.commit()
    await link_service.cache_forget(redis, codes)
    log.info("admin.user_deleted", target_user_id=str(user_id), links_removed=len(codes), admin_id=str(admin.id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/links", response_model=Page[LinkOut], summary="List all links")
async def list_all_links(
    session: SessionDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[LinkOut]:
    total = await session.scalar(select(func.count()).select_from(Link))
    rows = (
        await session.scalars(select(Link).order_by(Link.created_at.desc(), Link.id.desc()).limit(limit).offset(offset))
    ).all()
    return Page(items=[link_out(settings, r) for r in rows], total=total or 0, limit=limit, offset=offset)


@router.delete("/links/{link_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete any link")
async def delete_any_link(
    link_id: int, admin: AdminUser, session: SessionDep, redis: RedisDep
) -> Response:
    link = await session.get(Link, link_id)
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link not found")
    code = link.code
    await session.delete(link)
    await session.commit()
    await link_service.cache_forget(redis, [code])
    log.info("admin.link_deleted", link_id=link_id, code=code, admin_id=str(admin.id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
