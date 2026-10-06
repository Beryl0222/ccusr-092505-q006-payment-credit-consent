"""角色字段投影：平台、放款机构、客服各自只能看到必要字段。

投影只读不改：返回新字典，被裁剪的字段名列入 ``redacted_fields``，
让消费方知道存在被保留的事实；监管角色可以看到全部内容。
"""

from __future__ import annotations

import copy
from typing import Any

ROLE_PLATFORM = "platform"
ROLE_LENDER = "lender"
ROLE_CUSTOMER_SERVICE = "customer_service"
ROLE_REGULATOR = "regulator"

ROLES = (ROLE_PLATFORM, ROLE_LENDER, ROLE_CUSTOMER_SERVICE, ROLE_REGULATOR)

# 按角色隐藏的事实字段：最小必要原则
_HIDDEN_FACTS: dict[str, dict[str, set[str]]] = {
    ROLE_PLATFORM: {
        # 平台不接触评估细节与复核人，只看结论状态
        "SUITABILITY_REVIEWED": {"anomalies", "decided_by"},
        "MANUAL_REVIEW_QUEUED": {"anomalies"},
        "MANUAL_REVIEW_RESOLVED": {"reviewer", "note"},
        # 还款计划属于放款机构与催收方
        "CONTRACT_ISSUED": {"schedule"},
    },
    ROLE_LENDER: {
        # 放款机构不需要购物车明细与平台展示文案
        "ORDER_OPENED": {"items"},
        "OFFER_SHOWN": {"display_copy"},
        "OFFER_UPDATED": {"display_copy"},
        "PAYMENT_CONFIRMED": {"consent"},
    },
    ROLE_CUSTOMER_SERVICE: {
        # 客服能看到用户当时看到的展示与确认，不接触评估内部细节
        "SUITABILITY_REVIEWED": {"anomalies", "decided_by", "risk_level"},
        "MANUAL_REVIEW_QUEUED": {"anomalies"},
        "MANUAL_REVIEW_RESOLVED": {"reviewer", "final_risk_level", "note"},
    },
    ROLE_REGULATOR: {},
}


def project_event(event: dict[str, Any], role: str) -> dict[str, Any]:
    """按角色投影单条事件；未知角色直接拒绝。"""
    if role not in ROLES:
        raise ValueError(f"未知角色: {role!r}")
    projected = {key: copy.deepcopy(value) for key, value in event.items() if key != "facts"}
    facts = copy.deepcopy(event.get("facts", {}))
    hidden = _HIDDEN_FACTS[role].get(event["event_type"], set())
    redacted = sorted(key for key in facts if key in hidden)
    for key in redacted:
        facts.pop(key)
    projected["facts"] = facts
    projected["redacted_fields"] = redacted
    return projected


def project_events(events: list[dict[str, Any]], role: str) -> list[dict[str, Any]]:
    return [project_event(event, role) for event in events]
