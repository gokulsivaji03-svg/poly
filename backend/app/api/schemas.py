"""Request bodies (responses are plain dicts built by ``app.api.queries``)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain.market import Dec


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginIn(Body):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class WalletIn(Body):
    address: str = Field(min_length=42, max_length=42)
    nickname: str = Field(default="", max_length=64)
    follow: bool = True


class WalletPatch(Body):
    nickname: str | None = Field(default=None, max_length=64)
    follow: bool | None = None
    sizing_override: dict[str, Any] | None = None

    model_config = ConfigDict(extra="forbid")


class SettingsIn(Body):
    settings: dict[str, Any]
    expected_version: int | None = None


class ResetIn(Body):
    starting_bankroll: Dec = Field(gt=0)
