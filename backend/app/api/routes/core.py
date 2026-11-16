"""Auth, health, dashboard, portfolio, feeds, settings/control and system routes."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import ValidationError
from sqlalchemy import text

from app.api import queries as q
from app.api.auth import User, check_password, issue_token
from app.api.schemas import LoginIn, ResetIn, SettingsIn
from app.domain.settings import SizingSettings
from app.runtime import Runtime
from app.services.settings_service import SettingsConflict

router = APIRouter(prefix="/api")


def rt_of(request: Request) -> Runtime:
    return request.app.state.runtime


# ----------------------------------------------------------------------- auth
@router.post("/auth/login")
async def login(body: LoginIn, request: Request) -> dict[str, Any]:
    rt = rt_of(request)
    if not check_password(rt.cfg, body.username, body.password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    # Token lifetimes always use wall-clock time, never the (possibly simulated) strategy clock.
    token, exp = issue_token(rt.cfg, body.username, datetime.now(UTC).timestamp())
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_at": datetime.fromtimestamp(exp, tz=UTC).isoformat(),
    }


@router.get("/auth/me")
async def me(user: User) -> dict[str, str]:
    return {"username": user}


# --------------------------------------------------------------------- health
@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request) -> dict[str, Any]:
    rt = rt_of(request)
    db_ok = True
    try:
        async with rt.sm() as s:
            await s.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    return {
        "ready": db_ok,
        "database": db_ok,
        "leader": rt.supervisor.is_leader,
        "workers": q.worker_status(rt),
    }


# ------------------------------------------------------------------ portfolio
@router.get("/dashboard")
async def dashboard(request: Request, _: User) -> dict[str, Any]:
    return await q.dashboard(rt_of(request))


@router.get("/portfolio/equity-history")
async def equity_history(
    request: Request, _: User, range: Annotated[str, Query(pattern="^(1d|7d|30d|all)$")] = "7d"
) -> dict[str, Any]:
    return await q.equity_history(rt_of(request), range)


@router.get("/positions")
async def positions(request: Request, _: User) -> dict[str, Any]:
    rt = rt_of(request)
    states = getattr(rt, "hedge_states", lambda: {})()
    return await q.positions(rt, states)


@router.get("/orders")
async def orders(
    request: Request, _: User, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> dict[str, Any]:
    return await q.orders(rt_of(request), limit)


# ---------------------------------------------------------------------- feeds
@router.get("/source-trades")
async def source_trades(
    request: Request,
    _: User,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    wallet_id: int | None = None,
) -> dict[str, Any]:
    return {"trades": await q.source_trades(rt_of(request), limit=limit, wallet_id=wallet_id)}


@router.get("/copy-signals")
async def copy_signals(
    request: Request, _: User, limit: Annotated[int, Query(ge=1, le=500)] = 50
) -> dict[str, Any]:
    return {"signals": await q.copy_signals(rt_of(request), limit=limit)}


# ------------------------------------------------------------ settings/control
async def _settings_payload(rt: Runtime) -> dict[str, Any]:
    st = await rt.settings.get()
    return {"version": rt.settings.version, "settings": st.model_dump(mode="json", by_alias=True)}


@router.get("/settings")
async def get_settings(request: Request, _: User) -> dict[str, Any]:
    return await _settings_payload(rt_of(request))


@router.put("/settings")
async def put_settings(body: SettingsIn, request: Request, _: User) -> dict[str, Any]:
    rt = rt_of(request)
    try:
        await rt.settings.update(body.settings, expected_version=body.expected_version)
    except SettingsConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            validation_detail(exc),
        ) from exc
    payload = await _settings_payload(rt)
    rt.bus.publish("settings", payload)
    return payload


async def _control(rt: Runtime, patch: dict[str, Any], event: str) -> dict[str, Any]:
    await rt.settings.update(patch)
    await rt.events.record(event, f"Operator action: {event}", level="WARNING")
    payload = await _settings_payload(rt)
    rt.bus.publish("settings", payload)
    rt.bus.publish("portfolio", {})
    return payload


@router.post("/control/pause")
async def pause(request: Request, _: User) -> dict[str, Any]:
    return await _control(rt_of(request), {"strategies_paused": True}, "STRATEGIES_PAUSED")


@router.post("/control/resume")
async def resume(request: Request, _: User) -> dict[str, Any]:
    return await _control(rt_of(request), {"strategies_paused": False}, "STRATEGIES_RESUMED")


@router.post("/control/emergency-stop")
async def emergency_stop(request: Request, _: User) -> dict[str, Any]:
    rt = rt_of(request)
    payload = await _control(rt, {"emergency_stop": True}, "EMERGENCY_STOP")
    released = await rt.ledger.release_idle(rt.account_id)
    if released:
        await rt.events.record("RESERVATIONS_RELEASED", f"Released {released} idle reservations")
    return payload


@router.post("/control/clear-emergency")
async def clear_emergency(request: Request, _: User) -> dict[str, Any]:
    return await _control(rt_of(request), {"emergency_stop": False}, "EMERGENCY_CLEARED")


@router.post("/control/reset-paper")
async def reset_paper(body: ResetIn, request: Request, _: User) -> dict[str, Any]:
    rt = rt_of(request)
    if rt.cfg.execution_mode.value == "LIVE":
        raise HTTPException(status.HTTP_409_CONFLICT, "Cannot reset a live account")
    await rt.ledger.reset_account(rt.account_id, body.starting_bankroll)
    await rt.settings.update({"paper": {"starting_bankroll_usd": str(body.starting_bankroll)}})
    await rt.events.record(
        "PAPER_RESET", f"Paper account reset to {body.starting_bankroll}", level="WARNING"
    )
    rt.bus.publish("portfolio", {})
    rt.bus.publish("positions", {})
    return await q.portfolio(rt)


# --------------------------------------------------------------------- system
@router.get("/system/health")
async def system_health(request: Request, _: User) -> dict[str, Any]:
    rt = rt_of(request)
    problems = await rt.ledger.verify(rt.account_id)
    venues = getattr(rt, "venue_status", None)
    return {
        "mode": rt.cfg.execution_mode.value,
        "venues": await venues() if venues else [],
        "workers": q.worker_status(rt),
        "metrics": await q.system_metrics(rt),
        "ledger_ok": not problems,
        "ledger_problems": problems,
    }


@router.get("/system/events")
async def system_events(
    request: Request, _: User, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> dict[str, Any]:
    return await q.system_events(rt_of(request), limit)


def validate_sizing_override(override: dict[str, Any] | None) -> None:
    if override is None:
        return
    try:
        SizingSettings.model_validate({**SizingSettings().model_dump(mode="json"), **override})
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            validation_detail(exc),
        ) from exc


def validation_detail(exc: ValidationError) -> list[dict[str, Any]]:
    """Pydantic errors as JSON-safe dicts (inputs may contain Decimals)."""
    return json.loads(exc.json(include_url=False, include_context=False))
