from __future__ import annotations

import json
import sqlite3
from typing import Any

ACTIVE_PHASES = ("planned", "draining", "claim_paused", "lease_check", "switched")
TERMINAL_PHASES = ("completed", "aborted", "expired")


class MaintenanceWindowRepository:
    """维护窗口、阶段转换审计与豁免记录的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def window_by_id(self, window_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM maintenance_windows WHERE id=?", (window_id,)).fetchone()

    def window_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM maintenance_windows WHERE code=?", (code,)).fetchone()

    def active_window(self) -> sqlite3.Row | None:
        placeholders = ",".join("?" for _ in ACTIVE_PHASES)
        return self.connection.execute(
            f"SELECT * FROM maintenance_windows WHERE phase IN ({placeholders}) ORDER BY id DESC LIMIT 1",
            ACTIVE_PHASES,
        ).fetchone()

    def expired_active_windows(self, now: str) -> list[sqlite3.Row]:
        placeholders = ",".join("?" for _ in ACTIVE_PHASES)
        return self.connection.execute(
            f"SELECT * FROM maintenance_windows WHERE phase IN ({placeholders}) AND planned_end_at<? ORDER BY id",
            (*ACTIVE_PHASES, now),
        ).fetchall()

    def list_windows(self, phase: str | None, limit: int) -> list[sqlite3.Row]:
        if phase:
            return self.connection.execute("SELECT * FROM maintenance_windows WHERE phase=? ORDER BY id DESC LIMIT ?", (phase, limit)).fetchall()
        return self.connection.execute("SELECT * FROM maintenance_windows ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def insert_window(self, *, code: str, title: str, planned_start_at: str, planned_end_at: str, exemption_rules: dict[str, Any], created_by: str, now: str) -> sqlite3.Row:
        cursor = self.connection.execute(
            "INSERT INTO maintenance_windows(code,title,phase,version,planned_start_at,planned_end_at,exemption_rules_json,created_by,created_at,updated_at) VALUES(?,?,'planned',1,?,?,?,?,?,?)",
            (code, title, planned_start_at, planned_end_at, json.dumps(exemption_rules, ensure_ascii=False, sort_keys=True), created_by, now, now),
        )
        return self.window_by_id(cursor.lastrowid)

    def update_window_phase(self, *, window_id: int, phase: str, version: int, now: str, closed_at: str | None = None) -> None:
        self.connection.execute(
            "UPDATE maintenance_windows SET phase=?,version=?,updated_at=?,closed_at=COALESCE(?,closed_at) WHERE id=?",
            (phase, version, now, closed_at, window_id),
        )

    def update_lease_snapshot(self, *, window_id: int, remaining: int, checked_at: str) -> None:
        self.connection.execute(
            "UPDATE maintenance_windows SET remaining_lease_snapshot=?,lease_checked_at=? WHERE id=?",
            (remaining, checked_at, window_id),
        )

    def transition_by_key(self, window_id: int, key: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM maintenance_window_transitions WHERE window_id=? AND idempotency_key=?", (window_id, key)).fetchone()

    def transitions(self, window_id: int) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM maintenance_window_transitions WHERE window_id=? ORDER BY id", (window_id,)).fetchall()

    def add_transition(self, *, window_id: int, key: str, actor: str, reason: str, from_phase: str, to_phase: str, version: int, detail: dict[str, Any], now: str) -> sqlite3.Row:
        cursor = self.connection.execute(
            "INSERT INTO maintenance_window_transitions(window_id,idempotency_key,actor,reason,from_phase,to_phase,window_version,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (window_id, key, actor, reason, from_phase, to_phase, version, json.dumps(detail, ensure_ascii=False, sort_keys=True), now),
        )
        return self.connection.execute("SELECT * FROM maintenance_window_transitions WHERE id=?", (cursor.lastrowid,)).fetchone()

    def add_grant(self, *, window_id: int, task_id: int, rule_code: str, allow_submit: bool, allow_claim: bool, actor: str, reason: str, now: str) -> None:
        self.connection.execute(
            "INSERT INTO maintenance_exemption_grants(window_id,task_id,rule_code,allow_submit,allow_claim,actor,reason,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (window_id, task_id, rule_code, int(allow_submit), int(allow_claim), actor, reason, now),
        )

    def grants(self, window_id: int) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM maintenance_exemption_grants WHERE window_id=? ORDER BY id", (window_id,)).fetchall()

    def claim_exempt_task_ids(self, window_id: int) -> list[int]:
        rows = self.connection.execute("SELECT task_id FROM maintenance_exemption_grants WHERE window_id=? AND allow_claim=1", (window_id,)).fetchall()
        return [int(row["task_id"]) for row in rows]

    def count_remaining_leases(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM compute_tasks WHERE status IN ('running','cancel_requested')").fetchone()[0])

    def count_queued(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM compute_tasks WHERE status='queued'").fetchone()[0])
