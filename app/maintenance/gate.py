from __future__ import annotations

import sqlite3
from typing import Any

from app.core.errors import ConflictError

#: 维护窗口阶段。顺序即允许的推进方向。
PHASE_PLANNED = "planned"
PHASE_DRAINING = "draining"
PHASE_CLAIM_PAUSED = "claim_paused"
PHASE_SWITCHED = "switched"
PHASE_COMPLETED = "completed"
PHASE_ABORTED = "aborted"
PHASE_EXPIRED = "expired"

ORDERED_PHASES = (
    PHASE_PLANNED,
    PHASE_DRAINING,
    PHASE_CLAIM_PAUSED,
    PHASE_SWITCHED,
    PHASE_COMPLETED,
)

#: 仍然对服务单生效（拒绝普通新单）的阶段。
ACTIVE_GATE_PHASES = (PHASE_DRAINING, PHASE_CLAIM_PAUSED, PHASE_SWITCHED)
#: 中断领取（调度员只能领取紧急豁免任务）的阶段。
CLAIM_PAUSED_PHASES = (PHASE_CLAIM_PAUSED, PHASE_SWITCHED)
#: 终态，不再影响后续计划。
TERMINAL_PHASES = (PHASE_COMPLETED, PHASE_ABORTED, PHASE_EXPIRED)


def active_gate(connection: sqlite3.Connection) -> sqlite3.Row | None:
    """返回当前生效中的维护窗口；不存在时返回 None。"""
    placeholders = ",".join("?" for _ in ACTIVE_GATE_PHASES)
    return connection.execute(
        f"SELECT * FROM maintenance_windows WHERE status IN ({placeholders}) ORDER BY id DESC LIMIT 1",
        ACTIVE_GATE_PHASES,
    ).fetchone()


def ensure_submission_allowed(connection: sqlite3.Connection) -> None:
    gate = active_gate(connection)
    if gate is not None:
        raise ConflictError(
            "维护窗口期间暂停接收新的服务单，请走紧急白事豁免流程",
            context={"window_id": int(gate["id"]), "phase": gate["status"]},
        )


def claim_exempt_only(connection: sqlite3.Connection) -> bool:
    """领取是否被限制为紧急豁免任务（排空阶段仍允许普通领取）。"""
    gate = active_gate(connection)
    return gate is not None and gate["status"] in CLAIM_PAUSED_PHASES


def remaining_work(connection: sqlite3.Connection, now: str, window_id: int | None = None) -> dict[str, Any]:
    """统计排空情况；紧急豁免任务单列，不计入阻塞项。"""
    clauses = ["t.emergency_exempt=0"]
    params: list[Any] = []
    if window_id is not None:
        clauses.append("(t.maintenance_window_id=? OR t.maintenance_window_id IS NULL)")
        params.append(window_id)
    where = " WHERE " + " AND ".join(clauses)
    rows = connection.execute(
        "SELECT t.status,t.lease_expires_at FROM compute_tasks t" + where,
        params,
    ).fetchall()
    queued = 0
    running_active = 0
    running_lease_expired = 0
    cancel_requested = 0
    for row in rows:
        status = row["status"]
        if status == "queued":
            queued += 1
        elif status == "running":
            if row["lease_expires_at"] and row["lease_expires_at"] <= now:
                running_lease_expired += 1
            else:
                running_active += 1
        elif status == "cancel_requested":
            cancel_requested += 1
    exempt = connection.execute(
        "SELECT COUNT(*) FROM compute_tasks WHERE emergency_exempt=1 AND status IN ('queued','running','cancel_requested')"
    ).fetchone()[0]
    return {
        "queued": queued,
        "running_active": running_active,
        "running_lease_expired": running_lease_expired,
        "cancel_requested": cancel_requested,
        "exempt_in_flight": int(exempt),
        "ready_to_switch": queued == 0 and running_active == 0 and running_lease_expired == 0 and cancel_requested == 0,
    }
