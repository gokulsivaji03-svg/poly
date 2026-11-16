from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy import select

from app.api.auth import User
from app.api.routes.core import rt_of, validation_detail
from app.db.models import ReplayRun
from app.services.replay import ReplayBusy, ReplayParams

router = APIRouter(prefix="/api/replay")


def _run_dict(run: ReplayRun, *, full: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": run.id,
        "status": run.status,
        "params": run.params,
        "created_at": run.created_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "error": run.error,
        "summary": (run.result or {}).get("summary"),
    }
    if full:
        out["equity_curve"] = (run.result or {}).get("equity_curve", [])
        out["signals"] = (run.result or {}).get("signals", [])
    return out


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def start_replay(body: dict[str, Any], request: Request, _: User) -> dict[str, Any]:
    try:
        params = ReplayParams.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, validation_detail(exc)) from exc
    try:
        run_id = await rt_of(request).replay.submit(params)
    except ReplayBusy as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"id": run_id, "status": "RUNNING"}


@router.get("")
async def list_runs(request: Request, _: User) -> dict[str, Any]:
    async with rt_of(request).sm() as s:
        runs = (await s.scalars(select(ReplayRun).order_by(ReplayRun.id.desc()).limit(50))).all()
    return {"runs": [_run_dict(r, full=False) for r in runs]}


@router.get("/{run_id}")
async def get_run(run_id: int, request: Request, _: User) -> dict[str, Any]:
    async with rt_of(request).sm() as s:
        run = await s.get(ReplayRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Replay run not found")
    return _run_dict(run, full=True)
