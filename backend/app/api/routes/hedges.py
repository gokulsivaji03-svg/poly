from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status

from app.api.auth import User
from app.api.routes.core import rt_of

router = APIRouter(prefix="/api/hedges")


@router.get("/opportunities")
async def opportunities(request: Request, _: User) -> dict[str, Any]:
    return await rt_of(request).hedger.payload()


@router.post("/{opp_id}/execute")
async def execute(opp_id: str, request: Request, _: User) -> dict[str, Any]:
    if ":" not in opp_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown opportunity")
    return await rt_of(request).hedger.execute(opp_id, manual=True)


@router.post("/{opp_id}/merge")
async def merge(opp_id: str, request: Request, _: User) -> dict[str, Any]:
    if ":" not in opp_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown opportunity")
    return await rt_of(request).hedger.merge(opp_id)


@router.get("/events")
async def events(
    request: Request, _: User, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> dict[str, Any]:
    return {"events": await rt_of(request).hedger.events_list(limit)}
