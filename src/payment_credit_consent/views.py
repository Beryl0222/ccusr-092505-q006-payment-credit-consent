"""按角色裁剪的只读视图，以及面向监管的事实还原。

平台、放款机构和客服各自只能看到必要字段；监管可以基于事件流完整还原
"当时展示了什么、谁作出决定、资金流向谁、争议处理到哪一步"。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

# 各角色在完整视图基础上需要隐藏的字段（按分区列出）。
_ROLE_HIDDEN: dict[str, dict[str, tuple[str, ...]]] = {
    # 平台不需要看到适当性评估的内部细节和放款账户信息。
    "platform": {"offer": ("suitability_detail",), "credit": ("disbursement",)},
    # 放款机构不需要看到购物车商品明细，只关心金额与信贷事实。
    "lender": {"order": ("items",)},
    # 客服需要还原页面事实与合同参与方，但看不到评估内部细节和放款账户。
    "customer_service": {"offer": ("suitability_detail",), "credit": ("disbursement",)},
    # 监管可以看到全部字段。
    "regulator": {},
}

ROLES = tuple(_ROLE_HIDDEN)


def project_for_role(role: str, full_view: Mapping[str, Any]) -> dict[str, Any]:
    """在完整视图基础上按角色删除字段，不新增、不改写任何内容。"""
    if role not in _ROLE_HIDDEN:
        raise ValueError(f"未知角色：{role}，可用角色：{', '.join(ROLES)}")
    hidden = _ROLE_HIDDEN[role]
    view: dict[str, Any] = {}
    for section, content in full_view.items():
        if content is None:
            view[section] = None
            continue
        view[section] = {key: value for key, value in content.items() if key not in hidden.get(section, ())}
    return view


def reconstruct(order_id: str, events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """按写入顺序回放事件，还原一笔交易的可审计事实。"""
    displayed: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    consents: list[dict[str, Any]] = []
    fund_flow: list[dict[str, Any]] = []
    parties: dict[str, Any] = {}
    dispute: dict[str, Any] | None = None
    for raw in events:
        payload = raw.get("payload", {})
        event_type = raw["event_type"]
        at = raw["occurred_at"]
        if event_type == "OFFER_SHOWN":
            displayed.append(
                {
                    "offer_version": payload["offer_version"],
                    "shown_at": at,
                    "display": payload["display"],
                    "fee_breakdown": payload["fee_breakdown"],
                    "risk_level": payload["risk_level"],
                    "revocation_window_hours": payload["revocation_window_hours"],
                }
            )
            parties = {
                "lender": payload["lender"],
                "funding_party": payload["funding_party"],
                "collection_party": payload["collection_party"],
            }
        elif event_type == "OFFER_SUPERSEDED":
            decisions.append(
                {
                    "kind": "offer_superseded",
                    "offer_version": payload["offer_version"],
                    "reason": payload["reason"],
                    "at": at,
                }
            )
        elif event_type == "SUITABILITY_REVIEWED":
            decisions.append(
                {
                    "kind": "suitability",
                    "outcome": payload["outcome"],
                    "decided_by": payload["decided_by"],
                    "reasons": payload.get("reasons", []),
                    "at": at,
                }
            )
        elif event_type == "MANUAL_REVIEW_RESOLVED":
            decisions.append(
                {
                    "kind": "manual_review",
                    "outcome": payload["outcome"],
                    "decided_by": payload["reviewer"],
                    "note": payload.get("note", ""),
                    "at": at,
                }
            )
        elif event_type == "CONSENT_GIVEN":
            consents.append(
                {
                    "kind": "given",
                    "offer_version": payload["offer_version"],
                    "display_version": payload["display_version"],
                    "items": payload["items"],
                    "confirmed_by": payload["confirmed_by"],
                    "at": at,
                }
            )
            decisions.append({"kind": "consent", "decided_by": payload["confirmed_by"], "at": at})
        elif event_type == "CONSENT_REVOKED":
            consents.append({"kind": "revoked", "reason": payload.get("reason", ""), "at": at})
        elif event_type == "PAYMENT_CONFIRMED":
            fund_flow.append(
                {
                    "kind": "payment",
                    "amount": payload["amount"],
                    "currency": payload.get("currency"),
                    "method": payload["method"],
                    "payee": payload["payee"],
                    "funded_by": payload.get("funded_by"),
                    "at": at,
                }
            )
        elif event_type == "CREDIT_ACCEPTED":
            fund_flow.append(
                {
                    "kind": "disbursement",
                    "agreement_id": payload["agreement_id"],
                    "amount": payload["disbursement"]["amount"],
                    "to": payload["disbursement"]["to"],
                    "funding_party": payload["funding_party"],
                    "at": at,
                }
            )
            parties = {
                "lender": payload["lender"],
                "funding_party": payload["funding_party"],
                "collection_party": payload["collection_party"],
            }
        elif event_type == "CREDIT_REJECTED":
            decisions.append({"kind": "credit_rejected", "decided_by": payload["decided_by"], "at": at})
        elif event_type == "EARLY_SETTLED":
            fund_flow.append(
                {
                    "kind": "early_settlement",
                    "agreement_id": payload["agreement_id"],
                    "amount": payload["settled_amount"],
                    "at": at,
                }
            )
        elif event_type == "DISPUTE_OPENED":
            dispute = {
                "dispute_id": raw["aggregate_id"],
                "topic": payload["topic"],
                "stage": payload["stage"],
                "history": [{"stage": payload["stage"], "handler": payload["opened_by"], "at": at}],
            }
        elif event_type == "DISPUTE_ADVANCED" and dispute is not None:
            dispute["stage"] = payload["stage"]
            dispute["history"].append(
                {"stage": payload["stage"], "handler": payload["handler"], "note": payload.get("note", ""), "at": at}
            )
    return {
        "order_id": order_id,
        "displayed": displayed,
        "decisions": decisions,
        "consents": consents,
        "fund_flow": fund_flow,
        "parties": parties,
        "dispute": dispute,
    }
