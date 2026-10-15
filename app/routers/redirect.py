from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from app import metrics
from app.deps import RedisDep, SessionDep, SettingsDep
from app.services import clicks
from app.services.links import CODE_RE, resolve

router = APIRouter(tags=["redirect"])


@router.head("/{code}", include_in_schema=False, response_class=RedirectResponse, status_code=status.HTTP_302_FOUND)
@router.get(
    "/{code}",
    summary="Follow a short link",
    response_class=RedirectResponse,
    status_code=status.HTTP_302_FOUND,
    responses={302: {"description": "Redirect to the original URL"}, 404: {"description": "Unknown short code"}},
)
async def follow(code: str, request: Request, session: SessionDep, redis: RedisDep, settings: SettingsDep):
    if not CODE_RE.fullmatch(code):
        metrics.REDIRECTS.labels("not_found").inc()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    found, outcome = await resolve(session, redis, settings, code)
    metrics.REDIRECTS.labels(outcome).inc()
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    link_id, target = found
    if request.method == "GET":
        await clicks.record_click(redis, request.app.state.sessionmaker, link_id)
    # 302 rather than 301 so browsers never cache a link we may later delete, and every click is counted.
    return RedirectResponse(target, status_code=status.HTTP_302_FOUND, headers={"Cache-Control": "private, max-age=0"})
