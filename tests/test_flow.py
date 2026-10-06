from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import helpers
from helpers import (
    COLLECTOR_ID,
    DISPLAY_COPY,
    ORDER_ID,
    REPAYMENT_SCHEDULE,
    TERMS,
    assess,
    confirm_all_items,
    confirm_credit,
    make_flow,
    open_offer_assess_confirm,
    open_order,
    show_offer,
)


class FlowTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.flow = make_flow(Path(self.dir.name))

    def tearDown(self) -> None:
        self.dir.cleanup()


class PaymentWithoutCreditTests(FlowTestCase):
    def test_payment_completes_without_credit(self) -> None:
        self.assertTrue(open_order(self.flow).ok)
        decision = self.flow.confirm_checkout(
            "cmd-pay-cash", order_id=ORDER_ID, pay_with_credit=False,
            occurred_at="2026-10-06T10:05:00+08:00",
        )
        self.assertTrue(decision.ok)
        self.assertEqual(["PAYMENT_CONFIRMED"], [e["event_type"] for e in decision.events])
        self.assertEqual("cash", decision.events[0]["facts"]["funded_by"])
        state = self.flow.order_state(ORDER_ID)
        self.assertEqual("paid", state.order_status)
        self.assertIsNone(state.contract_id)

    def test_credit_decline_does_not_change_order(self) -> None:
        self.assertTrue(open_order(self.flow).ok)
        self.assertTrue(show_offer(self.flow).ok)
        declined = self.flow.decline_credit(
            "cmd-decline-1", order_id=ORDER_ID, reason="综合评分不足",
            declined_by="lender", occurred_at="2026-10-06T10:06:00+08:00",
        )
        self.assertTrue(declined.ok)
        state = self.flow.order_state(ORDER_ID)
        self.assertEqual("opened", state.order_status)
        self.assertEqual("declined", state.offer_status)
        # 订单仍可无信贷支付
        paid = self.flow.confirm_checkout(
            "cmd-pay-cash-2", order_id=ORDER_ID, pay_with_credit=False,
            occurred_at="2026-10-06T10:07:00+08:00",
        )
        self.assertTrue(paid.ok)
        # 被拒绝的报价不能再用于信贷确认
        stale = self.flow.confirm_checkout(
            "cmd-confirm-declined", order_id=ORDER_ID, pay_with_credit=False,
            occurred_at="2026-10-06T10:08:00+08:00",
        )
        self.assertFalse(stale.ok)
        self.assertEqual("already_confirmed", stale.reason)


class IdempotencyTests(FlowTestCase):
    def test_retry_returns_original_decision_and_events(self) -> None:
        first = open_order(self.flow)
        self.assertTrue(first.ok)
        retry = open_order(self.flow)
        self.assertTrue(retry.ok)
        self.assertEqual(
            [e["event_id"] for e in first.events],
            [e["event_id"] for e in retry.events],
        )
        self.assertEqual(1, len(self.flow.events_for_order(ORDER_ID)))

    def test_same_command_id_with_different_payload_conflicts(self) -> None:
        self.assertTrue(open_order(self.flow).ok)
        conflict = open_order(self.flow, total_minor=999)
        self.assertFalse(conflict.ok)
        self.assertEqual("idempotency_conflict", conflict.reason)

    def test_rejected_decision_is_replayed_verbatim(self) -> None:
        self.assertTrue(open_order(self.flow).ok)
        self.assertTrue(show_offer(self.flow).ok)
        self.assertTrue(assess(self.flow).ok)
        # 未做任何逐项确认，综合确认被拒绝
        rejected = confirm_credit(self.flow)
        self.assertFalse(rejected.ok)
        self.assertEqual("items_missing", rejected.reason)
        # 补齐确认后，同一 command_id 仍返回原拒绝决定
        for decision in confirm_all_items(self.flow):
            self.assertTrue(decision.ok)
        replay = confirm_credit(self.flow)
        self.assertFalse(replay.ok)
        self.assertEqual("items_missing", replay.reason)
        self.assertEqual(rejected.details, replay.details)
        # 新的 command_id 才能基于新事实作出新决定
        fresh = confirm_credit(self.flow, command_id="cmd-confirm-2")
        self.assertTrue(fresh.ok)


