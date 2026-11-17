"""HTTP API contract tests through the ASGI app with a mocked upstream world."""

import httpx
import pytest
import respx
from app.core.clock import ManualClock
from app.main import create_app
from scenario import WALLET_A, FakeRest, World, make_runtime, scenario_settings
from sqlalchemy.ext.asyncio import create_async_engine

from conftest import TEST_DB
from factories import NOW


@pytest.fixture
async def client(sm):
    clock = ManualClock(NOW)
    rest = FakeRest(clock)
    world = World()

    async def factory(_cfg):
        return await make_runtime(TEST_DB, clock, rest, create_async_engine(TEST_DB))

    app = create_app(scenario_settings(TEST_DB), runtime_factory=factory)
    with respx.mock(assert_all_called=False) as router:
        world.install(router)
        router.route(host="test").pass_through()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                yield c


async def auth(client) -> dict[str, str]:
    r = await client.post("/api/auth/login", json={"username": "admin", "password": "change-me"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_auth_required_and_login(client):
    assert (await client.get("/api/health")).json() == {"status": "ok"}
    assert (await client.get("/api/dashboard")).status_code == 401
    bad = await client.post("/api/auth/login", json={"username": "admin", "password": "nope"})
    assert bad.status_code == 401
    h = await auth(client)
    assert (await client.get("/api/auth/me", headers=h)).json() == {"username": "admin"}
    forged = {"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.e30.x"}
    assert (await client.get("/api/dashboard", headers=forged)).status_code == 401


async def test_wallet_crud(client):
    h = await auth(client)
    r = await client.post(
        "/api/wallets", json={"address": WALLET_A, "nickname": "whale"}, headers=h
    )
    assert r.status_code == 201
    w = r.json()
    assert w["address"] == WALLET_A and w["follow"] is True
    assert w["estimated_equity"] is None and w["equity_coverage"] == "UNAVAILABLE"
    dup = await client.post(
        "/api/wallets", json={"address": WALLET_A.upper().replace("0X", "0x")}, headers=h
    )
    assert dup.status_code == 409
    bad = await client.post("/api/wallets", json={"address": "0x" + "zz" * 20}, headers=h)
    assert bad.status_code == 422
    p = await client.patch(
        f"/api/wallets/{w['id']}",
        headers=h,
        json={"follow": False, "sizing_override": {"multiplier": "2"}},
    )
    assert p.json()["follow"] is False and p.json()["sizing_override"] == {"multiplier": "2"}
    invalid = await client.patch(
        f"/api/wallets/{w['id']}", headers=h, json={"sizing_override": {"multiplier": "-1"}}
    )
    assert invalid.status_code == 422
    detail = (await client.get(f"/api/wallets/{w['id']}", headers=h)).json()
    assert detail["metrics"]["trade_count"] == 0
    assert (await client.delete(f"/api/wallets/{w['id']}", headers=h)).status_code == 204
    assert (await client.get("/api/wallets", headers=h)).json() == {"wallets": []}
    assert (await client.get(f"/api/wallets/{w['id']}", headers=h)).status_code == 404


async def test_settings_versioning_and_validation(client):
    h = await auth(client)
    cur = (await client.get("/api/settings", headers=h)).json()
    v = cur["version"]
    ok = await client.put(
        "/api/settings",
        headers=h,
        json={"settings": {"sizing": {"multiplier": "1.5"}}, "expected_version": v},
    )
    assert ok.status_code == 200 and ok.json()["settings"]["sizing"]["multiplier"] == "1.5"
    stale = await client.put(
        "/api/settings",
        headers=h,
        json={"settings": {"sizing": {"multiplier": "2"}}, "expected_version": v},
    )
    assert stale.status_code == 409
    invalid = await client.put(
        "/api/settings", headers=h, json={"settings": {"risk": {"max_pct_per_trade": "5"}}}
    )
    assert invalid.status_code == 422


async def test_controls_and_dashboard(client):
    h = await auth(client)
    d = (await client.get("/api/dashboard", headers=h)).json()
    assert d["mode"] == "PAPER" and d["portfolio"]["equity"] == "1000"
    assert d["portfolio"]["cash"] == "1000" and d["trades_copied_today"] == 0
    assert (await client.post("/api/control/pause", headers=h)).json()["settings"][
        "strategies_paused"
    ] is True
    stop = (await client.post("/api/control/emergency-stop", headers=h)).json()
    assert stop["settings"]["emergency_stop"] is True
    resumed = (await client.post("/api/control/resume", headers=h)).json()
    assert resumed["settings"]["strategies_paused"] is False
    assert resumed["settings"]["emergency_stop"] is True  # resume never clears e-stop
    reset = await client.post(
        "/api/control/reset-paper", headers=h, json={"starting_bankroll": "750"}
    )
    assert reset.json()["equity"] == "750"
    health = (await client.get("/api/system/health", headers=h)).json()
    assert health["ledger_ok"] is True and health["mode"] == "PAPER"
    assert health["metrics"]["copy_stages"] == [], "no copies yet, no stage timings"
    assert health["metrics"]["mempool_stream_connected"] is False
    events = (await client.get("/api/system/events", headers=h)).json()["events"]
    assert {e["kind"] for e in events} >= {"EMERGENCY_STOP", "PAPER_RESET"}
    assert (await client.get("/api/positions", headers=h)).json() == {"positions": []}
    hist = await client.get("/api/portfolio/equity-history?range=bogus", headers=h)
    assert hist.status_code == 422
