from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

CaseCategory = Literal["funeral", "wedding", "other"]
UrgencyLevel = Literal["immediate", "same_day", "scheduled"]

#: 维护窗口期间唯一允许的豁免类型：紧急白事且已经过值班主管授权。
EXEMPTION_RULE_CODE = "FUNERAL_EMERGENCY_APPROVED"


@dataclass(frozen=True, slots=True)
class ExemptionRule:
    code: str
    description: str


#: 明确的豁免规则，全部满足才放行；任何一条不满足都拒绝并留审计。
EXEMPTION_RULES: tuple[ExemptionRule, ...] = (
    ExemptionRule("R1", "事项类别必须为白事（funeral），婚庆与普通业务不享受豁免"),
    ExemptionRule("R2", "紧急程度必须为 immediate（要求维护期间立即到场处置）"),
    ExemptionRule("R3", "必须登记家属联系人 next_of_kin_contact 与服务地址 service_address"),
    ExemptionRule("R4", "必须提供值班主管授权码 approval_code，作为人工批准凭据"),
    ExemptionRule("R5", "豁免单只能提交到处于排空/暂停领取/切换阶段的有效维护窗口"),
)


@dataclass(frozen=True, slots=True)
class ExemptionDecision:
    accepted: bool
    rule_code: str
    matched: list[str]
    failures: list[str]
    evidence: dict[str, Any]

    @property
    def reject_reason(self) -> str:
        return "；".join(self.failures)


def evaluate_exemption(payload: dict[str, Any], *, window_active: bool) -> ExemptionDecision:
    """依据 :data:`EXEMPTION_RULES` 逐条核验紧急白事豁免申请。"""
    matched: list[str] = []
    failures: list[str] = []

    if payload.get("case_category") == "funeral":
        matched.append("R1")
    else:
        failures.append("R1 不满足：仅白事事项可申请紧急豁免")

    if payload.get("urgency_level") == "immediate":
        matched.append("R2")
    else:
        failures.append("R2 不满足：必须为 immediate 级紧急事项")

    contact = str(payload.get("next_of_kin_contact") or "").strip()
    address = str(payload.get("service_address") or "").strip()
    if contact and address:
        matched.append("R3")
    else:
        failures.append("R3 不满足：缺少家属联系人或服务地址")

    approval_code = str(payload.get("approval_code") or "").strip()
    if approval_code:
        matched.append("R4")
    else:
        failures.append("R4 不满足：缺少值班主管授权码")

    if window_active:
        matched.append("R5")
    else:
        failures.append("R5 不满足：当前没有处于排空阶段的维护窗口")

    evidence = {
        "case_category": payload.get("case_category"),
        "urgency_level": payload.get("urgency_level"),
        "next_of_kin_contact": contact,
        "service_address": address,
        "approval_code_present": bool(approval_code),
        "window_active": window_active,
    }
    accepted = not failures
    return ExemptionDecision(
        accepted=accepted,
        rule_code=EXEMPTION_RULE_CODE if accepted else "",
        matched=matched,
        failures=failures,
        evidence=evidence,
    )