class ReconfirmationTests(FlowTestCase):
    def test_offer_update_invalidates_previous_item_confirmations(self) -> None:
        self.assertTrue(open_order(self.flow).ok)
        self.assertTrue(show_offer(self.flow).ok)
        self.assertTrue(assess(self.flow).ok)
        for decision in confirm_all_items(self.flow):
            self.assertTrue(decision.ok)
        # 报价变化：费用上涨，展示文案升版
        new_terms = dict(TERMS, total_cost_minor=103000, total_interest_minor=3000)
        new_schedule = [dict(entry, amount_minor=34333) for entry in REPAYMENT_SCHEDULE]
        new_schedule[2]["amount_minor"] = 34334
        updated = self.flow.update_credit_offer(
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
            occurred_at="2026-10-06T10:03:30+08:00",
        )
        self.assertTrue(updated.ok)
        # 旧报价版本不能沿用旧同意
        stale = confirm_credit(self.flow, command_id="cmd-confirm-stale", offer_version=1)
        self.assertFalse(stale.ok)
        self.assertEqual("reconfirmation_required", stale.reason)
        # 新版本下旧的逐项确认已失效
        missing = confirm_credit(self.flow, command_id="cmd-confirm-v2", offer_version=2)
        self.assertFalse(missing.ok)
        self.assertEqual("items_missing", missing.reason)
        self.assertEqual(4, len(missing.details["missing"]))
        # 重新逐项确认后才能完成综合确认
        for decision in confirm_all_items(self.flow, command_prefix="cmd-item-v2"):
            self.assertTrue(decision.ok)
        confirmed = confirm_credit(self.flow, command_id="cmd-confirm-v2b", offer_version=2)
        self.assertTrue(confirmed.ok)
        contract = next(e for e in confirmed.events if e["event_type"] == "CONTRACT_ISSUED")
        self.assertEqual(2, contract["facts"]["offer_version"])

    def test_risk_level_change_invalidates_previous_consent(self) -> None:
        self.assertTrue(open_order(self.flow).ok)
        self.assertTrue(show_offer(self.flow).ok)
        self.assertTrue(assess(self.flow, risk_level="low").ok)
        for decision in confirm_all_items(self.flow):
            self.assertTrue(decision.ok)
        # 风险等级变化：新的评估结果
        reassess = assess(
            self.flow, command_id="cmd-assess-2", risk_level="high",
            occurred_at="2026-10-06T10:03:40+08:00",
        )
        self.assertTrue(reassess.ok)
        stale = confirm_credit(self.flow, command_id="cmd-confirm-old-risk", risk_level="low")
        self.assertFalse(stale.ok)
        self.assertEqual("reconfirmation_required", stale.reason)
        missing = confirm_credit(self.flow, command_id="cmd-confirm-high", risk_level="high")
        self.assertFalse(missing.ok)
        self.assertEqual("items_missing", missing.reason)
        for decision in confirm_all_items(self.flow, command_prefix="cmd-item-high"):
            self.assertTrue(decision.ok)
        confirmed = confirm_credit(self.flow, command_id="cmd-confirm-high-2", risk_level="high")
        self.assertTrue(confirmed.ok)


class ItemConfirmationTests(FlowTestCase):
    def test_every_required_item_must_be_confirmed(self) -> None:
        self.assertTrue(open_order(self.flow).ok)
        self.assertTrue(show_offer(self.flow).ok)
        self.assertTrue(assess(self.flow).ok)
        rejected = confirm_credit(self.flow)
        self.assertFalse(rejected.ok)
        self.assertEqual("items_missing", rejected.reason)
        self.assertEqual(
            ["comprehensive_cost", "risk_warning", "contract_parties", "revocation_window"],
            rejected.details["missing"],
        )
        self.flow.confirm_item(
            "cmd-item-only-1", order_id=ORDER_ID, item_key="comprehensive_cost",
            occurred_at="2026-10-06T10:03:00+08:00",
        )
        still_missing = confirm_credit(self.flow, command_id="cmd-confirm-partial")
        self.assertFalse(still_missing.ok)
        self.assertEqual(
            ["risk_warning", "contract_parties", "revocation_window"],
            still_missing.details["missing"],
        )

    def test_unknown_item_is_rejected(self) -> None:
        self.assertTrue(open_order(self.flow).ok)
        self.assertTrue(show_offer(self.flow).ok)
        decision = self.flow.confirm_item(
            "cmd-item-x", order_id=ORDER_ID, item_key="marketing_opt_in",
            occurred_at="2026-10-06T10:03:00+08:00",
        )
        self.assertFalse(decision.ok)
        self.assertEqual("unknown_item", decision.reason)


