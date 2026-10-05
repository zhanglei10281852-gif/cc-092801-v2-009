from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.core.clock import FrozenClock, set_clock_override
from app.main import app

TEMPLATE = {
    "code": "ceremony-service",
    "name": "礼仪服务单",
    "algorithm": "ceremony",
    "parameter_schema": {"venue": {"type": "string", "required": True}},
    "default_parameters": {},
    "max_runtime_seconds": 300,
    "max_attempts": 2,
}


@pytest.fixture()
def clock(client: TestClient):
    frozen = FrozenClock(datetime(2026, 10, 4, 8, 0, tzinfo=UTC))
    set_clock_override(frozen)
    yield frozen
    set_clock_override(None)


def create_template(client: TestClient) -> None:
    response = client.post("/api/compute/templates?actor=operator", json=TEMPLATE)
    assert response.status_code == 201, response.text


def submit_order(key: str, *, category: str = "其他", emergency: bool = False, reason: str | None = None, priority: int = 50, user: str = "dispatcher-1") -> dict:
    return {
        "template_code": "ceremony-service",
        "project_code": "festival-maintenance",
        "requested_by": user,
        "parameters": {"venue": "礼堂"},
        "priority": priority,
        "idempotency_key": key,
        "service_category": category,
        "emergency": emergency,
        "emergency_reason": reason,
    }


def plan_window(client: TestClient, *, key: str = "plan-000001", code: str = "window-spring-festival", start: str = "2026-10-04T10:00:00+00:00", end: str = "2026-10-04T12:00:00+00:00", rules: dict | None = None):
    payload = {
        "code": code,
        "title": "节前档案维护",
        "planned_start_at": start,
        "planned_end_at": end,
        "exemption_rules": rules or {},
        "actor": "ops-lead",
        "reason": "节前礼仪人员与车辆档案维护",
        "idempotency_key": key,
    }
    return client.post("/api/maintenance-windows", json=payload)


def transition(client: TestClient, window_id: int, target: str, key: str, *, expected_version: int | None = None):
    payload = {"target_phase": target, "actor": "ops-lead", "reason": f"推进到 {target}", "idempotency_key": key}
    if expected_version is not None:
        payload["expected_version"] = expected_version
    return client.post(f"/api/maintenance-windows/{window_id}/transitions", json=payload)


def claim(client: TestClient, worker: str):
    return client.post("/api/compute/tasks/claim", json={"worker_id": worker, "capabilities": ["ceremony"], "lease_seconds": 300})


def test_window_drains_gates_and_restores_availability(client: TestClient, clock: FrozenClock):
    create_template(client)
    planned = plan_window(client, rules={"emergency_funeral": {"submit": True, "claim": True}})
    assert planned.status_code == 201, planned.text
    window = planned.json()["window"]
    assert window["phase"] == "planned" and window["version"] == 1
    wid = window["id"]

    # 计划阶段不影响接单
    normal = client.post("/api/compute/tasks", json=submit_order("order-normal-1"))
    assert normal.status_code == 202

    # 进入排空：普通新单被拒绝，紧急白事按豁免规则放行
    drained = transition(client, wid, "draining", "drain-000001")
    assert drained.status_code == 200 and drained.json()["window"]["phase"] == "draining"
    rejected = client.post("/api/compute/tasks", json=submit_order("order-normal-2"))
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "maintenance_window"
    exempt = client.post("/api/compute/tasks", json=submit_order("order-emergency-1", category="白事", emergency=True, reason="突发白事需立即出车", priority=90))
    assert exempt.status_code == 202, exempt.text

    # 排空阶段仍可领取：紧急单优先被领取并安全结束
    first_claim = claim(client, "crew-1")
    assert first_claim.json()["task"]["id"] == exempt.json()["id"]
    done = client.post(f"/api/compute/tasks/{exempt.json()['id']}/complete", json={"worker_id": "crew-1", "result": {"done": True}, "metrics": {}})
    assert done.status_code == 200

    # 暂停领取：普通排队单对调度员隐藏，紧急白事仍可按豁免领取
    paused = transition(client, wid, "claim_paused", "pause-000001")
    assert paused.status_code == 200
    blocked = claim(client, "crew-2")
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "maintenance_window"
    exempt2 = client.post("/api/compute/tasks", json=submit_order("order-emergency-2", category="白事", emergency=True, reason="深夜突发白事", priority=95))
    assert exempt2.status_code == 202
    exempt_claim = claim(client, "crew-2")
    assert exempt_claim.status_code == 200 and exempt_claim.json()["task"]["id"] == exempt2.json()["id"]
    client.post(f"/api/compute/tasks/{exempt2.json()['id']}/complete", json={"worker_id": "crew-2", "result": {"done": True}, "metrics": {}})

    # 检查剩余租约：旧单已收敛（无在执租约，仅余排队单）
    checked = transition(client, wid, "lease_check", "check-000001")
    assert checked.json()["transition"]["detail"]["remaining_leases"] == 0
    assert checked.json()["transition"]["detail"]["queued_remaining"] == 1

    # 完成切换后仍不可领取，直到维护完成
    switched = transition(client, wid, "switched", "switch-000001")
    assert switched.json()["window"]["phase"] == "switched"
    assert claim(client, "crew-3").status_code == 409
    closed = transition(client, wid, "completed", "close-000001")
    assert closed.json()["window"]["phase"] == "completed"

    # 恢复后的可用状态：新单可提交，遗留排队单重新可领取
    resumed = client.post("/api/compute/tasks", json=submit_order("order-normal-3"))
    assert resumed.status_code == 202
    after = claim(client, "crew-3")
    assert after.status_code == 200 and after.json()["task"] is not None

    # 所有阶段变化都记录了操作者与原因
    detail = client.get(f"/api/maintenance-windows/{wid}").json()
    phases = [(item["from_phase"], item["to_phase"]) for item in detail["transitions"]]
    assert phases == [("", "planned"), ("planned", "draining"), ("draining", "claim_paused"), ("claim_paused", "lease_check"), ("lease_check", "switched"), ("switched", "completed")]
    assert all(item["actor"] == "ops-lead" and item["reason"] for item in detail["transitions"])

    # 豁免审计：两笔紧急白事，规则、操作者与事由齐全
    exemptions = client.get(f"/api/maintenance-windows/{wid}/exemptions").json()["items"]
    assert len(exemptions) == 2
    assert {item["rule_code"] for item in exemptions} == {"emergency_funeral"}
    assert all(item["actor"] and item["reason"] for item in exemptions)


