from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.clock import FrozenClock, clock_registry
from app.database import close_connection

BASE_TIME = datetime(2026, 10, 3, 0, 0, tzinfo=UTC)

TEMPLATE = {
    "code": "solver-a",
    "name": "方程求解模板",
    "algorithm": "solver-a",
    "parameter_schema": {
        "iterations": {"type": "integer", "required": True, "minimum": 1, "maximum": 10000},
        "mode": {"type": "string", "required": True, "choices": ["fast", "accurate"]},
    },
    "default_parameters": {},
    "max_runtime_seconds": 300,
    "max_attempts": 2,
}


def submit_payload(key: str, *, user: str = "researcher-1", priority: int = 50) -> dict:
    return {
        "template_code": "solver-a",
        "project_code": "project-a",
        "requested_by": user,
        "parameters": {"iterations": 100, "mode": "accurate"},
        "priority": priority,
        "idempotency_key": key,
    }


def exemption_payload(key: str, **overrides) -> dict:
    payload = {
        "template_code": "solver-a",
        "project_code": "funeral-urgent",
        "requested_by": "dispatcher-1",
        "parameters": {"iterations": 100, "mode": "accurate"},
        "priority": 95,
        "idempotency_key": key,
        "case_category": "funeral",
        "urgency_level": "immediate",
        "next_of_kin_contact": "13800000000",
        "service_address": "幸福村 12 号",
        "approver": "值班主管王姐",
        "approval_code": "EMG-20261003-01",
        "reason": "家属要求立即到场布置灵堂",
    }
    payload.update(overrides)
    return payload


@pytest.fixture()
def frozen_env(tmp_path: Path):
    os.environ["TOWNSHIP_DATABASE_PATH"] = str(tmp_path / "maintenance.db")
    close_connection()
    clock = FrozenClock(BASE_TIME)
    clock_registry.override(clock)
    from app.main import app

    with TestClient(app) as client:
        yield client, clock
    clock_registry.reset()
    close_connection()


@pytest.fixture()
def admin_headers(frozen_env) -> tuple[TestClient, FrozenClock, dict]:
    client, clock = frozen_env
    response = client.post("/api/auth/bootstrap", json={"username": "admin", "password": "Admin!23456", "client_label": "tests"})
    assert response.status_code == 201, response.text
    login = client.post("/api/auth/login", json={"username": "admin", "password": "Admin!23456", "client_label": "tests"})
    assert login.status_code == 200, login.text
    return client, clock, {"Authorization": f"Bearer {login.json()['token']}"}


def create_template(client: TestClient) -> None:
    response = client.post("/api/compute/templates?actor=administrator", json=TEMPLATE)
    assert response.status_code == 201, response.text


def transition(client, headers, window_id, phase, *, reason="节前档案维护", expected_version=None):
    body = {"actor": "运营主管老李", "reason": reason}
    if expected_version is not None:
        body["expected_version"] = expected_version
    return client.post(f"/api/maintenance/windows/{window_id}/{phase}", json=body, headers=headers)


# --------------------------------------------------------------------- flows