class ManualReviewTests(FlowTestCase):
    def test_anomaly_routes_to_manual_review_and_blocks_confirmation(self) -> None:
        self.assertTrue(open_order(self.flow).ok)
        self.assertTrue(show_offer(self.flow).ok)
        flagged = assess(self.flow, anomalies=["identity_mismatch"], risk_level="high")
        self.assertTrue(flagged.ok)
        self.assertEqual(
            ["SUITABILITY_REVIEWED", "MANUAL_REVIEW_QUEUED"],
            [e["event_type"] for e in flagged.events],
        )
        for decision in confirm_all_items(self.flow):
            self.assertTrue(decision.ok)
        blocked = confirm_credit(self.flow, risk_level="high")
        self.assertFalse(blocked.ok)
        self.assertEqual("manual_review_pending", blocked.reason)
        self.assertEqual([ORDER_ID], [item["order_id"] for item in self.flow.pending_manual_reviews()])

    def test_review_approval_unblocks_and_risk_change_requires_reconfirm(self) -> None:
        self.assertTrue(open_order(self.flow).ok)
        self.assertTrue(show_offer(self.flow).ok)
        self.assertTrue(assess(self.flow, anomalies=["income_gap"], risk_level="high").ok)
        for decision in confirm_all_items(self.flow):
            self.assertTrue(decision.ok)
        resolved = self.flow.resolve_manual_review(
            "cmd-review-1", order_id=ORDER_ID, decision="approved", reviewer="reviewer-7",
            final_risk_level="medium", occurred_at="2026-10-06T11:00:00+08:00",
        )
        self.assertTrue(resolved.ok)
        # 复核调整了风险等级，旧确认失效
        stale = confirm_credit(self.flow, command_id="cmd-confirm-after-review", risk_level="high")
        self.assertFalse(stale.ok)
        self.assertEqual("reconfirmation_required", stale.reason)
        for decision in confirm_all_items(self.flow, command_prefix="cmd-item-review"):
            self.assertTrue(decision.ok)
        confirmed = confirm_credit(
            self.flow, command_id="cmd-confirm-reviewed", risk_level="medium",
        )
        self.assertTrue(confirmed.ok)

    def test_review_decline_blocks_credit_but_not_cash_payment(self) -> None:
        self.assertTrue(open_order(self.flow).ok)
        self.assertTrue(show_offer(self.flow).ok)
        self.assertTrue(assess(self.flow, anomalies=["fraud_signal"], risk_level="high").ok)
        resolved = self.flow.resolve_manual_review(
            "cmd-review-decline", order_id=ORDER_ID, decision="declined", reviewer="reviewer-9",
            occurred_at="2026-10-06T11:00:00+08:00",
        )
        self.assertTrue(resolved.ok)
        blocked = confirm_credit(self.flow, command_id="cmd-confirm-declined-review", risk_level="high")
        self.assertFalse(blocked.ok)
        self.assertEqual("suitability_declined", blocked.reason)
        cash = self.flow.confirm_checkout(
            "cmd-cash-after-decline", order_id=ORDER_ID, pay_with_credit=False,
            occurred_at="2026-10-06T11:05:00+08:00",
        )
        self.assertTrue(cash.ok)
        self.assertEqual("paid", self.flow.order_state(ORDER_ID).order_status)


