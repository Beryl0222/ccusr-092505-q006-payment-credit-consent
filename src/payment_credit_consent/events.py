"""领域事件类型常量与构造辅助。

事件信封字段以 ``contracts/domain.schema.json`` 为准，本模块只负责
按契约组装字典，不在交换层之外引入第二份事实定义。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

# 聚合类型
AGGREGATE_PAYMENT_ORDER = "payment_order"
AGGREGATE_CREDIT_OFFER = "credit_offer"
AGGREGATE_CONSENT_RECORD = "consent_record"
AGGREGATE_SUITABILITY = "suitability_assessment"
AGGREGATE_CREDIT_CONTRACT = "credit_contract"
AGGREGATE_DISPUTE_CASE = "dispute_case"

# 事件类型
ORDER_OPENED = "ORDER_OPENED"
OFFER_SHOWN = "OFFER_SHOWN"
OFFER_UPDATED = "OFFER_UPDATED"
SUITABILITY_REVIEWED = "SUITABILITY_REVIEWED"
MANUAL_REVIEW_QUEUED = "MANUAL_REVIEW_QUEUED"
MANUAL_REVIEW_RESOLVED = "MANUAL_REVIEW_RESOLVED"
ITEM_CONFIRMED = "ITEM_CONFIRMED"
PAYMENT_CONFIRMED = "PAYMENT_CONFIRMED"
CREDIT_ACCEPTED = "CREDIT_ACCEPTED"
CREDIT_DECLINED = "CREDIT_DECLINED"
CONTRACT_ISSUED = "CONTRACT_ISSUED"
CONSENT_REVOKED = "CONSENT_REVOKED"
EARLY_SETTLED = "EARLY_SETTLED"
STATEMENT_GENERATED = "STATEMENT_GENERATED"
DISPUTE_OPENED = "DISPUTE_OPENED"
DISPUTE_STAGE_ADVANCED = "DISPUTE_STAGE_ADVANCED"
DISPUTE_RESOLVED = "DISPUTE_RESOLVED"


def consent_record_id(order_id: str) -> str:
    """同意记录与订单一一对应，标识派生自订单号。"""
    return f"consent-{order_id}"


def suitability_id(order_id: str) -> str:
    return f"suitability-{order_id}"


def contract_id_of(order_id: str) -> str:
    return f"contract-{order_id}"


def build_event(
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    occurred_at: str,
    version: int,
    summary: str,
    facts: dict[str, Any],
    command_id: str,
) -> dict[str, Any]:
    """组装一条符合契约信封的事件，事实内容放入 ``facts``。"""
    return {
        "event_id": f"evt-{uuid4().hex}",
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "occurred_at": occurred_at,
        "version": version,
        "summary": summary,
        "command_id": command_id,
        "facts": facts,
    }