def test_full_window_flow_rejects_new_orders_drains_and_restores(admin_headers):
    client, clock, headers = admin_headers
    create_template(client)
    task_a = client.post("/api/compute/tasks", json=submit_payload("order-000001")).json()
    task_b = client.post("/api/compute/tasks", json=submit_payload("order-000002")).json()

    plan = client.post(
        "/api/maintenance/windows/plan",
        json={"reason": "礼仪人员与车辆档案节前维护", "drain_deadline": "2026-10-03T01:00:00+00:00"},
        headers=headers,
    )
    assert plan.status_code == 201, plan.text
    window = plan.json()
    assert window["status"] == "planned" and window["version"] == 1
    window_id = window["id"]

    # planned 不影响接单；进入排空后才拒绝新单
    planned_order = client.post("/api/compute/tasks", json=submit_payload("order-planned")).json()
    assert planned_order["status"] == "queued"

    drained = transition(client, headers, window_id, "drain")
    assert drained.status_code == 200 and drained.json()["status"] == "draining"
    rejected = client.post("/api/compute/tasks", json=submit_payload("order-blocked"))
    assert rejected.status_code == 409
    assert rejected.json()["error"]["context"]["window_id"] == window_id

    # 排空阶段仍允许领取旧单，以便安全结束
    claimed = client.post("/api/compute/tasks/claim", json={"worker_id": "w1", "capabilities": ["solver-a"], "lease_seconds": 600})
    assert claimed.status_code == 200 and claimed.json()["task"]["id"] == task_a["id"]

    paused = transition(client, headers, window_id, "pause-claims")
    assert paused.status_code == 200 and paused.json()["status"] == "claim_paused"
    # 暂停领取后普通调度员领不到剩余的 queued 旧单
    assert client.post("/api/compute/tasks/claim", json={"worker_id": "w2", "capabilities": ["solver-a"], "lease_seconds": 600}).json()["task"] is None

    leases = client.get(f"/api/maintenance/windows/{window_id}/leases?reason=核对剩余租约", headers=headers)
    assert leases.status_code == 200
    body = leases.json()
    assert body["remaining"]["queued"] >= 1
    assert body["remaining"]["running_active"] == 1
    assert body["remaining"]["ready_to_switch"] is False
    assert {item["id"] for item in body["active_leases"]} == {task_a["id"]}

    # 尚有 queued 旧单时切换被拒绝
    blocked_switch = transition(client, headers, window_id, "switch")
    assert blocked_switch.status_code == 409
    assert blocked_switch.json()["error"]["context"]["ready_to_switch"] is False

    # 旧单收敛：运行中的正常完结，排队的人工取消
    completed = client.post(
        f"/api/compute/tasks/{task_a['id']}/complete",
        json={"worker_id": "w1", "result": {"value": 1}, "metrics": {}},
    )
    assert completed.status_code == 200 and completed.json()["status"] == "succeeded"
    cancelled = client.post(
        f"/api/compute/tasks/{task_b['id']}/cancel",
        json={"actor": "运营主管老李", "reason": "节前维护窗口排空取消排队单"},
    )
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    cancelled_planned = client.post(
        f"/api/compute/tasks/{planned_order['id']}/cancel",
        json={"actor": "运营主管老李", "reason": "节前维护窗口排空取消排队单"},
    )
    assert cancelled_planned.status_code == 200 and cancelled_planned.json()["status"] == "cancelled"

    switched = transition(client, headers, window_id, "switch")
    assert switched.status_code == 200 and switched.json()["status"] == "switched"
    done = transition(client, headers, window_id, "complete", reason="档案维护结束恢复接单")
    assert done.status_code == 200 and done.json()["status"] == "completed"

    # 恢复后新单可提交、领取恢复
    assert client.post("/api/compute/tasks", json=submit_payload("order-restored")).status_code == 202

    # 每个阶段变化都有操作者与原因
    events = client.get(f"/api/maintenance/windows/{window_id}/events", headers=headers).json()["items"]
    phase_events = [e for e in events if e["event_type"] in {"planned", "enter_draining", "pause_claims", "complete_switch", "complete"}]
    assert [e["phase"] for e in phase_events] == ["planned", "draining", "claim_paused", "switched", "completed"]
    assert all(e["actor"] for e in phase_events) and all(e["reason"] for e in phase_events)

    audit = client.get("/api/audit?resource_type=maintenance_window&size=50", headers=headers).json()["data"]
    actions = {event["action"] for event in audit}
    assert {"maintenance.window.plan", "maintenance.window.enter_draining", "maintenance.window.pause_claims",
            "maintenance.window.complete_switch", "maintenance.window.complete"} <= actions


def test_phase_transitions_are_idempotent_and_version_checked(admin_headers):
    client, clock, headers = admin_headers
    create_template(client)
    window_id = client.post(
        "/api/maintenance/windows/plan",
        json={"reason": "幂等性验证", "drain_deadline": "2026-10-03T02:00:00+00:00"},
        headers=headers,
    ).json()["id"]

    first = transition(client, headers, window_id, "drain")
    assert first.status_code == 200 and first.json()["version"] == 2
    # 重复执行同一阶段：幂等返回，版本不变、不重复落事件
    second = transition(client, headers, window_id, "drain")
    assert second.status_code == 200
    assert second.json()["version"] == 2 and second.json()["changed"] is False
    events = client.get(f"/api/maintenance/windows/{window_id}/events", headers=headers).json()["items"]
    assert [e["event_type"] for e in events].count("enter_draining") == 1

    # 乐观版本校验：过期版本推进被拒绝
    stale = transition(client, headers, window_id, "pause-claims", expected_version=1)
    assert stale.status_code == 409 and stale.json()["error"]["context"]["current"] == 2

    # 不能跨阶段跳转
    jump = transition(client, headers, window_id, "switch")
    assert jump.status_code == 409


