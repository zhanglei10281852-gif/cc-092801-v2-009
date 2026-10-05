from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class WindowPlan(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    title: str = Field(min_length=2, max_length=120)
    planned_start_at: datetime
    planned_end_at: datetime
    exemption_rules: dict[str, dict[str, bool]] = Field(default_factory=dict)
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)
    idempotency_key: str = Field(min_length=6, max_length=160)


class PhaseTransition(BaseModel):
    target_phase: Literal["draining", "claim_paused", "lease_check", "switched", "completed", "aborted"]
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)
    idempotency_key: str = Field(min_length=6, max_length=160)
    expected_version: int | None = Field(default=None, ge=1)


class RecoveryRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)
    idempotency_key: str = Field(min_length=6, max_length=160)
