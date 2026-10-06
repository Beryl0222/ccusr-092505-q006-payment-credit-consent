from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import helpers
from helpers import (
    COLLECTOR_ID,
    CUSTOMER_ID,
    DISPLAY_COPY,
    LENDER_ID,
    MERCHANT_ID,
    ORDER_ID,
    REPAYMENT_SCHEDULE,
    TERMS,
    assess,
    confirm_all_items,
    confirm_credit,
    make_flow,
    open_order,
    show_offer,
)
from payment_credit_consent.regulator import reconstruct_transaction


class RegulatorTests(unittest.TestCase):
    """监管查询一笔交易：展示、决定、资金、争议四要素都能还原。"""

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.flow = make_flow(Path(self.dir.name))
        self.assertTrue(open_order(self.flow).ok)
        self.assertTrue(show_offer(self.flow).ok)
        # 报价变化一次，产生两版页面事实
        new_terms = dict(TERMS, total_cost_minor=103000, total_interest_minor=3000)
        new_schedule = [dict(entry, amount_minor=34333) for entry in REPAYMENT_SCHEDULE]
        new_schedule[2]["amount_minor"] = 34334
        self.assertTrue(
            self.flow.update_credit_offer(
                "cmd-offer-2",
                order_id=ORDER_ID,
                terms=new_terms,
                fees=helpers.FEES,
                parties=helpers.PARTIES,
                collection_party_id=COLLECTOR_ID,
                revocation_window_hours=48,
                repayment_schedule=new_schedule,
                display_copy=dict(DISPLAY_COPY, copy_version="copy-v2"),
                change_reason="资金成本上调",
                occurred_at="2026-10-06T10:02:30+08:00",
            ).ok
        )
        self.assertTrue(assess(self.flow, occurred_at="2026-10-06T10:03:00+08:00").ok)
        for decision in confirm_all_items(self.flow, occurred_at="2026-10-06T10:03:30+08:00"):
            self.assertTrue(decision.ok)
        self.assertTrue(
            confirm_credit(self.flow, offer_version=2, occurred_at="2026-10-06T10:04:00+08:00").ok
        )
        self.assertTrue(
            self.flow.generate_statement(
                "cmd-stmt-1", order_id=ORDER_ID, seq=1,
                occurred_at="2026-10-20T10:00:00+08:00",
            ).ok
        )
        self.assertTrue(
            self.flow.open_dispute(
                "cmd-dispute-1", dispute_id="dispute-1", order_id=ORDER_ID,
                raised_by="customer", reason="未注意到同时开通了信贷",
                occurred_at="2026-10-21T09:00:00+08:00",
            ).ok
        )
        self.assertTrue(
            self.flow.advance_dispute(
                "cmd-dispute-2", dispute_id="dispute-1", stage="evidence",
                occurred_at="2026-10-21T10:00:00+08:00",
            ).ok
        )
        self.report = reconstruct_transaction(self.flow, ORDER_ID)

    def tearDown(self) -> None:
        self.dir.cleanup()

    def test_display_history_rebuilds_every_page_version(self) -> None:
        history = self.report["display_history"]
        self.assertEqual([1, 2], [item["offer_version"] for item in history])
        self.assertEqual("copy-v1", history[0]["display_copy"]["copy_version"])
        self.assertEqual("copy-v2", history[1]["display_copy"]["copy_version"])
        self.assertEqual("资金成本上调", history[1]["change_reason"])
        # 当时展示的综合成本、风险提示、合同参与方、撤销窗口都在
        keys = {block["key"] for block in history[1]["display_copy"]["blocks"]}
        self.assertEqual(
            {"comprehensive_cost", "risk_warning", "contract_parties", "revocation_window"}, keys
        )
        self.assertEqual(COLLECTOR_ID, history[1]["collection_party_id"])

    def test_decisions_identify_who_decided_what(self) -> None:
        decisions = self.report["decisions"]
        by_kind = {}
        for item in decisions:
            by_kind.setdefault(item["decision"], []).append(item)
        self.assertEqual("risk-engine", by_kind["suitability_reviewed"][0]["actor"])
        self.assertEqual(4, len(by_kind["item_confirmed"]))
        self.assertEqual(CUSTOMER_ID, by_kind["payment_confirmed"][0]["actor"])
        self.assertEqual("credit", by_kind["payment_confirmed"][0]["detail"]["funded_by"])
        self.assertEqual(CUSTOMER_ID, by_kind["credit_accepted"][0]["actor"])
        self.assertEqual("customer", by_kind["dispute_opened"][0]["actor"])

    def test_money_flow_shows_where_money_went(self) -> None:
        flow_entries = self.report["money_flow"]
        actual = [e for e in flow_entries if e["nature"] == "actual"]
        purposes = {e["purpose"] for e in actual}
        self.assertEqual({"订单支付", "信贷放款"}, purposes)
        payment = next(e for e in actual if e["purpose"] == "订单支付")
        self.assertEqual((CUSTOMER_ID, MERCHANT_ID, 100000), (payment["payer"], payment["payee"], payment["amount_minor"]))
        disbursement = next(e for e in actual if e["purpose"] == "信贷放款")
        self.assertEqual((LENDER_ID, MERCHANT_ID, 100000), (disbursement["payer"], disbursement["payee"], disbursement["amount_minor"]))
        scheduled = [e for e in flow_entries if e["nature"] == "scheduled"]
        self.assertEqual(3, len(scheduled))
        self.assertTrue(all(e["payee"] == COLLECTOR_ID for e in scheduled))
        self.assertTrue(all(e["status"] == "scheduled" for e in scheduled))
        billed = [e for e in flow_entries if e["nature"] == "billed"]
        self.assertEqual(1, len(billed))
        self.assertEqual(COLLECTOR_ID, billed[0]["payee"])

    def test_dispute_stage_is_reported(self) -> None:
        dispute = self.report["dispute"]
        self.assertEqual("dispute-1", dispute["dispute_id"])
        self.assertEqual("evidence", dispute["current_stage"])
        self.assertFalse(dispute["resolved"])
        self.assertEqual(["opened", "evidence"], [h["stage"] for h in dispute["history"]])

    def test_consent_and_statements_are_auditable(self) -> None:
        consent = self.report["consent"]
        self.assertEqual(2, consent["offer_version"])
        self.assertEqual("low", consent["risk_level"])
        self.assertEqual("active", consent["contract_status"])
        statements = self.report["statements"]
        self.assertEqual(1, len(statements))
        self.assertEqual(COLLECTOR_ID, statements[0]["payee"])

    def test_unknown_order_raises(self) -> None:
        with self.assertRaises(KeyError):
            reconstruct_transaction(self.flow, "order-missing")


if __name__ == "__main__":
    unittest.main()