def test_expired_planned_window_does_not_block_next_plan(admin_headers):
    client, clock, headers = admin_headers
    stale = client.post(
        "/api/maintenance/windows/plan",
        json={"reason": "原定窗口", "drain_deadline": "2026-10-03T01:00:00+00:00"},
        headers=headers,
    ).json()
    clock.advance(hours=2)
    # 直接尝试排空过期窗口被拒绝
    assert transition(client, headers, stale["id"], "drain").status_code == 409

    fresh = client.post(
        "/api/maintenance/windows/plan",
        json={"reason": "重新排期的窗口", "drain_deadline": "2026-10-03T05:00:00+00:00"},
        headers=headers,
    )
    assert fresh.status_code == 201, fresh.text
    new_window = fresh.json()
    assert new_window["id"] != stale["id"]

    old = client.get(f"/api/maintenance/windows/{stale['id']}", headers=headers).json()
    assert old["status"] == "expired" and old["gate_active"] is False
    old_events = client.get(f"/api/maintenance/windows/{stale['id']}/events", headers=headers).json()["items"]
    assert old_events[-1]["event_type"] == "expire" and old_events[-1]["actor"] == "system"

    drained = transition(client, headers, new_window["id"], "drain")
    assert drained.status_code == 200 and drained.json()["status"] == "draining"


def test_restart_resumes_draining_phase_without_re_exposing_orders(admin_headers):
    client, clock, headers = admin_headers
    create_template(client)
    client.post("/api/compute/tasks", json=submit_payload("order-before-restart"))
    window_id = client.post(
        "/api/maintenance/windows/plan",
        json={"reason": "跨重启验证", "drain_deadline": "2026-10-03T03:00:00+00:00"},
        headers=headers,
    ).json()["id"]
    transition(client, headers, window_id, "drain")
    transition(client, headers, window_id, "pause-claims")

    # 模拟服务重启：同一数据库重新执行启动生命周期
    close_connection()
    from app.main import app

    with TestClient(app) as restarted:
        # 重启不会把未完成服务重新暴露：门禁仍生效
        assert restarted.post("/api/compute/tasks", json=submit_payload("order-after-restart")).status_code == 409
        assert restarted.post(
            "/api/compute/tasks/claim", json={"worker_id": "w9", "capabilities": ["solver-a"], "lease_seconds": 60}
        ).json()["task"] is None
        recovery = restarted.post("/api/maintenance/windows/recover", headers=headers)
        assert recovery.status_code == 200
        assert recovery.json() == {"resumed_window_id": window_id, "phase": "claim_paused", "gate_active": True}


def test_restart_after_completion_leaves_service_available(admin_headers):
    client, clock, headers = admin_headers
    create_template(client)
    window_id = client.post(
        "/api/maintenance/windows/plan", json={"reason": "完成后重启验证"}, headers=headers
    ).json()["id"]
    transition(client, headers, window_id, "drain")
    transition(client, headers, window_id, "pause-claims")
    transition(client, headers, window_id, "switch")
    transition(client, headers, window_id, "complete")

    close_connection()
    from app.main import app

    with TestClient(app) as restarted:
        recovery = restarted.post("/api/maintenance/windows/recover", headers=headers).json()
        assert recovery["gate_active"] is False and recovery["phase"] == "completed"
        assert restarted.post("/api/compute/tasks", json=submit_payload("order-new-day")).status_code == 202