class RevocationAndSettlementTests(FlowTestCase):
    def test_revoke_within_window_cancels_only_future_schedule(self) -> None:
        # 第一期在撤销窗口内到期，第二、三期在窗口之后到期
        schedule = [
            {"seq": 1, "due_at": "2026-10-07T10:00:00+08:00", "amount_minor": 34000},
            {"seq": 2, "due_at": "2026-11-26T10:00:00+08:00", "amount_minor": 34000},
            {"seq": 3, "due_at": "2026-12-26T10:00:00+08:00", "amount_minor": 34000},
        ]
        self.assertTrue(open_order(self.flow).ok)
        self.assertTrue(show_offer(self.flow, repayment_schedule=schedule).ok)
        self.assertTrue(assess(self.flow).ok)
        for decision in confirm_all_items(self.flow):
            self.assertTrue(decision.ok)
        self.assertTrue(confirm_credit(self.flow).ok)
        # 窗口内先出具第一期账单（历史账单）
        statement = self.flow.generate_statement(
            "cmd-stmt-1", order_id=ORDER_ID, seq=1,
            occurred_at="2026-10-06T20:00:00+08:00",
        )
        self.assertTrue(statement.ok)
        # 窗口内（合同 10-08 10:04 前可撤）撤回：第一期已到期保留，第二、三期取消
        revoked = self.flow.revoke_consent(
            "cmd-revoke-1", order_id=ORDER_ID,
            occurred_at="2026-10-08T09:00:00+08:00",
        )
        self.assertTrue(revoked.ok)
        facts = revoked.events[0]["facts"]
        self.assertEqual([2, 3], facts["cancelled_schedule_seqs"])
        self.assertEqual([1], facts["preserved_statement_seqs"])
        self.assertEqual(COLLECTOR_ID, facts["collection_party_id"])
        state = self.flow.order_state(ORDER_ID)
        self.assertEqual("revoked", state.contract_status)
        status_by_seq = {entry["seq"]: entry["status"] for entry in state.schedule}
        self.assertEqual(
            {1: "scheduled", 2: "cancelled", 3: "cancelled"}, status_by_seq
        )
        # 历史账单仍可审计
        self.assertEqual([1], sorted(state.statements))
        self.assertEqual(COLLECTOR_ID, state.statements[1]["payee"])
        # 撤回后不允许再出具新账单
        blocked = self.flow.generate_statement(
            "cmd-stmt-2", order_id=ORDER_ID, seq=1,
            occurred_at="2026-11-02T10:00:00+08:00",
        )
        self.assertFalse(blocked.ok)
        self.assertEqual("contract_not_active", blocked.reason)

    def test_revoke_after_window_is_rejected(self) -> None:
        self.assertTrue(open_offer_assess_confirm(self.flow).ok)
        expired = self.flow.revoke_consent(
            "cmd-revoke-late", order_id=ORDER_ID,
            occurred_at="2026-10-09T10:00:00+08:00",
        )
        self.assertFalse(expired.ok)
        self.assertEqual("revocation_window_expired", expired.reason)

    def test_early_settlement_closes_followups_but_keeps_history(self) -> None:
        self.assertTrue(open_offer_assess_confirm(self.flow).ok)
        wrong = self.flow.settle_early(
            "cmd-settle-wrong", order_id=ORDER_ID, settled_amount_minor=1,
            occurred_at="2026-10-10T10:00:00+08:00",
        )
        self.assertFalse(wrong.ok)
        self.assertEqual("amount_mismatch", wrong.reason)
        self.assertEqual(102000, wrong.details["remaining_minor"])
        settled = self.flow.settle_early(
            "cmd-settle-1", order_id=ORDER_ID, settled_amount_minor=102000,
            occurred_at="2026-10-10T10:00:00+08:00",
        )
        self.assertTrue(settled.ok)
        state = self.flow.order_state(ORDER_ID)
        self.assertEqual("settled", state.contract_status)
        # 结清后不再产生到期提醒
        self.assertEqual([], self.flow.due_reminders("2026-10-25T10:00:00+08:00"))
        # 历史事件仍然完整可审计
        types = [e["event_type"] for e in self.flow.events_for_order(ORDER_ID)]
        self.assertIn("CONTRACT_ISSUED", types)
        self.assertIn("EARLY_SETTLED", types)

    def test_statement_rules_on_active_contract(self) -> None:
        self.assertTrue(open_offer_assess_confirm(self.flow).ok)
        duplicate = None
        first = self.flow.generate_statement(
            "cmd-stmt-a", order_id=ORDER_ID, seq=1,
            occurred_at="2026-10-20T10:00:00+08:00",
        )
        self.assertTrue(first.ok)
        duplicate = self.flow.generate_statement(
            "cmd-stmt-b", order_id=ORDER_ID, seq=1,
            occurred_at="2026-10-21T10:00:00+08:00",
        )
        self.assertFalse(duplicate.ok)
        self.assertEqual("statement_exists", duplicate.reason)
        unknown = self.flow.generate_statement(
            "cmd-stmt-c", order_id=ORDER_ID, seq=9,
            occurred_at="2026-10-21T10:00:00+08:00",
        )
        self.assertFalse(unknown.ok)
        self.assertEqual("unknown_schedule_seq", unknown.reason)


