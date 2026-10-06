"""测试共用的场景构造工具。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from payment_credit_consent.flow import ConsentFlow  # noqa: E402

SCHEMA = json.loads((ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8"))

T0 = "2026-10-06T10:00:00+08:00"

ORDER_ID = "order-1"
CUSTOMER_ID = "cust-1"
MERCHANT_ID = "merchant-1"
PLATFORM_ID = "platform-1"
LENDER_ID = "lender-1"
COLLECTOR_ID = "collector-1"

ITEMS = [{"sku": "sku-1", "name": "降噪耳机", "qty": 1, "unit_price_minor": 120000}]
TOTAL_MINOR = 120000
DISCOUNT_MINOR = 20000
PAYABLE_MINOR = 100000

TERMS = {
    "principal_minor": PAYABLE_MINOR,
    "apr_bp": 1200,
    "installments": 3,
    "per_installment_minor": 34000,
    "total_interest_minor": 2000,
    "total_cost_minor": 102000,
}

REPAYMENT_SCHEDULE = [
    {"seq": 1, "due_at": "2026-10-26T10:00:00+08:00", "amount_minor": 34000},
    {"seq": 2, "due_at": "2026-11-26T10:00:00+08:00", "amount_minor": 34000},
    {"seq": 3, "due_at": "2026-12-26T10:00:00+08:00", "amount_minor": 34000},
]

FEES = [
    {"key": "instant_discount", "label": "立减优惠", "kind": "discount", "amount_minor": -20000},
    {"key": "credit_service", "label": "信贷服务费", "kind": "service", "amount_minor": 2000},
]

PARTIES = [
    {"party_id": PLATFORM_ID, "role": "platform", "name": "平台方"},
    {"party_id": MERCHANT_ID, "role": "merchant", "name": "商户"},
    {"party_id": LENDER_ID, "role": "lender", "name": "放款银行"},
    {"party_id": COLLECTOR_ID, "role": "collection_agency", "name": "催收服务机构"},
]

DISPLAY_COPY = {
    "page_id": "checkout",
    "copy_version": "copy-v1",
    "blocks": [
        {"key": "comprehensive_cost", "title": "综合成本", "body": "实付 1000.00 元，分 3 期，总成本 1020.00 元"},
        {"key": "risk_warning", "title": "风险提示", "body": "逾期将影响征信并产生罚息"},
        {"key": "contract_parties", "title": "合同参与方", "body": "放款银行为实际放款方，催收由催收服务机构负责"},
        {"key": "revocation_window", "title": "撤销窗口", "body": "确认后 48 小时内可撤回信贷同意"},
    ],
}

REVOCATION_WINDOW_HOURS = 48


def make_flow(store_dir: Path) -> ConsentFlow:
    return ConsentFlow(store_dir, SCHEMA)


def open_order(flow: ConsentFlow, order_id: str = ORDER_ID, command_id: str | None = None,
               occurred_at: str = T0, **overrides):
    kwargs = {
        "order_id": order_id,
        "customer_id": CUSTOMER_ID,
        "items": ITEMS,
        "total_minor": TOTAL_MINOR,
        "discount_minor": DISCOUNT_MINOR,
        "currency": "CNY",
        "merchant_party_id": MERCHANT_ID,
        "platform_party_id": PLATFORM_ID,
        "occurred_at": occurred_at,
    }
    kwargs.update(overrides)
    return flow.open_checkout(command_id or f"cmd-open-{order_id}", **kwargs)


def show_offer(flow: ConsentFlow, order_id: str = ORDER_ID, command_id: str | None = None,
               occurred_at: str = "2026-10-06T10:01:00+08:00", **overrides):
    kwargs = {
        "order_id": order_id,
        "offer_id": f"offer-{order_id}",
        "terms": dict(TERMS),
        "fees": list(FEES),
        "parties": list(PARTIES),
        "collection_party_id": COLLECTOR_ID,
        "revocation_window_hours": REVOCATION_WINDOW_HOURS,
        "repayment_schedule": [dict(entry) for entry in REPAYMENT_SCHEDULE],
        "display_copy": DISPLAY_COPY,
        "occurred_at": occurred_at,
    }
    kwargs.update(overrides)
    return flow.show_credit_offer(command_id or f"cmd-offer-{order_id}", **kwargs)


def assess(flow: ConsentFlow, order_id: str = ORDER_ID, command_id: str | None = None,
           risk_level: str = "low", anomalies: list[str] | None = None,
           occurred_at: str = "2026-10-06T10:02:00+08:00"):
    return flow.record_suitability(
        command_id or f"cmd-assess-{order_id}",
        order_id=order_id,
        risk_level=risk_level,
        anomalies=anomalies or [],
        decided_by="risk-engine",
        occurred_at=occurred_at,
    )


def confirm_all_items(flow: ConsentFlow, order_id: str = ORDER_ID,
                      command_prefix: str | None = None,
                      occurred_at: str = "2026-10-06T10:03:00+08:00"):
    prefix = command_prefix or f"cmd-item-{order_id}"
    decisions = []
    for index, item in enumerate(
        ("comprehensive_cost", "risk_warning", "contract_parties", "revocation_window"), start=1
    ):
        decisions.append(
            flow.confirm_item(
                f"{prefix}-{index}",
                order_id=order_id,
                item_key=item,
                occurred_at=occurred_at,
            )
        )
    return decisions


def confirm_credit(flow: ConsentFlow, order_id: str = ORDER_ID, command_id: str | None = None,
                   offer_version: int = 1, risk_level: str = "low",
                   occurred_at: str = "2026-10-06T10:04:00+08:00"):
    return flow.confirm_checkout(
        command_id or f"cmd-confirm-{order_id}",
        order_id=order_id,
        pay_with_credit=True,
        offer_version=offer_version,
        risk_level=risk_level,
        occurred_at=occurred_at,
    )


def open_offer_assess_confirm(flow: ConsentFlow, order_id: str = ORDER_ID):
    """走完 下单→报价→评估→逐项确认→综合确认 的完整信贷链路。"""
    assert open_order(flow, order_id=order_id).ok
    assert show_offer(flow, order_id=order_id).ok
    assert assess(flow, order_id=order_id).ok
    for decision in confirm_all_items(flow, order_id=order_id):
        assert decision.ok
    return confirm_credit(flow, order_id=order_id)
