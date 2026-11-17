"""Process configuration from environment (prefix ``POLYCOPY_``). Secrets never logged."""

from __future__ import annotations

import secrets
from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ExecutionMode(StrEnum):
    PAPER = "PAPER"
    REPLAY = "REPLAY"
    LIVE = "LIVE"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="POLYCOPY_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = "postgresql+asyncpg://localhost/polycopy"
    run_workers: bool = True
    """Start strategy workers in this process (still gated by the leader lock)."""
    execution_mode: ExecutionMode = ExecutionMode.PAPER

    data_api_url: str = "https://data-api.polymarket.com"
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    geoblock_url: str = "https://polymarket.com/api/geoblock"
    polygon_rpc_url: str = "https://polygon-bor-rpc.publicnode.com"
    """Polygon JSON-RPC: the trade feed (exchange fill logs) and source collateral reads."""
    polygon_ws_url: str | None = None
    """WebSocket endpoint for pushed fill logs; defaults to polygon_rpc_url as wss://."""
    chain_mempool: bool | None = None
    """Also copy from pending ``matchOrders`` transactions (median 0.8 s before their block,
    measured by scripts/mempool_probe.py); needs a node serving full pending bodies. Unset,
    it is on for PAPER and REPLAY and off for LIVE: a pending transaction can still revert,
    so live copying from the mempool is an explicit choice."""
    chain_poll_interval_s: float = Field(default=0.5, gt=0)
    """Backstop log polling interval while the WebSocket stream is down."""
    chain_backstop_interval_s: float = Field(default=2.0, gt=0)
    """Backstop log polling interval while the WebSocket stream is up."""
    chain_lookback_blocks: int = Field(default=30, ge=0)
    """Blocks re-read on the very first start (no persisted cursor yet)."""

    @property
    def mempool_enabled(self) -> bool:
        if self.chain_mempool is not None:
            return self.chain_mempool
        return self.execution_mode is not ExecutionMode.LIVE

    @property
    def chain_ws_url(self) -> str | None:
        if self.polygon_ws_url:
            return self.polygon_ws_url
        if self.polygon_rpc_url.startswith("https://"):
            return "wss://" + self.polygon_rpc_url.removeprefix("https://")
        return None

    equity_snapshot_interval_s: float = Field(default=60.0, gt=0)
    """How often each watched wallet's public equity is re-observed."""
    portfolio_snapshot_interval_s: float = Field(default=30.0, gt=0)
    backfill_hours: int = Field(default=48, ge=0, le=24 * 90)
    """History loaded when a wallet is first added (metrics only; never copied)."""
    http_timeout_s: float = 10.0
    http_max_retries: int = 5

    auth_username: str = "admin"
    auth_password: SecretStr = SecretStr("change-me")
    jwt_secret: SecretStr = Field(default_factory=lambda: SecretStr(secrets.token_urlsafe(48)))
    """Unset -> random per process (sessions end on restart). Set it in production."""
    jwt_ttl_minutes: int = 12 * 60
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # Live execution: every flag must be explicitly enabled AND the venue must pass checks.
    live_trading_enabled: bool = False
    live_private_key: SecretStr | None = None
    live_funder_address: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
