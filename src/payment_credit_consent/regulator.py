"""监管还原：一笔交易当时展示了什么、谁作出决定、资金流向谁、争议到哪一步。

全部内容从不可变事件重建，不依赖任何可变的当前快照之外的信息；
还款计划条目的当前状态（有效/已取消/已结清）来自状态折叠结果。
"""

from __future__ import annotations

from typing import Any

from . import events as ev
from .flow import ConsentFlow


def reconstruct_transaction(flow: ConsentFlow, order_id: str) -> dict[str, Any]:
    """还原一笔交易的完整事实链；订单不存在时抛出 KeyError。"""
    events = flow.events_for_order(order_id)
    if not events:
        raise KeyError(f"订单不存在: {order_id}")
    state = flow.order_state(order_id)
    customer_id = state.customer_id if state else None
    return {
        "order_id": order_id,
        "timeline": events,
        "display_history": _display_history(events),
        "decisions": _decisions(events),
        "money_flow": _money_flow(events, state, customer_id),
        "statements": _statements(state),
        "consent": _consent(state),
        "dispute": _dispute(events),
    }


def _display_history(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """每次点击前后的页面事实：每一版报价对应一份不可变展示快照。"""
    history = []
    for event in events:
        if event["event_type"] not in (ev.OFFER_SHOWN, ev.OFFER_UPDATED):
            continue
        facts = event["facts"]
        history.append(
            {
                "offer_version": facts["offer_version"],
                "occurred_at": event["occurred_at"],
                "event_id": event["event_id"],
                "display_copy": facts["display_copy"],
                "fees": facts["fees"],
                "terms": facts["terms"],
                "repayment_schedule": facts["repayment_schedule"],
                "parties": facts["parties"],
                "collection_party_id": facts["collection_party_id"],
                "revocation_window_hours": facts["revocation_window_hours"],
                "change_reason": facts.get("change_reason"),
            }
        )
    return history


def _decisions(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """谁、在什么时候、作出了什么决定。"""
    decisions = []

    def add(event: dict[str, Any], decision: str, actor: str | None, detail: dict[str, Any]) -> None:
        decisions.append(
            {
                "occurred_at": event["occurred_at"],
                "event_id": event["event_id"],
                "decision": decision,
                "actor": actor,
                "detail": detail,
            }
        )

    for event in events:
        facts = event["facts"]
        event_type = event["event_type"]
        if event_type == ev.SUITABILITY_REVIEWED:
            add(event, "suitability_reviewed", facts["decided_by"],
                {"risk_level": facts["risk_level"], "status": facts["status"]})
        elif event_type == ev.MANUAL_REVIEW_QUEUED:
            add(event, "manual_review_queued", "system", {"anomalies": facts["anomalies"]})
        elif event_type == ev.MANUAL_REVIEW_RESOLVED:
            add(event, "manual_review_resolved", facts["reviewer"],
                {"decision": facts["decision"], "final_risk_level": facts.get("final_risk_level")})
        elif event_type == ev.ITEM_CONFIRMED:
            add(event, "item_confirmed", facts["actor"],
                {"item_key": facts["item_key"], "offer_version": facts["offer_version"],
                 "risk_level": facts["risk_level"]})
        elif event_type == ev.PAYMENT_CONFIRMED:
            add(event, "payment_confirmed", facts["payer"],
                {"funded_by": facts["funded_by"], "amount_minor": facts["amount_minor"]})
        elif event_type == ev.CREDIT_ACCEPTED:
            add(event, "credit_accepted", facts["customer_id"],
                {"offer_version": facts["offer_version"]})
        elif event_type == ev.CREDIT_DECLINED:
            add(event, "credit_declined", facts["declined_by"], {"reason": facts["reason"]})
        elif event_type == ev.CONSENT_REVOKED:
            add(event, "consent_revoked", "customer",
                {"cancelled_schedule_seqs": facts["cancelled_schedule_seqs"]})
        elif event_type == ev.EARLY_SETTLED:
            add(event, "early_settled", "customer",
                {"settled_amount_minor": facts["settled_amount_minor"]})
        elif event_type == ev.DISPUTE_OPENED:
            add(event, "dispute_opened", facts["raised_by"], {"reason": facts["reason"]})
        elif event_type == ev.DISPUTE_STAGE_ADVANCED:
            add(event, "dispute_stage_advanced", "platform", {"stage": facts["stage"]})
        elif event_type == ev.DISPUTE_RESOLVED:
            add(event, "dispute_resolved", "platform", {"outcome": facts["outcome"]})
    return decisions


def _money_flow(events: list[dict[str, Any]], state: Any, customer_id: str | None) -> list[dict[str, Any]]:
    """资金流向：实际支付、放款、计划还款（含当前状态）、账单应收。"""
    flow_entries = []
    schedule_status = {}
    if state is not None:
        schedule_status = {entry["seq"]: entry["status"] for entry in state.schedule}
    for event in events:
        facts = event["facts"]
        if event["event_type"] == ev.PAYMENT_CONFIRMED:
            flow_entries.append(
                {
                    "occurred_at": event["occurred_at"],
                    "event_id": event["event_id"],
                    "payer": facts["payer"],
                    "payee": facts["payee"],
                    "amount_minor": facts["amount_minor"],
                    "currency": facts["currency"],
                    "purpose": "订单支付",
                    "nature": "actual",
                    "funded_by": facts["funded_by"],
                }
            )
        elif event["event_type"] == ev.CONTRACT_ISSUED:
            disbursement = facts["disbursement"]
            flow_entries.append(
                {
                    "occurred_at": event["occurred_at"],
                    "event_id": event["event_id"],
                    "payer": disbursement["payer"],
                    "payee": disbursement["payee"],
                    "amount_minor": disbursement["amount_minor"],
                    "currency": disbursement["currency"],
                    "purpose": "信贷放款",
                    "nature": "actual",
                }
            )
            for entry in facts["schedule"]:
                flow_entries.append(
                    {
                        "occurred_at": entry["due_at"],
                        "event_id": event["event_id"],
                        "payer": customer_id,
                        "payee": facts["collection_party_id"],
                        "amount_minor": entry["amount_minor"],
                        "currency": facts.get("currency", "CNY"),
                        "purpose": f"分期还款第{entry['seq']}期",
                        "nature": "scheduled",
                        "status": schedule_status.get(entry["seq"], "scheduled"),
                    }
                )
        elif event["event_type"] == ev.STATEMENT_GENERATED:
            flow_entries.append(
                {
                    "occurred_at": event["occurred_at"],
                    "event_id": event["event_id"],
                    "payer": customer_id,
                    "payee": facts["payee"],
                    "amount_minor": facts["amount_minor"],
                    "currency": facts["currency"],
                    "purpose": f"第{facts['seq']}期账单应收",
                    "nature": "billed",
                }
            )
    return flow_entries


def _statements(state: Any) -> list[dict[str, Any]]:
    """历史账单：撤回或结清后仍然保留，收款主体可审计。"""
    if state is None:
        return []
    return [state.statements[seq] for seq in sorted(state.statements)]


def _consent(state: Any) -> dict[str, Any]:
    if state is None:
        return {}
    return {
        "offer_version": state.offer_version,
        "risk_level": state.risk_level,
        "confirmed_items": state.confirmed_items,
        "contract_status": state.contract_status,
        "revocation_window_until": state.revocation_window_until,
    }


def _dispute(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """争议处理处于哪一步；一笔订单可能有多张争议工单。"""
    cases: dict[str, dict[str, Any]] = {}
    for event in events:
        if event["aggregate_type"] != ev.AGGREGATE_DISPUTE_CASE:
            continue
        case = cases.setdefault(
            event["aggregate_id"],
            {"dispute_id": event["aggregate_id"], "current_stage": None, "resolved": False, "history": []},
        )
        facts = event["facts"]
        if event["event_type"] == ev.DISPUTE_OPENED:
            case["current_stage"] = facts["stage"]
            case["history"].append({"stage": facts["stage"], "occurred_at": event["occurred_at"]})
        elif event["event_type"] == ev.DISPUTE_STAGE_ADVANCED:
            case["current_stage"] = facts["stage"]
            case["history"].append({"stage": facts["stage"], "occurred_at": event["occurred_at"]})
        elif event["event_type"] == ev.DISPUTE_RESOLVED:
            case["current_stage"] = "resolved"
            case["resolved"] = True
            case["history"].append({"stage": "resolved", "occurred_at": event["occurred_at"]})
    if not cases:
        return None
    ordered = sorted(cases.values(), key=lambda case: case["dispute_id"])
    return ordered[0] if len(ordered) == 1 else {"cases": ordered}
