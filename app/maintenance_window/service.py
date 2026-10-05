from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, MaintenanceWindowError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.maintenance_window.repository import ACTIVE_PHASES, TERMINAL_PHASES, MaintenanceWindowRepository

SUBMISSION_GATED_PHASES = ("draining", "claim_paused", "lease_check", "switched")
CLAIM_GATED_PHASES = ("claim_paused", "lease_check", "switched")
FORWARD_TRANSITIONS = {
    "planned": {"draining", "aborted"},
    "draining": {"claim_paused", "aborted"},
    "claim_paused": {"lease_check", "aborted"},
    "lease_check": {"switched", "claim_paused", "aborted"},
    "switched": {"completed"},
}
KNOWN_EXEMPTION_RULES = {"emergency_funeral"}
EMERGENCY_FUNERAL_CATEGORY = "白事"


@dataclass(frozen=True, slots=True)
class ExemptionDecision:
    window_id: int
    window_code: str
    rule_code: str
    allow_claim: bool


@dataclass(frozen=True, slots=True)
class ClaimGate:
    window_id: int
    window_code: str
    phase: str
    exempt_task_ids: tuple[int, ...]


def window_view(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["exemption_rules"] = json.loads(data.pop("exemption_rules_json") or "{}")
    return data


def transition_view(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["detail"] = json.loads(data.pop("detail_json") or "{}")
    return data


class MaintenanceWindowService:
    """维护窗口阶段机：计划、排空、暂停领取、租约检查、完成切换与异常恢复。

    所有阶段变化写入 maintenance_window_transitions（操作者、原因、幂等键），
    窗口状态持久化在 SQLite 中，服务重启后仍处于正确的排空阶段。
    """

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = MaintenanceWindowRepository(self.connection)

    def plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        start = payload["planned_start_at"]
        end = payload["planned_end_at"]
        if end <= start:
            raise ValidationError("计划结束时间必须晚于开始时间")
        rules = payload.get("exemption_rules") or {}
        self._validate_rules(rules)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = MaintenanceWindowRepository(connection)
            self._sweep_expired(repository, now)
            same_code = repository.window_by_code(payload["code"])
            if same_code is not None:
                recorded = repository.transition_by_key(same_code["id"], payload["idempotency_key"])
                if recorded is not None and recorded["to_phase"] == "planned":
                    return {"window": window_view(same_code), "transition": transition_view(recorded), "applied": False, "idempotent_replay": True}
                raise ConflictError("维护窗口编码已存在")
            active = repository.active_window()
            if active is not None:
                raise ConflictError("已存在进行中的维护窗口", context={"window_code": active["code"], "phase": active["phase"]})
            window = repository.insert_window(
                code=payload["code"], title=payload["title"],
                planned_start_at=to_storage(start), planned_end_at=to_storage(end),
                exemption_rules=rules, created_by=payload["actor"], now=now,
            )
            transition = repository.add_transition(
                window_id=window["id"], key=payload["idempotency_key"], actor=payload["actor"], reason=payload["reason"],
                from_phase="", to_phase="planned", version=1,
                detail={"planned_start_at": to_storage(start), "planned_end_at": to_storage(end)}, now=now,
            )
            return {"window": window_view(repository.window_by_id(window["id"])), "transition": transition_view(transition), "applied": True, "idempotent_replay": False}

    def transition(self, window_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        target = payload["target_phase"]
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = MaintenanceWindowRepository(connection)
            self._sweep_expired(repository, now)
            window = repository.window_by_id(window_id)
            if window is None:
                raise NotFoundError("维护窗口不存在")
            recorded = repository.transition_by_key(window_id, payload["idempotency_key"])
            if recorded is not None:
                if recorded["to_phase"] != target:
                    raise ConflictError("同一幂等键对应了不同的目标阶段")
                return {"window": window_view(repository.window_by_id(window_id)), "transition": transition_view(recorded), "applied": False, "idempotent_replay": True}
            current = window["phase"]
            if current in TERMINAL_PHASES:
                raise ConflictError("维护窗口已关闭，无法变更阶段")
            expected = payload.get("expected_version")
            if expected is not None and int(expected) != int(window["version"]):
                raise ConflictError("维护窗口版本不匹配", context={"current_version": window["version"]})
            if target == current and target != "lease_check":
                return {"window": window_view(window), "transition": None, "applied": False, "idempotent_replay": False}
            if target != current and target not in FORWARD_TRANSITIONS.get(current, set()):
                raise ConflictError(f"不允许从阶段 {current} 转换到 {target}")
            detail: dict[str, Any] = {}
            if target == "lease_check":
                detail = {"remaining_leases": repository.count_remaining_leases(), "queued_remaining": repository.count_queued()}
                repository.update_lease_snapshot(window_id=window_id, remaining=detail["remaining_leases"], checked_at=now)
            if target == "switched":
                remaining = repository.count_remaining_leases()
                if remaining > 0:
                    raise ConflictError("仍有未结束的租约，无法完成切换", context={"remaining_leases": remaining})
                detail = {"remaining_leases": 0, "queued_remaining": repository.count_queued()}
                repository.update_lease_snapshot(window_id=window_id, remaining=0, checked_at=now)
            closed_at = now if target in {"completed", "aborted"} else None
            new_version = int(window["version"]) + 1
            repository.update_window_phase(window_id=window_id, phase=target, version=new_version, now=now, closed_at=closed_at)
            transition = repository.add_transition(
                window_id=window_id, key=payload["idempotency_key"], actor=payload["actor"], reason=payload["reason"],
                from_phase=current, to_phase=target, version=new_version, detail=detail, now=now,
            )
            return {"window": window_view(repository.window_by_id(window_id)), "transition": transition_view(transition), "applied": True, "idempotent_replay": False}

    def recover(self, window_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """异常恢复：回收过期租约（重新排队但不暴露给调度员），并把窗口校正到与实际一致的阶段。"""
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = MaintenanceWindowRepository(connection)
            self._sweep_expired(repository, now)
            window = repository.window_by_id(window_id)
            if window is None:
                raise NotFoundError("维护窗口不存在")
            recorded = repository.transition_by_key(window_id, payload["idempotency_key"])
            if recorded is not None:
                return {"window": window_view(repository.window_by_id(window_id)), "transition": transition_view(recorded), "applied": False, "idempotent_replay": True, "recovery": transition_view(recorded)["detail"]}
            current = window["phase"]
            if current in TERMINAL_PHASES:
                raise ConflictError("维护窗口已关闭，无需恢复")
            recovered_ids = self._requeue_expired_leases(connection, now, payload["actor"])
            remaining = repository.count_remaining_leases()
            queued = repository.count_queued()
            target = current
            if current in {"lease_check", "switched"} and remaining > 0:
                target = "claim_paused"
            detail = {"recovered_task_ids": recovered_ids, "remaining_leases": remaining, "queued_remaining": queued, "adjusted": target != current}
            repository.update_lease_snapshot(window_id=window_id, remaining=remaining, checked_at=now)
            new_version = int(window["version"]) + 1
            repository.update_window_phase(window_id=window_id, phase=target, version=new_version, now=now)
            transition = repository.add_transition(
                window_id=window_id, key=payload["idempotency_key"], actor=payload["actor"], reason=payload["reason"],
                from_phase=current, to_phase=target, version=new_version, detail=detail, now=now,
            )
            return {"window": window_view(repository.window_by_id(window_id)), "transition": transition_view(transition), "applied": True, "idempotent_replay": False, "recovery": detail}

    def current(self) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = MaintenanceWindowRepository(connection)
            self._sweep_expired(repository, now)
            window = repository.active_window()
            remaining = repository.count_remaining_leases()
            queued = repository.count_queued()
            view = window_view(window) if window is not None else None
        accepting_submissions = view is None or view["phase"] not in SUBMISSION_GATED_PHASES
        accepting_claims = view is None or view["phase"] not in CLAIM_GATED_PHASES
        return {"window": view, "accepting_submissions": accepting_submissions, "accepting_claims": accepting_claims, "remaining_leases": remaining, "queued_remaining": queued}

    def get_window(self, window_id: int) -> dict[str, Any]:
        window = self.repository.window_by_id(window_id)
        if window is None:
            raise NotFoundError("维护窗口不存在")
        return {
            "window": window_view(window),
            "transitions": [transition_view(row) for row in self.repository.transitions(window_id)],
            "exemptions": [dict(row) for row in self.repository.grants(window_id)],
        }

    def list_windows(self, *, phase: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return [window_view(row) for row in self.repository.list_windows(phase, max(1, min(limit, 500)))]

    def exemptions(self, window_id: int) -> list[dict[str, Any]]:
        if self.repository.window_by_id(window_id) is None:
            raise NotFoundError("维护窗口不存在")
        return [dict(row) for row in self.repository.grants(window_id)]

    def submission_gate(self, connection: sqlite3.Connection, payload: dict[str, Any]) -> ExemptionDecision | None:
        """在服务单提交事务内调用：窗口生效时拒绝非豁免订单，豁免订单返回豁免决定。"""
        repository = MaintenanceWindowRepository(connection)
        self._sweep_expired(repository, to_storage(self.clock.now()))
        window = repository.active_window()
        if window is None or window["phase"] not in SUBMISSION_GATED_PHASES:
            return None
        decision = self._exemption_for(window, payload)
        if decision is not None:
            return decision
        raise MaintenanceWindowError("维护窗口期间暂停接收新的服务单", context={"window_code": window["code"], "phase": window["phase"]})

    def record_exemption(self, connection: sqlite3.Connection, decision: ExemptionDecision, *, task_id: int, actor: str, reason: str) -> None:
        MaintenanceWindowRepository(connection).add_grant(
            window_id=decision.window_id, task_id=task_id, rule_code=decision.rule_code,
            allow_submit=True, allow_claim=decision.allow_claim,
            actor=actor, reason=reason, now=to_storage(self.clock.now()),
        )

    def claim_gate(self, connection: sqlite3.Connection) -> ClaimGate | None:
        """在领取事务内调用：暂停领取阶段只允许领取获得豁免的紧急服务单。"""
        repository = MaintenanceWindowRepository(connection)
        self._sweep_expired(repository, to_storage(self.clock.now()))
        window = repository.active_window()
        if window is None or window["phase"] not in CLAIM_GATED_PHASES:
            return None
        return ClaimGate(
            window_id=window["id"], window_code=window["code"], phase=window["phase"],
            exempt_task_ids=tuple(repository.claim_exempt_task_ids(window["id"])),
        )

    @staticmethod
    def _exemption_for(window: sqlite3.Row, payload: dict[str, Any]) -> ExemptionDecision | None:
        rules = json.loads(window["exemption_rules_json"] or "{}")
        rule = rules.get("emergency_funeral") or {}
        if not rule.get("submit"):
            return None
        if not payload.get("emergency"):
            return None
        if payload.get("service_category") != EMERGENCY_FUNERAL_CATEGORY:
            return None
        return ExemptionDecision(window_id=window["id"], window_code=window["code"], rule_code="emergency_funeral", allow_claim=bool(rule.get("claim")))

    @staticmethod
    def _validate_rules(rules: dict[str, Any]) -> None:
        unknown = set(rules) - KNOWN_EXEMPTION_RULES
        if unknown:
            raise ValidationError("未知的豁免规则", context={"rules": sorted(unknown)})
        for rule in rules.values():
            if not isinstance(rule, dict):
                raise ValidationError("豁免规则必须是对象")
            if rule.get("claim") and not rule.get("submit"):
                raise ValidationError("豁免领取必须同时豁免提交")

    @staticmethod
    def _sweep_expired(repository: MaintenanceWindowRepository, now: str) -> None:
        """把计划结束时间已过的活跃窗口落为 expired；幂等键保证清扫只生效一次。"""
        for window in repository.expired_active_windows(now):
            new_version = int(window["version"]) + 1
            repository.update_window_phase(window_id=window["id"], phase="expired", version=new_version, now=now, closed_at=now)
            repository.add_transition(
                window_id=window["id"], key=f"auto-expire:{window['id']}", actor="system",
                reason="窗口计划结束时间已过，自动过期", from_phase=window["phase"], to_phase="expired",
                version=new_version, detail={}, now=now,
            )

    @staticmethod
    def _requeue_expired_leases(connection: sqlite3.Connection, now: str, actor: str) -> list[int]:
        from app.compute.repository import ComputeRepository  # 延迟导入避免包初始化循环

        compute_repository = ComputeRepository(connection)
        rows = connection.execute("SELECT * FROM compute_tasks WHERE status='running' AND lease_expires_at<>'' AND lease_expires_at<? ORDER BY id", (now,)).fetchall()
        recovered: list[int] = []
        for task in rows:
            before = dict(task)
            if int(task["attempt_count"]) < int(task["max_attempts"]):
                status, finished_at = "queued", None
            else:
                status, finished_at = "failed", now
            connection.execute(
                "UPDATE compute_tasks SET status=?,lease_owner='',lease_expires_at='',available_at=?,last_error_code='lease_expired',last_error_message='维护窗口恢复：工作者租约已过期',finished_at=?,updated_at=?,version=version+1 WHERE id=?",
                (status, now, finished_at, now, task["id"]),
            )
            after = dict(compute_repository.task_by_id(task["id"]))
            compute_repository.add_intervention(task_id=task["id"], actor=actor, action="lease_recovery", reason="维护窗口恢复：租约过期自动回收", before=before, after=after, batch_key="", now=now)
            recovered.append(int(task["id"]))
        return recovered
