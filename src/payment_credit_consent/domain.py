"""确认流程的领域状态：由不可变事件折叠重建，不直接修改历史。

核心不变量：用户逐项确认绑定"同意纪元" = (报价版本, 当前风险等级)。
报价变化（OFFER_UPDATED）或风险等级变化（新的 SUITABILITY_REVIEWED /
MANUAL_REVIEW_RESOLVED 带来不同等级）都会推进纪元，旧纪元下的逐项
确认自动失效，必须重新确认，不能沿用旧同意。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import events as ev

# 综合确认前必须逐项确认的条目：综合成本、风险提示、合同参与方、撤销窗口
REQUIRED_CONSENT_ITEMS = (
    "comprehensive_cost",
    "risk_warning",
    "contract_parties",
    "revocation_window",
)

ORDER_OPENED_STATUS = "opened"
ORDER_PAID = "paid"

OFFER_SHOWN_STATUS = "shown"
OFFER_ACCEPTED = "accepted"
OFFER_DECLINED = "declined"

SUITABILITY_AUTO_CLEAR = "auto_clear"
SUITABILITY_PENDING_REVIEW = "pending_review"
SUITABILITY_APPROVED = "approved"
SUITABILITY_DECLINED = "declined"

CONTRACT_ACTIVE = "active"
CONTRACT_REVOKED = "revoked"
CONTRACT_SETTLED = "settled"

SCHEDULE_SCHEDULED = "scheduled"
SCHEDULE_CANCELLED = "cancelled"
SCHEDULE_SETTLED = "settled"


def parse_ts(value: str) -> datetime:
    """解析带时区的 ISO 时间；不带时区视为调用方错误。"""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"时间必须包含时区: {value!r}")
    return parsed


@dataclass
class CheckoutState:
    """一笔订单在支付与信贷确认流程中的全部当前状态。"""

    order_id: str
    order_status: str = "unknown"
    customer_id: str | None = None
    currency: str = "CNY"
    items: list[dict[str, Any]] = field(default_factory=list)
    total_minor: int = 0
    discount_minor: int = 0
    payable_minor: int = 0
    merchant_party_id: str | None = None
    platform_party_id: str | None = None
    offer_id: str | None = None
    offer_status: str | None = None
    offer_version: int = 0
    offer_facts: dict[str, Any] = field(default_factory=dict)
    risk_level: str | None = None
    suitability_status: str | None = None
    anomalies: list[str] = field(default_factory=list)
    confirmed_items: dict[str, dict[str, Any]] = field(default_factory=dict)
    contract_id: str | None = None
    contract_status: str | None = None
    contract_facts: dict[str, Any] = field(default_factory=dict)
    schedule: list[dict[str, Any]] = field(default_factory=list)
    statements: dict[int, dict[str, Any]] = field(default_factory=dict)
    revocation_window_until: str | None = None

    @property
    def consent_epoch(self) -> tuple[int, str | None]:
        """当前同意纪元：逐项确认必须在同一纪元内完成才有效。"""
        return (self.offer_version, self.risk_level)

    def missing_items(self) -> list[str]:
        """当前纪元下仍未完成逐项确认的条目。"""
        version, risk = self.consent_epoch
        missing = []
        for item in REQUIRED_CONSENT_ITEMS:
            record = self.confirmed_items.get(item)
            if not record or record.get("offer_version") != version or record.get("risk_level") != risk:
                missing.append(item)
        return missing

    def active_schedule(self) -> list[dict[str, Any]]:
        return [entry for entry in self.schedule if entry["status"] == SCHEDULE_SCHEDULED]

    def apply(self, event: dict[str, Any]) -> None:
        handler = getattr(self, f"_on_{event['event_type'].lower()}", None)
        if handler is not None:
            handler(event)

    # --- 订单与支付 -----------------------------------------------------
    def _on_order_opened(self, event: dict[str, Any]) -> None:
        facts = event["facts"]
        self.order_status = ORDER_OPENED_STATUS
        self.customer_id = facts["customer_id"]
        self.currency = facts["currency"]
        self.items = list(facts["items"])
        self.total_minor = facts["total_minor"]
        self.discount_minor = facts["discount_minor"]
        self.payable_minor = facts["payable_minor"]
        self.merchant_party_id = facts["merchant_party_id"]
        self.platform_party_id = facts["platform_party_id"]

    def _on_payment_confirmed(self, event: dict[str, Any]) -> None:
        self.order_status = ORDER_PAID

    # --- 信贷报价 -------------------------------------------------------
    def _on_offer_shown(self, event: dict[str, Any]) -> None:
        facts = event["facts"]
        self.offer_id = event["aggregate_id"]
        self.offer_status = OFFER_SHOWN_STATUS
        self.offer_version = facts["offer_version"]
        self.offer_facts = dict(facts)

    def _on_offer_updated(self, event: dict[str, Any]) -> None:
        self._on_offer_shown(event)

    def _on_credit_accepted(self, event: dict[str, Any]) -> None:
        self.offer_status = OFFER_ACCEPTED

    def _on_credit_declined(self, event: dict[str, Any]) -> None:
        # 信贷拒绝只影响报价自身，订单状态保持不变
        self.offer_status = OFFER_DECLINED

    # --- 适当性评估 -----------------------------------------------------
    def _on_suitability_reviewed(self, event: dict[str, Any]) -> None:
        facts = event["facts"]
        self.risk_level = facts["risk_level"]
        self.anomalies = list(facts.get("anomalies", []))
        self.suitability_status = facts["status"]

    def _on_manual_review_queued(self, event: dict[str, Any]) -> None:
        self.suitability_status = SUITABILITY_PENDING_REVIEW

    def _on_manual_review_resolved(self, event: dict[str, Any]) -> None:
        facts = event["facts"]
        self.suitability_status = facts["decision"]
        if facts.get("final_risk_level"):
            self.risk_level = facts["final_risk_level"]

    # --- 逐项确认 -------------------------------------------------------
    def _on_item_confirmed(self, event: dict[str, Any]) -> None:
        facts = event["facts"]
        self.confirmed_items[facts["item_key"]] = {
            "offer_version": facts["offer_version"],
            "risk_level": facts["risk_level"],
            "confirmed_at": event["occurred_at"],
        }

    # --- 合同、账单与后续动作 -------------------------------------------
    def _on_contract_issued(self, event: dict[str, Any]) -> None:
        facts = event["facts"]
        self.contract_id = event["aggregate_id"]
        self.contract_status = CONTRACT_ACTIVE
        self.contract_facts = dict(facts)
        self.revocation_window_until = facts["revocation_window_until"]
        self.schedule = [
            {
                "seq": entry["seq"],
                "due_at": entry["due_at"],
                "amount_minor": entry["amount_minor"],
                "status": SCHEDULE_SCHEDULED,
                "statement_generated": False,
            }
            for entry in facts["schedule"]
        ]

    def _on_consent_revoked(self, event: dict[str, Any]) -> None:
        # 撤回只取消允许的后续动作（未到期的还款计划），历史账单保留
        self.contract_status = CONTRACT_REVOKED
        cancelled = set(event["facts"].get("cancelled_schedule_seqs", []))
        for entry in self.schedule:
            if entry["seq"] in cancelled:
                entry["status"] = SCHEDULE_CANCELLED

    def _on_early_settled(self, event: dict[str, Any]) -> None:
        self.contract_status = CONTRACT_SETTLED
        for entry in self.schedule:
            if entry["status"] == SCHEDULE_SCHEDULED:
                entry["status"] = SCHEDULE_SETTLED

    def _on_statement_generated(self, event: dict[str, Any]) -> None:
        facts = event["facts"]
        seq = facts["seq"]
        self.statements[seq] = dict(facts)
        for entry in self.schedule:
            if entry["seq"] == seq:
                entry["statement_generated"] = True


@dataclass
class DisputeState:
    """争议工单的处理阶段，终态为已解决。"""

    dispute_id: str
    order_id: str = ""
    stage: str = "opened"
    resolved: bool = False
    history: list[dict[str, Any]] = field(default_factory=list)

    def apply(self, event: dict[str, Any]) -> None:
        facts = event["facts"]
        self.order_id = facts["order_id"]
        if event["event_type"] == ev.DISPUTE_OPENED:
            self.stage = facts["stage"]
            self.history.append(
                {"stage": facts["stage"], "occurred_at": event["occurred_at"], "note": facts.get("reason")}
            )
        elif event["event_type"] == ev.DISPUTE_STAGE_ADVANCED:
            self.stage = facts["stage"]
            self.history.append(
                {"stage": facts["stage"], "occurred_at": event["occurred_at"], "note": facts.get("note")}
            )
        elif event["event_type"] == ev.DISPUTE_RESOLVED:
            self.resolved = True
            self.stage = "resolved"
            self.history.append(
                {"stage": "resolved", "occurred_at": event["occurred_at"], "note": facts.get("outcome")}
            )
