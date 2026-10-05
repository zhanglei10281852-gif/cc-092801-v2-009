from __future__ import annotations

from fastapi import APIRouter, Query

from app.core.clock import current_clock
from app.maintenance_window.schemas import PhaseTransition, RecoveryRequest, WindowPlan
from app.maintenance_window.service import MaintenanceWindowService

router = APIRouter(prefix="/api/maintenance-windows", tags=["维护窗口"])


def service() -> MaintenanceWindowService:
    return MaintenanceWindowService(clock=current_clock())


@router.post("", status_code=201)
def plan_window(payload: WindowPlan):
    return service().plan(payload.model_dump())


@router.get("")
def list_windows(phase: str | None = None, limit: int = Query(default=100, ge=1, le=500)):
    return {"items": service().list_windows(phase=phase, limit=limit)}


@router.get("/current")
def current_window():
    return service().current()


@router.get("/{window_id}")
def window_detail(window_id: int):
    return service().get_window(window_id)


@router.post("/{window_id}/transitions")
def transition_window(window_id: int, payload: PhaseTransition):
    return service().transition(window_id, payload.model_dump())


@router.post("/{window_id}/recover")
def recover_window(window_id: int, payload: RecoveryRequest):
    return service().recover(window_id, payload.model_dump())


@router.get("/{window_id}/exemptions")
def window_exemptions(window_id: int):
    return {"items": service().exemptions(window_id)}
