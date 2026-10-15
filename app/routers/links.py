from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import func, select

from app import metrics
from app.config import Settings
from app.deps import CurrentUser, RedisDep, SessionDep, SettingsDep
from app.logging import get_logger
from app.models import Link
from app.schemas import LinkCreate, LinkOut, Page
from app.services import links as link_service

router = APIRouter(prefix="/api/v1/links", tags=["links"])
log = get_logger(__name__)


def link_out(settings: Settings, link: Link) -> LinkOut:
    return LinkOut(
        id=link.id,
        code=link.code,
        short_url=f"{settings.public_base_url}/{link.code}",
        target_url=link.target_url,
        is_custom=link.is_custom,
        click_count=link.click_count,
        last_clicked_at=link.last_clicked_at,
        created_at=link.created_at,
        owner_id=link.owner_id,
    )


@router.post("", response_model=LinkOut, status_code=status.HTTP_201_CREATED, summary="Create a short link")
async def create_link(
    body: LinkCreate, user: CurrentUser, session: SessionDep, redis: RedisDep, settings: SettingsDep
) -> LinkOut:
    link = await link_service.create_link(session, redis, settings, user, body.url, body.alias)
    metrics.LINKS_CREATED.labels("custom" if link.is_custom else "generated").inc()
    log.info("link.created", link_id=link.id, code=link.code, custom=link.is_custom)
    return link_out(settings, link)


@router.get("", response_model=Page[LinkOut], summary="List my links")
async def list_links(
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[LinkOut]:
    """Click counts are updated in batches, so they can lag by a few seconds."""
    total = await session.scalar(select(func.count()).select_from(Link).where(Link.owner_id == user.id))
    rows = (
        await session.scalars(
            select(Link)
            .where(Link.owner_id == user.id)
            .order_by(Link.created_at.desc(), Link.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(items=[link_out(settings, r) for r in rows], total=total or 0, limit=limit, offset=offset)


async def _own_link(session, user, link_id: int) -> Link:
    link = await session.get(Link, link_id)
    if link is None or link.owner_id != user.id:  # 404 (not 403) so ids can't be probed
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link not found")
    return link


@router.get("/{link_id}", response_model=LinkOut, summary="Get one of my links")
async def get_link(link_id: int, user: CurrentUser, session: SessionDep, settings: SettingsDep) -> LinkOut:
    return link_out(settings, await _own_link(session, user, link_id))


@router.delete("/{link_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete one of my links")
async def delete_link(link_id: int, user: CurrentUser, session: SessionDep, redis: RedisDep) -> Response:
    link = await _own_link(session, user, link_id)
    code = link.code
    await session.delete(link)
    await session.commit()
    await link_service.cache_forget(redis, [code])
    log.info("link.deleted", link_id=link_id, code=code)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
