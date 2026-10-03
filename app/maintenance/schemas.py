from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class WindowPlan(BaseModel):
    reason: str = Field(min_length=2, max_length=1000)
    drain_deadline: str | None = Field(default=None, description="ISO8601；为空表示不强制排空截止时间")


class PhaseTransition(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)
    expected_version: int | None = Field(default=None, ge=1)


class LeaseReconcile(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)


class EmergencyExemptionRequest(BaseModel):
    window_id: int | None = None
    template_code: str = Field(min_length=2, max_length=64)
    project_code: str = Field(min_length=1, max_length=80)
    requested_by: str = Field(min_length=1, max_length=80)
    parameters: dict[str, Any]
    priority: int = Field(default=90, ge=0, le=100)
    idempotency_key: str = Field(min_length=6, max_length=160)
    case_category: Literal["funeral", "wedding", "other"]
    urgency_level: Literal["immediate", "same_day", "scheduled"]
    next_of_kin_contact: str = Field(min_length=1, max_length=120)
    service_address: str = Field(min_length=2, max_length=300)
    approver: str = Field(min_length=1, max_length=120)
    approval_code: str = Field(min_length=4, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)
