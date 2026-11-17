"""Coded, human-readable explanation attached to every skip, reduction and rejection."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Reason:
    code: str
    message: str