def test_transitions_are_idempotent_and_versioned(client: TestClient, clock: FrozenClock):
    create_template(client)
    wid = plan_window(client).json()["window"]["id"]
    first = transition(client, wid, "draining", "drain-key-1")
    assert first.status_code == 200 and first.json()["applied"] is True

    # 同一幂等键重放：返回记录结果，不产生新审计、不推进版本
    replay = transition(client, wid, "draining", "drain-key-1")
    assert replay.json()["applied"] is False and replay.json()["idempotent_replay"] is True
    assert replay.json()["window"]["version"] == first.json()["window"]["version"]

    # 同一目标阶段重复执行：幂等空转
    noop = transition(client, wid, "draining", "drain-key-2")
    assert noop.json()["applied"] is False
    detail = client.get(f"/api/maintenance-windows/{wid}").json()
    assert [item["to_phase"] for item in detail["transitions"]] == ["planned", "draining"]

    # 乐观版本守卫
    stale = transition(client, wid, "claim_paused", "pause-key-1", expected_version=99)
    assert stale.status_code == 409
    guarded = transition(client, wid, "claim_paused", "pause-key-1", expected_version=2)
    assert guarded.status_code == 200 and guarded.json()["window"]["version"] == 3

    # 幂等键复用到不同目标、非法跃迁都被拒绝
    assert transition(client, wid, "lease_check", "pause-key-1").status_code == 409
    assert transition(client, wid, "completed", "close-key-1").status_code == 409


def test_restart_preserves_phase_and_recovery_hides_unfinished(client: TestClient, clock: FrozenClock):
    create_template(client)
    wid = plan_window(client).json()["window"]["id"]
    order = client.post("/api/compute/tasks", json=submit_order("order-restart-1"))
    assert order.status_code == 202
    transition(client, wid, "draining", "drain-restart")
    claimed = client.post("/api/compute/tasks/claim", json={"worker_id": "crew-1", "capabilities": ["ceremony"], "lease_seconds": 60})
    task_id = claimed.json()["task"]["id"]
    transition(client, wid, "claim_paused", "pause-restart")
    clock.advance(seconds=120)  # 租约已过期，但服务尚未回收

    # 模拟服务重启：重新进入应用生命周期，窗口状态来自数据库
    with TestClient(app) as restarted:
        current = restarted.get("/api/maintenance-windows/current").json()
        assert current["window"]["phase"] == "claim_paused"
        assert current["accepting_claims"] is False

        # 未完成服务不会重新暴露给调度员
        blocked = restarted.post("/api/compute/tasks/claim", json={"worker_id": "crew-2", "capabilities": ["ceremony"], "lease_seconds": 60})
        assert blocked.status_code == 409

        # 异常恢复：过期租约被回收为排队，但仍被窗口隐藏，阶段保持正确
        recovered = restarted.post(f"/api/maintenance-windows/{wid}/recover", json={"actor": "ops-lead", "reason": "重启后恢复排空阶段", "idempotency_key": "recover-restart"})
        assert recovered.status_code == 200, recovered.text
        body = recovered.json()
        assert body["recovery"]["recovered_task_ids"] == [task_id]
        assert body["window"]["phase"] == "claim_paused"
        still_hidden = restarted.post("/api/compute/tasks/claim", json={"worker_id": "crew-2", "capabilities": ["ceremony"], "lease_seconds": 60})
        assert still_hidden.status_code == 409

        # 恢复幂等：同一键重放不重复回收
        again = restarted.post(f"/api/maintenance-windows/{wid}/recover", json={"actor": "ops-lead", "reason": "重启后恢复排空阶段", "idempotency_key": "recover-restart"})
        assert again.json()["idempotent_replay"] is True

        # 恢复后继续推进：检查、切换、完成，遗留单重新可领取
        checked = restarted.post(f"/api/maintenance-windows/{wid}/transitions", json={"target_phase": "lease_check", "actor": "ops-lead", "reason": "确认剩余租约", "idempotency_key": "check-restart"})
        assert checked.json()["transition"]["detail"]["remaining_leases"] == 0
        restarted.post(f"/api/maintenance-windows/{wid}/transitions", json={"target_phase": "switched", "actor": "ops-lead", "reason": "完成切换", "idempotency_key": "switch-restart"})
        restarted.post(f"/api/maintenance-windows/{wid}/transitions", json={"target_phase": "completed", "actor": "ops-lead", "reason": "维护完成", "idempotency_key": "close-restart"})
        available = restarted.post("/api/compute/tasks/claim", json={"worker_id": "crew-2", "capabilities": ["ceremony"], "lease_seconds": 60})
        assert available.status_code == 200 and available.json()["task"]["id"] == task_id


