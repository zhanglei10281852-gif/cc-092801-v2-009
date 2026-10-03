from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from app.compute.repository import ComputeRepository
from app.compute.service import digest
from app.core.clock import Clock, from_storage, to_storage
from app.core.clock import domain_clock
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import transaction
from app.maintenance.exemptions import EXEMPTION_RULE_CODE, evaluate_exemption
from app.maintenance.gate import (
    ACTIVE_GATE_PHASES,
    ORDERED_PHASES,
    PHASE_ABORTED,
    PHASE_CLAIM_PAUSED,
    PHASE_COMPLETED,
    PHASE_DRAINING,
    PHASE_EXPIRED,
    PHASE_PLANNED,
    PHASE_SWITCHED,
    TERMINAL_PHASES,
    active_gate,
    remaining_work,
)
from app.repositories.audit import AuditRepository

#: 允许的阶段推进路径；abort/expire 为旁路，单独处理。
TRANSITIONS: dict[tuple[str, str], str] = {
    (PHASE_PLANNED, PHASE_DRAINING): "enter_draining",
    (PHASE_DRAINING, PHASE_CLAIM_PAUSED): "pause_claims",
    (PHASE_CLAIM_PAUSED, PHASE_SWITCHED): "complete_switch",
    (PHASE_SWITCHED, PHASE_COMPLETED): "complete",
    # 排空/暂停阶段发现异常可以终止窗口恢复服务。
    (PHASE_DRAINING, PHASE_ABORTED): "abort",
    (PHASE_CLAIM_PAUSED, PHASE_ABORTED): "abort",
    (PHASE_SWITCHED, PHASE_ABORTED): "abort",
}

EVENT_TYPE_BY_PHASE = {
    PHASE_PLANNED: "planned",
    PHASE_DRAINING: "enter_draining",
    PHASE_CLAIM_PAUSED: "pause_claims",
    PHASE_SWITCHED: "complete_switch",
    PHASE_COMPLETED: "complete",
    PHASE_ABORTED: "abort",
    PHASE_EXPIRED: "expire",
}