def test_emergency_funeral_exemption_lane_and_audit(admin_headers):
    client, clock, headers = admin_headers
    create_template(client)
    normal = client.post("/api/compute/tasks", json=submit_payload("normal-000001")).json()
    window_id = client.post(
        "/api/maintenance/windows/plan", json={"reason": "豁免验证", "drain_deadline": "2026-10-03T02:00:00+00:00"}, headers=headers
    ).json()["id"]
    transition(client, headers, window_id, "drain")

    # 不合规申请（婚庆单）被拒并留痕
    wedding = client.post("/api/maintenance/windows/emergency-exemption", json=exemption_payload("emg-reject-wedding", case_category="wedding"))
    assert wedding.status_code == 409
    assert any("R1" in item for item in wedding.json()["error"]["context"]["failures"])

    # 缺授权码同样被拒
    no_code = client.post(
        "/api/maintenance/windows/emergency-exemption",
        json=exemption_payload("emg-reject-code", approval_code=""),
    )
    assert no_code.status_code == 422

    # 合规紧急白事放行
    accepted = client.post("/api/maintenance/windows/emergency-exemption", json=exemption_payload("emg-accept-0001"))
    assert accepted.status_code == 201, accepted.text
    accepted_body = accepted.json()
    exempt_task_id = accepted_body["task"]["id"]
    assert accepted_body["task"]["emergency_exempt"] == 1
    assert accepted_body["exemption"]["rule_code"] == "FUNERAL_EMERGENCY_APPROVED"

    # 幂等重放返回同一豁免单
    replay = client.post("/api/maintenance/windows/emergency-exemption", json=exemption_payload("emg-accept-0001"))
    assert replay.status_code == 201 and replay.json()["task"]["id"] == exempt_task_id
    assert replay.json()["idempotent_replay"] is True

    # 授权码不可在同一窗口复用
    duplicate_code = client.post("/api/maintenance/windows/emergency-exemption", json=exemption_payload("emg-accept-0002"))
    assert duplicate_code.status_code == 409

    transition(client, headers, window_id, "pause-claims")
    # 暂停领取期间调度员只能领到豁免单，普通单被隐藏
    claimed = client.post("/api/compute/tasks/claim", json={"worker_id": "emergency-team", "capabilities": ["solver-a"], "lease_seconds": 900})
    assert claimed.json()["task"]["id"] == exempt_task_id
    assert claimed.json()["task"]["id"] != normal["id"]

    exemptions = client.get(f"/api/maintenance/windows/exemptions/list?window_id={window_id}", headers=headers).json()["items"]
    outcomes = {item["outcome"] for item in exemptions}
    assert outcomes == {"accepted", "rejected"}
    accepted_rows = [item for item in exemptions if item["outcome"] == "accepted"]
    assert len(accepted_rows) == 1
    assert accepted_rows[0]["approver"] == "值班主管王姐"

    events = client.get(f"/api/maintenance/windows/{window_id}/events", headers=headers).json()["items"]
    assert any(e["event_type"] == "exemption_accepted" for e in events)
    assert any(e["event_type"] == "exemption_rejected" for e in events)

    audit = client.get("/api/audit?resource_type=maintenance_window&size=50", headers=headers).json()["data"]
    assert any(e["action"] == "maintenance.exemption.accepted" for e in audit)
    assert any(e["action"] == "maintenance.exemption.rejected" and e["outcome"] == "failure" for e in audit)


def test_exemption_requires_active_window(admin_headers):
    client, clock, headers = admin_headers
    create_template(client)
    response = client.post("/api/maintenance/windows/emergency-exemption", json=exemption_payload("emg-no-window"))
    assert response.status_code == 409


def test_abort_recovers_service_and_is_idempotent(admin_headers):
    client, clock, headers = admin_headers
    create_template(client)
    window_id = client.post(
        "/api/maintenance/windows/plan", json={"reason": "异常恢复验证"}, headers=headers
    ).json()["id"]
    transition(client, headers, window_id, "drain")
    client.post("/api/compute/tasks", json=submit_payload("would-block"))
    # 排空期间发现维护脚本异常，终止窗口
    aborted = transition(client, headers, window_id, "abort", reason="备份校验失败，终止本次维护")
    assert aborted.status_code == 200 and aborted.json()["status"] == "aborted"
    assert client.post("/api/compute/tasks", json=submit_payload("order-after-abort")).status_code == 202
    # 重复终止幂等
    again = transition(client, headers, window_id, "abort", reason="重复点击")
    assert again.status_code == 200 and again.json()["changed"] is False
    # 终态后不能再推进
    assert transition(client, headers, window_id, "complete").status_code == 409


def test_reconcile_expired_leases_then_drain_converges(admin_headers):
    client, clock, headers = admin_headers
    create_template(client)
    task = client.post("/api/compute/tasks", json=submit_payload("lease-000001")).json()
    window_id = client.post(
        "/api/maintenance/windows/plan", json={"reason": "租约处置验证"}, headers=headers
    ).json()["id"]
    transition(client, headers, window_id, "drain")
    claimed = client.post("/api/compute/tasks/claim", json={"worker_id": "w1", "capabilities": ["solver-a"], "lease_seconds": 10})
    assert claimed.json()["task"]["id"] == task["id"]
    transition(client, headers, window_id, "pause-claims")
    clock.advance(seconds=11)

    leases = client.get(f"/api/maintenance/windows/{window_id}/leases", headers=headers).json()
    assert leases["remaining"]["running_lease_expired"] == 1
    assert leases["remaining"]["ready_to_switch"] is False

    reconcile = client.post(
        f"/api/maintenance/windows/{window_id}/reconcile-leases",
        json={"actor": "运营主管老李", "reason": "工作者失联，回收过期租约"},
        headers=headers,
    )
    assert reconcile.status_code == 200
    assert reconcile.json()["recovered"]["recovered"] == [task["id"]]
    # 回收后任务回到排队，取消即可收敛
    client.post(
        f"/api/compute/tasks/{task['id']}/cancel",
        json={"actor": "运营主管老李", "reason": "维护窗口排空取消"},
    )
    assert transition(client, headers, window_id, "switch").status_code == 200
    assert transition(client, headers, window_id, "complete").status_code == 200