def test_expired_window_does_not_affect_next_plan(client: TestClient, clock: FrozenClock):
    create_template(client)
    first = plan_window(client, key="plan-a", code="window-a", start="2026-10-04T08:00:00+00:00", end="2026-10-04T09:00:00+00:00")
    assert first.status_code == 201
    wid = first.json()["window"]["id"]
    transition(client, wid, "draining", "drain-a")
    clock.advance(hours=2)  # 10:00，已越过 09:00 的计划结束时间

    # 过期窗口不再拦截新单
    allowed = client.post("/api/compute/tasks", json=submit_order("order-after-expiry"))
    assert allowed.status_code == 202

    # 过期窗口不影响下一次计划，并被自动落为 expired
    second = plan_window(client, key="plan-b", code="window-b", start="2026-10-04T10:30:00+00:00", end="2026-10-04T23:00:00+00:00")
    assert second.status_code == 201, second.text
    detail = client.get(f"/api/maintenance-windows/{wid}").json()
    assert detail["window"]["phase"] == "expired"
    auto = detail["transitions"][-1]
    assert auto["to_phase"] == "expired" and auto["actor"] == "system"


def test_exemption_requires_explicit_rule_and_reason(client: TestClient, clock: FrozenClock):
    create_template(client)
    # 无豁免规则的窗口：紧急白事同样被拒绝
    wid = plan_window(client, key="plan-strict", code="window-strict").json()["window"]["id"]
    transition(client, wid, "draining", "drain-strict")
    denied = client.post("/api/compute/tasks", json=submit_order("order-em-denied", category="白事", emergency=True, reason="突发白事"))
    assert denied.status_code == 409
    transition(client, wid, "aborted", "abort-strict")

    # 有规则但仅限白事：紧急红事不放行；claim=False 时豁免单不可领取
    wid2 = plan_window(client, key="plan-strict-2", code="window-strict-2", rules={"emergency_funeral": {"submit": True, "claim": False}}).json()["window"]["id"]
    transition(client, wid2, "draining", "drain-strict-2")
    red = client.post("/api/compute/tasks", json=submit_order("order-em-red", category="红事", emergency=True, reason="紧急红事"))
    assert red.status_code == 409
    white = client.post("/api/compute/tasks", json=submit_order("order-em-white", category="白事", emergency=True, reason="突发白事"))
    assert white.status_code == 202
    transition(client, wid2, "claim_paused", "pause-strict-2")
    assert claim(client, "crew-9").status_code == 409

    # 紧急订单必须填写紧急事由
    missing_reason = client.post("/api/compute/tasks", json=submit_order("order-em-noreason", category="白事", emergency=True))
    assert missing_reason.status_code == 422


def test_plan_validation_and_single_active_window(client: TestClient, clock: FrozenClock):
    create_template(client)
    bad_range = plan_window(client, key="plan-bad", code="window-bad", start="2026-10-04T12:00:00+00:00", end="2026-10-04T10:00:00+00:00")
    assert bad_range.status_code == 422
    unknown_rule = plan_window(client, key="plan-bad-2", code="window-bad-2", rules={"vip": {"submit": True}})
    assert unknown_rule.status_code == 422

    ok = plan_window(client, key="plan-ok", code="window-ok")
    assert ok.status_code == 201
    replay = plan_window(client, key="plan-ok", code="window-ok")
    assert replay.status_code == 201 and replay.json()["idempotent_replay"] is True

    conflict = plan_window(client, key="plan-other", code="window-other")
    assert conflict.status_code == 409
