from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, Response, status

from app.api import queries as q
from app.api.auth import User
from app.api.routes.core import rt_of, validate_sizing_override
from app.api.schemas import WalletIn, WalletPatch
from app.services.wallets import DuplicateWallet, WalletError

router = APIRouter(prefix="/api/wallets")


async def _get_or_404(request: Request, wallet_id: int) -> Any:
    w = await rt_of(request).registry.get(wallet_id)
    if w is None or w.removed_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Wallet not found")
    return w


@router.get("")
async def list_wallets(request: Request, _: User) -> dict[str, Any]:
    rt = rt_of(request)
    return {"wallets": [await q.wallet_summary(rt, w) for w in await rt.registry.active()]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_wallet(body: WalletIn, request: Request, _: User) -> dict[str, Any]:
    rt = rt_of(request)
    try:
        w = await rt.registry.add(body.address, body.nickname, body.follow)
    except DuplicateWallet as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "Wallet already watched") from exc
    except WalletError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    await rt.events.record("WALLET_ADDED", f"Watching {w.address}", data={"wallet_id": w.id})
    return await q.wallet_summary(rt, w)


@router.patch("/{wallet_id}")
async def patch_wallet(
    wallet_id: int, body: WalletPatch, request: Request, _: User
) -> dict[str, Any]:
    rt = rt_of(request)
    await _get_or_404(request, wallet_id)
    fields = body.model_dump(exclude_unset=True)
    if "sizing_override" in fields:
        validate_sizing_override(fields["sizing_override"])
    try:
        w = await rt.registry.update(wallet_id, fields)
    except WalletError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return await q.wallet_summary(rt, w)


@router.delete("/{wallet_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_wallet(wallet_id: int, request: Request, _: User) -> Response:
    rt = rt_of(request)
    try:
        await rt.registry.remove(wallet_id)
    except WalletError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    await rt.events.record("WALLET_REMOVED", f"Stopped watching wallet {wallet_id}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{wallet_id}")
async def wallet_detail(wallet_id: int, request: Request, _: User) -> dict[str, Any]:
    w = await _get_or_404(request, wallet_id)
    return await q.wallet_detail(rt_of(request), w)


@router.get("/{wallet_id}/trades")
async def wallet_trades(
    wallet_id: int,
    request: Request,
    _: User,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    before_id: int | None = None,
) -> dict[str, Any]:
    await _get_or_404(request, wallet_id)
    return {
        "trades": await q.source_trades(
            rt_of(request), limit=limit, wallet_id=wallet_id, before_id=before_id
        )
    }