class MaintenanceWindowService:
    """带版本的维护窗口编排：计划、排空、暂停领取、租约检查、切换与恢复。"""

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = domain_clock(clock)

    # ------------------------------------------------------------------ plan

    def plan_window(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        reason = str(payload.get("reason") or "").strip()
        if len(reason) < 2:
            raise ValidationError("维护原因不能为空且至少 2 个字符")
        now_value = self.clock.now()
        now = to_storage(now_value)
        deadline = payload.get("drain_deadline")
        deadline_text = ""
        if deadline:
            if isinstance(deadline, str):
                parsed = from_storage(deadline)
                if parsed is None:
                    raise ValidationError("drain_deadline 时间格式不合法")
                deadline_dt = parsed
            else:
                deadline_dt = deadline
            if deadline_dt <= now_value:
                raise ValidationError("排空截止时间必须晚于当前时间")
            deadline_text = to_storage(deadline_dt)
        with self._immediate() as connection:
            self._sweep_stale_plans(connection, now)
            gate = active_gate(connection)
            if gate is not None:
                raise ConflictError(
                    "已存在进行中的维护窗口，不能重复计划",
                    context={"window_id": int(gate["id"]), "phase": gate["status"]},
                )
            pending = connection.execute(
                "SELECT * FROM maintenance_windows WHERE status=? ORDER BY id DESC LIMIT 1",
                (PHASE_PLANNED,),
            ).fetchone()
            if pending is not None:
                raise ConflictError(
                    "已存在尚未进入排空的维护窗口",
                    context={"window_id": int(pending["id"]), "phase": PHASE_PLANNED},
                )
            cursor = connection.execute(
                "INSERT INTO maintenance_windows(version,reason,planned_by,planned_at,drain_deadline,status,"
                "last_transition_at,last_transition_by,created_at,updated_at) "
                "VALUES(1,?,?,?,?,?,?,?,?,?)",
                (reason, actor, now, deadline_text, PHASE_PLANNED, now, "", now, now),
            )
            window_id = int(cursor.lastrowid)
            self._append_event(connection, window_id, PHASE_PLANNED, "planned", actor, reason, {"drain_deadline": deadline_text}, now)
            self._audit(connection, actor, "maintenance.window.plan", window_id, after={"status": PHASE_PLANNED, "reason": reason}, now=now)
            return self._serialize(self._require_window(connection, window_id), connection, now)

    # -------------------------------------------------------------- transitions

    def enter_draining(self, window_id: int, actor: str, reason: str, *, expected_version: int | None = None) -> dict[str, Any]:
        return self._advance(window_id, PHASE_DRAINING, actor, reason, expected_version=expected_version)

    def pause_claims(self, window_id: int, actor: str, reason: str, *, expected_version: int | None = None) -> dict[str, Any]:
        return self._advance(window_id, PHASE_CLAIM_PAUSED, actor, reason, expected_version=expected_version)

    def complete_switch(self, window_id: int, actor: str, reason: str, *, expected_version: int | None = None) -> dict[str, Any]:
        reason = (reason or "").strip()
        if len(reason) < 2:
            raise ValidationError("阶段变更原因不能为空且至少 2 个字符")
        with self._immediate() as connection:
            window = self._require_window(connection, window_id)
            now = to_storage(self.clock.now())
            # 幂等：重复切换直接返回现状。
            if window["status"] == PHASE_SWITCHED:
                return self._serialize(window, connection, now, changed=False)
            self._check_version(window, expected_version)
            self._require_transition(window["status"], PHASE_SWITCHED)
            remaining = remaining_work(connection, now, window_id)
            if not remaining["ready_to_switch"]:
                raise ConflictError("仍有未收敛的服务单或有效租约，不能完成切换", context=remaining)
            self._apply_phase(connection, window, PHASE_SWITCHED, actor, reason, {"remaining": remaining}, now)
            return self._serialize(self._require_window(connection, window_id), connection, now)

    def complete(self, window_id: int, actor: str, reason: str, *, expected_version: int | None = None) -> dict[str, Any]:
        return self._advance(window_id, PHASE_COMPLETED, actor, reason, expected_version=expected_version)

    def abort(self, window_id: int, actor: str, reason: str, *, expected_version: int | None = None) -> dict[str, Any]:
        """异常恢复：终止窗口并立即恢复接单/领取能力。幂等。"""
        with self._immediate() as connection:
            window = self._require_window(connection, window_id)
            now = to_storage(self.clock.now())
            if window["status"] == PHASE_ABORTED:
                return self._serialize(window, connection, now, changed=False)
            self._check_version(window, expected_version)
            if window["status"] in TERMINAL_PHASES:
                raise ConflictError(f"窗口已处于终态 {window['status']}，不能异常终止")
            self._apply_phase(connection, window, PHASE_ABORTED, actor, reason, {}, now)
            return self._serialize(self._require_window(connection, window_id), connection, now)

    def _advance(self, window_id: int, target: str, actor: str, reason: str, *, expected_version: int | None) -> dict[str, Any]:
        reason = (reason or "").strip()
        if len(reason) < 2:
            raise ValidationError("阶段变更原因不能为空且至少 2 个字符")
        with self._immediate() as connection:
            self._sweep_stale_plans(connection, to_storage(self.clock.now()))
            window = self._require_window(connection, window_id)
            now = to_storage(self.clock.now())
            if window["status"] == PHASE_EXPIRED:
                raise ConflictError("维护窗口已过期，不能再推进，请重新计划")
            # 幂等：重复推进到当前阶段直接返回现状，不重复落事件。
            if window["status"] == target:
                return self._serialize(window, connection, now, changed=False)
            self._check_version(window, expected_version)
            self._require_transition(window["status"], target)
            detail: dict[str, Any] = {}
            if target == PHASE_SWITCHED:
                detail["remaining"] = remaining_work(connection, now, window_id)
            self._apply_phase(connection, window, target, actor, reason, detail, now)
            return self._serialize(self._require_window(connection, window_id), connection, now)

    # ------------------------------------------------------------- lease checks

    def inspect_leases(self, window_id: int, actor: str = "", reason: str = "") -> dict[str, Any]:
        """检查剩余租约（只读），并留痕一次检查事件。"""
        with self._immediate() as connection:
            window = self._require_window(connection, window_id)
            now = to_storage(self.clock.now())
            remaining = remaining_work(connection, now, window_id)
            active_leases = [
                dict(row)
                for row in connection.execute(
                    "SELECT id,lease_owner,lease_expires_at,started_at,emergency_exempt FROM compute_tasks "
                    "WHERE status='running' ORDER BY id"
                ).fetchall()
            ]
            exempt = [
                dict(row)
                for row in connection.execute(
                    "SELECT id,status,lease_owner,lease_expires_at FROM compute_tasks "
                    "WHERE emergency_exempt=1 AND status IN ('queued','running','cancel_requested') ORDER BY id"
                ).fetchall()
            ]
            if actor:
                self._append_event(
                    connection, window_id, window["status"], "lease_inspection",
                    actor, reason or "检查剩余租约", {"remaining": remaining, "active_lease_count": len(active_leases)}, now,
                )
            return {
                "window_id": window_id,
                "phase": window["status"],
                "version": window["version"],
                "remaining": remaining,
                "active_leases": active_leases,
                "exempt_in_flight": exempt,
                "checked_at": now,
            }

    def reconcile_expired_leases(self, window_id: int, actor: str, reason: str) -> dict[str, Any]:
        """租约异常处置：把过期租约按既有恢复策略回收到排队/失败，便于排空收敛。"""
        from app.compute.service import ComputeOperationsService

        reason = (reason or "").strip()
        if len(reason) < 2:
            raise ValidationError("处置原因不能为空且至少 2 个字符")
        with self._immediate() as connection:
            window = self._require_window(connection, window_id)
            if window["status"] not in ACTIVE_GATE_PHASES:
                raise ConflictError("只有进行中的维护窗口可以处置租约")
            now_text = to_storage(self.clock.now())
            expired_ids = [
                int(row[0])
                for row in connection.execute(
                    "SELECT id FROM compute_tasks WHERE status='running' AND lease_expires_at<>'' AND lease_expires_at<=? ORDER BY id",
                    (now_text,),
                ).fetchall()
            ]
            # 复用计算运营领域的过期租约恢复逻辑（同连接同时钟，不另开事务）。
            compute_service = ComputeOperationsService(connection, self.clock)
            recovered = compute_service._recover_expired_locked(ComputeRepository(connection), now_text, actor)
            self._append_event(
                connection, window_id, window["status"], "lease_reconcile",
                actor, reason, {"expired_lease_ids": expired_ids, **recovered}, now_text,
            )
            self._audit(
                connection, actor, "maintenance.window.lease_reconcile", window_id,
                after=recovered, metadata={"reason": reason}, now=now_text,
            )
            return {"recovered": recovered, "remaining": remaining_work(connection, now_text, window_id)}

    # -------------------------------------------------------------- exemptions

    def submit_emergency_exemption(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        """维护期间的紧急白事豁免：规则全部命中才建单，拒绝同样落审计。"""
        now_value = self.clock.now()
        now = to_storage(now_value)
        with self._immediate() as connection:
            gate = active_gate(connection)
            if gate is None:
                raise ConflictError("当前没有进行中的维护窗口，无需紧急豁免")
            window_id = int(gate["id"])
            provided_window = payload.get("window_id")
            if provided_window is not None and int(provided_window) != window_id:
                raise ConflictError("申请的窗口不是当前生效窗口", context={"active_window_id": window_id})
            decision = evaluate_exemption(payload, window_active=True)
            repository = ComputeRepository(connection)

            existing = repository.task_by_idempotency(payload["requested_by"], payload["idempotency_key"])
            if existing is not None:
                if not existing["emergency_exempt"]:
                    raise ConflictError("同一幂等键已用于普通服务单，不能改为豁免单")
                exemption = connection.execute(
                    "SELECT * FROM maintenance_emergency_exemptions WHERE task_id=? ORDER BY id DESC LIMIT 1",
                    (existing["id"],),
                ).fetchone()
                return {"task": dict(existing), "exemption": dict(exemption) if exemption else None, "idempotent_replay": True}

            if not decision.accepted:
                # 拒绝也要留痕：先在事务内记录并提交，事务外再抛出冲突。
                self._record_exemption(connection, window_id, None, payload, decision, now, actor)
                self._append_event(
                    connection, window_id, gate["status"], "exemption_rejected",
                    actor, payload.get("reason") or "紧急白事豁免申请被拒",
                    {"failures": decision.failures, "evidence": decision.evidence}, now,
                )
                self._audit(
                    connection, actor, "maintenance.exemption.rejected", window_id,
                    after={"failures": decision.failures}, outcome="failure", now=now,
                )
                rejected = ConflictError("紧急白事豁免条件不满足", context={"failures": decision.failures})
            else:
                template = repository.template_by_code(payload["template_code"])
                if template is None or not template["active"]:
                    raise NotFoundError("参数模板不存在或已经停用")
                # 复用计算领域的参数校验。
                from app.compute.service import ComputeOperationsService

                parameters = ComputeOperationsService(connection, self.clock)._validate_parameters(template, payload["parameters"])
                parameter_digest = digest(parameters)
                cursor = connection.execute(
                    "INSERT INTO compute_tasks(template_id,project_code,requested_by,parameters_json,parameter_digest,"
                    "priority,idempotency_key,status,attempt_count,max_attempts,available_at,"
                    "maintenance_window_id,emergency_exempt,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,'queued',0,?,?,?,1,?,?)",
                    (
                        template["id"], payload["project_code"], payload["requested_by"],
                        json.dumps(parameters, ensure_ascii=False, sort_keys=True), parameter_digest,
                        payload["priority"], payload["idempotency_key"],
                        int(template["max_attempts"]), to_storage(now_value),
                        window_id, now, now,
                    ),
                )
                task = dict(repository.task_by_id(int(cursor.lastrowid)))
                decision_row = self._record_exemption(connection, window_id, task["id"], payload, decision, now, actor)
                self._append_event(
                    connection, window_id, gate["status"], "exemption_accepted",
                    actor, payload.get("reason") or "紧急白事豁免放行",
                    {"task_id": task["id"], "rule_code": EXEMPTION_RULE_CODE, "evidence": decision.evidence}, now,
                )
                self._audit(
                    connection, actor, "maintenance.exemption.accepted", window_id,
                    resource_id=task["id"], after={"task_id": task["id"], "rule_code": EXEMPTION_RULE_CODE}, now=now,
                )
                accepted_result = {"task": task, "exemption": decision_row, "idempotent_replay": False}
        if not decision.accepted:
            raise rejected
        return accepted_result

    def list_exemptions(self, window_id: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM maintenance_emergency_exemptions"
        params: list[Any] = []
        if window_id is not None:
            sql += " WHERE window_id=?"
            params.append(window_id)
        sql += " ORDER BY id DESC"
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    # ---------------------------------------------------------------- reads

    def get_window(self, window_id: int) -> dict[str, Any]:
        window = self._require_window(self.connection, window_id)
        return self._serialize(window, self.connection, to_storage(self.clock.now()))

    def current_window(self) -> dict[str, Any] | None:
        gate = active_gate(self.connection)
        if gate is None:
            row = self.connection.execute("SELECT * FROM maintenance_windows ORDER BY id DESC LIMIT 1").fetchone()
            return self._serialize(row, self.connection, to_storage(self.clock.now())) if row is not None else None
        return self._serialize(gate, self.connection, to_storage(self.clock.now()))

    def list_events(self, window_id: int) -> list[dict[str, Any]]:
        self._require_window(self.connection, window_id)
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM maintenance_window_events WHERE window_id=? ORDER BY id", (window_id,)
            ).fetchall()
        ]

    # ------------------------------------------------------------- recovery

    def recover_on_startup(self) -> dict[str, Any]:
        """服务重启后调用：过期计划失效，进行中窗口保持原阶段（门禁以数据库为准）。"""
        now = to_storage(self.clock.now())
        with self._immediate() as connection:
            self._sweep_stale_plans(connection, now)
            gate = active_gate(connection)
            if gate is not None:
                return {"resumed_window_id": int(gate["id"]), "phase": gate["status"], "gate_active": True}
            latest = connection.execute("SELECT * FROM maintenance_windows ORDER BY id DESC LIMIT 1").fetchone()
            return {
                "resumed_window_id": int(latest["id"]) if latest else None,
                "phase": latest["status"] if latest else None,
                "gate_active": False,
            }

    # ---------------------------------------------------------------- helpers

    def _apply_phase(
        self, connection: sqlite3.Connection, window: sqlite3.Row, target: str,
        actor: str, reason: str, detail: dict[str, Any], now: str,
    ) -> None:
        timestamp_column = {
            PHASE_DRAINING: "entered_draining_at",
            PHASE_CLAIM_PAUSED: "claim_paused_at",
            PHASE_SWITCHED: "switched_at",
            PHASE_COMPLETED: "completed_at",
            PHASE_ABORTED: "aborted_at",
            PHASE_EXPIRED: "expired_at",
        }.get(target)
        sql = (
            "UPDATE maintenance_windows SET status=?,version=version+1,last_transition_at=?,"
            "last_transition_by=?,updated_at=?"
        )
        params: list[Any] = [target, now, actor, now]
        if timestamp_column:
            sql += f",{timestamp_column}=?"
            params.append(now)
        sql += " WHERE id=?"
        params.append(window["id"])
        connection.execute(sql, params)
        self._append_event(
            connection, int(window["id"]), target, EVENT_TYPE_BY_PHASE[target], actor, reason, detail, now
        )
        self._audit(
            connection, actor, f"maintenance.window.{EVENT_TYPE_BY_PHASE[target]}", int(window["id"]),
            before={"status": window["status"], "version": window["version"]},
            after={"status": target, "reason": reason, **({"detail": detail} if detail else {})}, now=now,
        )

    def _append_event(
        self, connection: sqlite3.Connection, window_id: int, phase: str, event_type: str,
        actor: str, reason: str, detail: dict[str, Any], now: str,
    ) -> None:
        connection.execute(
            "INSERT INTO maintenance_window_events(window_id,phase,event_type,actor,reason,detail_json,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (window_id, phase, event_type, actor, reason, json.dumps(detail, ensure_ascii=False, sort_keys=True), now),
        )

    def _record_exemption(
        self, connection: sqlite3.Connection, window_id: int, task_id: int | None,
        payload: dict[str, Any], decision: Any, now: str, actor: str,
    ) -> dict[str, Any]:
        approver = str(payload.get("approver") or "").strip()
        approval_code = str(payload.get("approval_code") or "").strip()
        outcome = "accepted" if decision.accepted else "rejected"
        try:
            cursor = connection.execute(
                "INSERT INTO maintenance_emergency_exemptions(window_id,task_id,requested_by,project_code,"
                "rule_code,reason,approver,approval_code,outcome,reject_reason,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    window_id, task_id, payload["requested_by"], payload["project_code"],
                    decision.rule_code or "REJECTED", payload.get("reason") or "",
                    approver, approval_code, outcome, decision.reject_reason, now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("该授权码在本维护窗口已使用过") from exc
        row = connection.execute(
            "SELECT * FROM maintenance_emergency_exemptions WHERE id=?", (cursor.lastrowid,)
        ).fetchone()
        return dict(row)

    def _audit(
        self, connection: sqlite3.Connection, actor: str, action: str, window_id: int,
        *, after: dict | None = None, before: dict | None = None, outcome: str = "success",
        resource_id: int | None = None, metadata: dict | None = None, now: str,
    ) -> None:
        AuditRepository(connection).append(
            actor_user_id=None,
            actor_name=actor,
            action=action,
            resource_type="maintenance_window",
            resource_id=resource_id if resource_id is not None else window_id,
            outcome=outcome,
            before=before,
            after=after,
            metadata=metadata,
            correlation_id=None,
            created_at=now,
        )

    def _sweep_stale_plans(self, connection: sqlite3.Connection, now: str) -> int:
        rows = connection.execute(
            "SELECT * FROM maintenance_windows WHERE status=? AND drain_deadline<>'' AND drain_deadline<?",
            (PHASE_PLANNED, now),
        ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE maintenance_windows SET status=?,expired_at=?,last_transition_at=?,"
                "last_transition_by='system',updated_at=?,version=version+1 WHERE id=?",
                (PHASE_EXPIRED, now, now, now, row["id"]),
            )
            self._append_event(
                connection, int(row["id"]), PHASE_EXPIRED, "expire", "system",
                "超过排空截止时间仍未进入排空，窗口自动过期", {}, now,
            )
        return len(rows)

    @staticmethod
    def _require_transition(current: str, target: str) -> None:
        if (current, target) not in TRANSITIONS:
            if target in ORDERED_PHASES and current in ORDERED_PHASES:
                raise ConflictError(f"维护窗口不能从 {current} 跳到 {target}")
            raise ConflictError(f"维护窗口当前状态 {current} 不允许转到 {target}")

    @staticmethod
    def _check_version(window: sqlite3.Row, expected_version: int | None) -> None:
        if expected_version is not None and int(expected_version) != int(window["version"]):
            raise ConflictError(
                "窗口版本已变化，请刷新后重试",
                context={"expected": expected_version, "current": window["version"]},
            )

    @staticmethod
    def _require_window(connection: sqlite3.Connection, window_id: int) -> sqlite3.Row:
        window = connection.execute("SELECT * FROM maintenance_windows WHERE id=?", (window_id,)).fetchone()
        if window is None:
            raise NotFoundError("维护窗口不存在")
        return window

    def _serialize(
        self, window: sqlite3.Row, connection: sqlite3.Connection, now: str, *, changed: bool = True
    ) -> dict[str, Any]:
        result = dict(window)
        result["changed"] = changed
        result["gate_active"] = window["status"] in ACTIVE_GATE_PHASES
        result["claims_paused"] = window["status"] in {"claim_paused", "switched"}
        if window["status"] in ACTIVE_GATE_PHASES:
            result["remaining"] = remaining_work(connection, now, int(window["id"]))
        return result

    @contextmanager
    def _immediate(self) -> Iterator[sqlite3.Connection]:
        with transaction(immediate=True) as connection:
            yield connection
