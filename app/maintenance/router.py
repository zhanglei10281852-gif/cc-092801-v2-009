from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.database import get_connection
from app.maintenance.schemas import EmergencyExemptionRequest, LeaseReconcile, PhaseTransition, WindowPlan
from app.maintenance.service import MaintenanceWindowService

router = APIRouter(prefix="/api/maintenance/windows", tags=["维护窗口"])


def service() -> MaintenanceWindowService:
    return MaintenanceWindowService(get_connection())


@router.get("/current")
def current_window(principal: Principal = Depends(current_principal)) -> dict | None:
    principal.require("maintenance.operate")
    return service().current_window()


@router.get("/exemptions/list")
def list_exemptions(window_id: int | None = Query(default=None), principal: Principal = Depends(current_principal)) -> dict:
    principal.require("maintenance.operate")
    return {"items": service().list_exemptions(window_id)}


@router.get("/{window_id}")
def get_window(window_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("maintenance.operate")
    return service().get_window(window_id)


@router.get("/{window_id}/events")
def list_events(window_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("maintenance.operate")
    return {"items": service().list_events(window_id)}


@router.get("/{window_id}/leases")
def inspect_leases(
    window_id: int,
    actor: str = Query(default="", max_length=120),
    reason: str = Query(default="", max_length=1000),
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("maintenance.operate")
    return service().inspect_leases(window_id, actor or principal.display_name, reason)


@router.post("/plan", status_code=201)
def plan_window(payload: WindowPlan, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("maintenance.operate")
    return service().plan_window(payload.model_dump(), principal.display_name)


@router.post("/{window_id}/drain")
def enter_draining(window_id: int, payload: PhaseTransition, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("maintenance.operate")
    return service().enter_draining(window_id, principal.display_name, payload.reason, expected_version=payload.expected_version)


@router.post("/{window_id}/pause-claims")
def pause_claims(window_id: int, payload: PhaseTransition, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("maintenance.operate")
    return service().pause_claims(window_id, principal.display_name, payload.reason, expected_version=payload.expected_version)


@router.post("/{window_id}/switch")
def complete_switch(window_id: int, payload: PhaseTransition, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("maintenance.operate")
    return service().complete_switch(window_id, principal.display_name, payload.reason, expected_version=payload.expected_version)


@router.post("/{window_id}/complete")
def complete(window_id: int, payload: PhaseTransition, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("maintenance.operate")
    return service().complete(window_id, principal.display_name, payload.reason, expected_version=payload.expected_version)


@router.post("/{window_id}/abort")
def abort(window_id: int, payload: PhaseTransition, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("maintenance.operate")
    return service().abort(window_id, principal.display_name, payload.reason, expected_version=payload.expected_version)


@router.post("/{window_id}/reconcile-leases")
def reconcile_leases(window_id: int, payload: LeaseReconcile, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("maintenance.operate")
    return service().reconcile_expired_leases(window_id, principal.display_name, payload.reason)


@router.post("/recover")
def recover(principal: Principal = Depends(current_principal)) -> dict:
    principal.require("maintenance.operate")
    return service().recover_on_startup()


@router.post("/emergency-exemption", status_code=201)
def emergency_exemption(payload: EmergencyExemptionRequest) -> dict:
    # 与服务单接口一致：值班主管身份以 approver + approval_code 凭据记录并审计。
    return service().submit_emergency_exemption(payload.model_dump(), payload.approver)