class OfferValidationTests(FlowTestCase):
    def test_display_copy_must_cover_required_items(self) -> None:
        self.assertTrue(open_order(self.flow).ok)
        broken = dict(DISPLAY_COPY, blocks=DISPLAY_COPY["blocks"][:2])
        decision = show_offer(self.flow, display_copy=broken)
        self.assertFalse(decision.ok)
        self.assertEqual("display_incomplete", decision.reason)
        self.assertEqual(["contract_parties", "revocation_window"], decision.details["missing_blocks"])

    def test_principal_must_match_payable(self) -> None:
        self.assertTrue(open_order(self.flow).ok)
        decision = show_offer(self.flow, terms=dict(TERMS, principal_minor=1))
        self.assertFalse(decision.ok)
        self.assertEqual("principal_mismatch", decision.reason)

    def test_schedule_must_sum_to_total_cost(self) -> None:
        self.assertTrue(open_order(self.flow).ok)
        bad_schedule = [dict(entry) for entry in REPAYMENT_SCHEDULE]
        bad_schedule[0]["amount_minor"] = 1
        decision = show_offer(self.flow, repayment_schedule=bad_schedule)
        self.assertFalse(decision.ok)
        self.assertEqual("schedule_mismatch", decision.reason)

    def test_invalid_occurred_at_is_rejected(self) -> None:
        decision = open_order(self.flow, occurred_at="2026-10-06 10:00")
        self.assertFalse(decision.ok)
        self.assertEqual("invalid_occurred_at", decision.reason)


class DisputeTests(FlowTestCase):
    def test_dispute_lifecycle(self) -> None:
        self.assertTrue(open_offer_assess_confirm(self.flow).ok)
        opened = self.flow.open_dispute(
            "cmd-dispute-1", dispute_id="dispute-1", order_id=ORDER_ID,
            raised_by="customer", reason="未注意到同时开通了信贷",
            occurred_at="2026-10-07T09:00:00+08:00",
        )
        self.assertTrue(opened.ok)
        advanced = self.flow.advance_dispute(
            "cmd-dispute-2", dispute_id="dispute-1", stage="evidence",
            occurred_at="2026-10-07T10:00:00+08:00",
        )
        self.assertTrue(advanced.ok)
        resolved = self.flow.resolve_dispute(
            "cmd-dispute-3", dispute_id="dispute-1", outcome="refund_and_close",
            occurred_at="2026-10-08T10:00:00+08:00",
        )
        self.assertTrue(resolved.ok)
        closed = self.flow.advance_dispute(
            "cmd-dispute-4", dispute_id="dispute-1", stage="evidence",
            occurred_at="2026-10-08T11:00:00+08:00",
        )
        self.assertFalse(closed.ok)
        self.assertEqual("dispute_closed", closed.reason)
        dispute = self.flow.dispute_state("dispute-1")
        self.assertTrue(dispute.resolved)
        self.assertEqual(["opened", "evidence", "resolved"], [h["stage"] for h in dispute.history])


if __name__ == "__main__":
    unittest.main()
