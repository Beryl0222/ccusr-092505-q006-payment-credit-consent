from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from payment_credit_consent.service import ConsentLedger

ITEMS = [{"sku": "手机", "price": 1200.0, "qty": 1}]
FEE = {"principal": 1200.0, "interest": 36.0, "service_fee": 0.0, "total_cost": 1236.0, "apr": 0.12}
DISPLAY = {
    "display_version": "page-v1",
    "copies": [
        {"id": "cost_notice", "text": "分 3 期，总成本 1236 元"},
        {"id": "risk_notice", "text": "逾期将产生罚息并影响征信"},
    ],
    "risk_disclosures": ["逾期将产生罚息并影响征信"],
}
CONSENT = {
    "comprehensive_cost": True,
    "risk_disclosure": True,
    "contract_parties": True,
    "revocation_window": True,
}
LENDER = {"id": "bank-01", "name": "示例银行"}


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.moment = start

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **kwargs) -> None:
        self.moment += timedelta(**kwargs)


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.clock = FakeClock(datetime(2026, 10, 6, 10, 0, tzinfo=timezone(timedelta(hours=8))))
        self.ledger = ConsentLedger.open(self.dir, clock=self.clock)
        self._seq = 0

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _req(self) -> str:
        self._seq += 1
        return f"req-{self._seq}"

    def _create_order(self, order_id: str = "o-1") -> None:
        decision = self.ledger.create_order(self._req(), order_id, items=ITEMS, amount=1200.0)
        self.assertTrue(decision.ok, decision.summary)

    def _show_offer(self, order_id: str = "o-1", **overrides):
        params = {
            "fee_breakdown": dict(FEE),
            "risk_level": "low",
            "lender": LENDER,
            "funding_party": "bank-01",
            "collection_party": "bank-01-collection",
            "display": dict(DISPLAY),
        }
        params.update(overrides)
        return self.ledger.show_offer(self._req(), order_id, **params)

    def _approve(self, order_id: str = "o-1") -> None:
        decision = self.ledger.review_suitability(self._req(), order_id, outcome="approved")
        self.assertTrue(decision.ok, decision.summary)

    def _confirm_credit(self, order_id: str = "o-1", offer_version: int = 1, request_id: str | None = None):
        return self.ledger.confirm(
            request_id or self._req(),
            order_id,
            offer_version=offer_version,
            display_version="page-v1",
            consent_items=dict(CONSENT),
            confirmed_by="user-9",
        )

    # 支付可以在没有信贷时完成
    def test_payment_completes_without_credit(self) -> None:
        self._create_order()
        decision = self.ledger.confirm(self._req(), "o-1", use_credit=False)
        self.assertTrue(decision.ok)
        self.assertEqual("payment_confirmed", decision.code)
        view = self.ledger.view("regulator", "o-1")
        self.assertEqual("paid", view["order"]["status"])
        self.assertEqual("balance", view["order"]["payment"]["method"])
        self.assertIsNone(view["offer"])

    # 信贷拒绝不能改变订单
    def test_credit_rejection_keeps_order_payable(self) -> None:
        self._create_order()
        self._show_offer()
        rejected = self.ledger.review_suitability(self._req(), "o-1", outcome="rejected", reasons=["评分不足"])
        self.assertTrue(rejected.ok)
        blocked = self._confirm_credit()
        self.assertFalse(blocked.ok)
        self.assertEqual("credit_rejected", blocked.code)
        view = self.ledger.view("regulator", "o-1")
        self.assertEqual("created", view["order"]["status"])
        paid = self.ledger.confirm(self._req(), "o-1", use_credit=False)
        self.assertTrue(paid.ok)
        self.assertEqual("paid", self.ledger.view("regulator", "o-1")["order"]["status"])

    # 同一请求重试必须返回原决定
    def test_retry_returns_original_decision_without_new_events(self) -> None:
        self._create_order()
        self._show_offer()
        self._approve()
        request_id = self._req()
        first = self._confirm_credit(request_id=request_id)
        self.assertTrue(first.ok)
        size = len(self.ledger._store)
        second = self._confirm_credit(request_id=request_id)
        self.assertTrue(second.replayed)
        self.assertEqual(first.code, second.code)
        self.assertEqual(
            [event["event_id"] for event in first.events],
            [event["event_id"] for event in second.events],
        )
        self.assertEqual(size, len(self.ledger._store))

    # 报价变化要求重新确认
    def test_quote_change_requires_reconfirmation(self) -> None:
        self._create_order()
        self._show_offer()
        changed = dict(FEE, interest=48.0, total_cost=1248.0)
        second = self._show_offer(fee_breakdown=changed)
        self.assertEqual("offer_shown", second.code)
        self.assertEqual(2, second.detail["offer_version"])
        stale = self._confirm_credit(offer_version=1)
        self.assertFalse(stale.ok)
        self.assertEqual("reconfirmation_required", stale.code)
        self._approve()
        fresh = self._confirm_credit(offer_version=2)
        self.assertTrue(fresh.ok)
        reconstruct = self.ledger.reconstruct("o-1")
        self.assertEqual("quote_changed", reconstruct["decisions"][0]["reason"])

    # 风险等级变化同样要求重新确认
    def test_risk_level_change_invalidates_old_consent(self) -> None:
        self._create_order()
        self._show_offer()
        second = self._show_offer(risk_level="high")
        self.assertEqual(2, second.detail["offer_version"])
        stale = self._confirm_credit(offer_version=1)
        self.assertEqual("reconfirmation_required", stale.code)
        reconstruct = self.ledger.reconstruct("o-1")
        reasons = [d["reason"] for d in reconstruct["decisions"] if d["kind"] == "offer_superseded"]
        self.assertEqual(["risk_level_changed"], reasons)

    # 报价未变化时不重复生成版本
    def test_identical_quote_is_not_shown_twice(self) -> None:
        self._create_order()
        self._show_offer()
        size = len(self.ledger._store)
        again = self._show_offer()
        self.assertEqual("offer_unchanged", again.code)
        self.assertEqual(1, again.detail["offer_version"])
        self.assertEqual(size, len(self.ledger._store))

    # 逐项确认缺一不可
    def test_incomplete_consent_items_are_rejected(self) -> None:
        self._create_order()
        self._show_offer()
        self._approve()
        consent = dict(CONSENT)
        del consent["risk_disclosure"]
        decision = self.ledger.confirm(
            self._req(), "o-1", offer_version=1, display_version="page-v1", consent_items=consent
        )
        self.assertFalse(decision.ok)
        self.assertEqual("consent_incomplete", decision.code)
        self.assertEqual(["risk_disclosure"], decision.detail["missing"])

    # 评估异常进入人工复核，复核通过前不能确认
    def test_manual_review_blocks_confirm_until_resolved(self) -> None:
        self._create_order()
        self._show_offer()
        flagged = self.ledger.review_suitability(
            self._req(), "o-1", outcome="manual_review", reasons=["收入证明缺失"]
        )
        self.assertEqual("suitability_manual_review", flagged.code)
        blocked = self._confirm_credit()
        self.assertEqual("manual_review_pending", blocked.code)
        pending = self.ledger.pending_work()
        self.assertEqual(["o-1"], [item["order_id"] for item in pending["manual_reviews"]])
        resolved = self.ledger.resolve_manual_review(self._req(), "o-1", reviewer="staff-7", approved=True)
        self.assertTrue(resolved.ok)
        confirmed = self._confirm_credit()
        self.assertTrue(confirmed.ok)

    def test_manual_review_rejection_keeps_order(self) -> None:
        self._create_order()
        self._show_offer()
        self.ledger.review_suitability(self._req(), "o-1", outcome="manual_review")
        self.ledger.resolve_manual_review(self._req(), "o-1", reviewer="staff-7", approved=False, note="材料不实")
        blocked = self._confirm_credit()
        self.assertEqual("credit_rejected", blocked.code)
        self.assertEqual("created", self.ledger.view("regulator", "o-1")["order"]["status"])

    # 撤销窗口
    def test_revoke_within_window_only_affects_future_actions(self) -> None:
        self._create_order()
        self._show_offer()
        self._approve()
        self._confirm_credit()
        self.clock.advance(hours=1)
        revoked = self.ledger.revoke_consent(self._req(), "o-1", reason="误操作")
        self.assertTrue(revoked.ok)
        view = self.ledger.view("regulator", "o-1")
        self.assertEqual("revoked", view["credit"]["status"])
        self.assertEqual("paid", view["order"]["status"])
        settle = self.ledger.early_settle(self._req(), "o-1")
        self.assertEqual("credit_not_active", settle.code)
        reconstruct = self.ledger.reconstruct("o-1")
        self.assertEqual("disbursement", reconstruct["fund_flow"][1]["kind"])
        self.assertEqual("revoked", reconstruct["consents"][-1]["kind"])

    def test_revoke_after_window_is_rejected(self) -> None:
        self._create_order()
        self._show_offer()
        self._approve()
        self._confirm_credit()
        self.clock.advance(hours=25)
        decision = self.ledger.revoke_consent(self._req(), "o-1")
        self.assertFalse(decision.ok)
        self.assertEqual("revocation_window_expired", decision.code)

    # 提前结清保留历史账单
    def test_early_settlement_keeps_history_auditable(self) -> None:
        self._create_order()
        self._show_offer()
        self._approve()
        self._confirm_credit()
        settled = self.ledger.early_settle(self._req(), "o-1")
        self.assertTrue(settled.ok)
        self.assertEqual(1236.0, settled.detail["settled_amount"])
        view = self.ledger.view("regulator", "o-1")
        self.assertEqual("settled", view["credit"]["status"])
        self.assertTrue(all(item["settled"] for item in view["credit"]["schedule"]))
        self.assertEqual(3, len(view["credit"]["schedule"]))
        self.assertEqual([], self.ledger.pending_work()["due_reminders"])
        kinds = [entry["kind"] for entry in self.ledger.reconstruct("o-1")["fund_flow"]]
        self.assertEqual(["payment", "disbursement", "early_settlement"], kinds)

    # 报价过期不能确认
    def test_expired_offer_cannot_be_confirmed(self) -> None:
        self._create_order()
        self._show_offer(valid_hours=1)
        self._approve()
        self.clock.advance(hours=2)
        decision = self._confirm_credit()
        self.assertEqual("offer_expired", decision.code)
        pending = self.ledger.pending_work()["pending_confirmations"]
        self.assertEqual([True], [item["expired"] for item in pending])

    # 重启后继续未完成的确认和到期提醒，幂等记录也保留
    def test_restart_recovers_pending_work_and_idempotency(self) -> None:
        self._create_order("o-pending")
        self._show_offer("o-pending")
        self._create_order("o-credit")
        self.ledger.show_offer(
            self._req(),
            "o-credit",
            fee_breakdown=dict(FEE),
            risk_level="low",
            lender=LENDER,
            funding_party="bank-01",
            collection_party="bank-01-collection",
            display=dict(DISPLAY),
        )
        self.ledger.review_suitability(self._req(), "o-credit", outcome="approved")
        confirm_request = self._req()
        first = self.ledger.confirm(
            confirm_request,
            "o-credit",
            offer_version=1,
            display_version="page-v1",
            consent_items=dict(CONSENT),
            confirmed_by="user-9",
        )
        self.assertTrue(first.ok)
        self.clock.advance(days=30)

        reopened = ConsentLedger.open(self.dir, clock=self.clock)
        work = reopened.pending_work()
        self.assertEqual(["o-pending"], [item["order_id"] for item in work["pending_confirmations"]])
        self.assertEqual(["o-credit"], [item["order_id"] for item in work["due_reminders"]])
        self.assertEqual(1, work["due_reminders"][0]["seq"])
        self.assertEqual("bank-01-collection", work["due_reminders"][0]["collection_party"])

        replay = reopened.confirm(
            confirm_request,
            "o-credit",
            offer_version=1,
            display_version="page-v1",
            consent_items=dict(CONSENT),
            confirmed_by="user-9",
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(first.code, replay.code)
        self.assertEqual(
            [event["event_id"] for event in first.events],
            [event["event_id"] for event in replay.events],
        )

    # 争议阶段只能顺序前进，监管可还原处理进度
    def test_dispute_stages_advance_in_order(self) -> None:
        self._create_order()
        self.ledger.confirm(self._req(), "o-1", use_credit=False)
        opened = self.ledger.open_dispute(self._req(), "o-1", topic="未告知即开通信贷", opened_by="user-9")
        dispute_id = opened.detail["dispute_id"]
        backward = self.ledger.advance_dispute(self._req(), dispute_id, stage="mediating", handler="staff-1")
        self.assertFalse(backward.ok)
        self.assertEqual("invalid_stage_transition", backward.code)
        self.ledger.advance_dispute(self._req(), dispute_id, stage="investigating", handler="staff-1")
        self.ledger.advance_dispute(self._req(), dispute_id, stage="mediating", handler="staff-2")
        dispute = self.ledger.reconstruct("o-1")["dispute"]
        self.assertEqual("mediating", dispute["stage"])
        self.assertEqual(["accepted", "investigating", "mediating"], [h["stage"] for h in dispute["history"]])
        again = self.ledger.open_dispute(self._req(), "o-1", topic="重复立案", opened_by="user-9")
        self.assertEqual("dispute_already_open", again.code)

    # 监管还原：展示了什么、谁决定、资金流向谁
    def test_regulator_reconstruction_covers_display_decision_and_funds(self) -> None:
        self._create_order()
        self._show_offer()
        changed = dict(FEE, interest=48.0, total_cost=1248.0)
        self._show_offer(fee_breakdown=changed, display=dict(DISPLAY, display_version="page-v2"))
        self.ledger.review_suitability(self._req(), "o-1", outcome="approved", decided_by="system:rule-v3")
        self.ledger.confirm(
            self._req(),
            "o-1",
            offer_version=2,
            display_version="page-v2",
            consent_items=dict(CONSENT),
            confirmed_by="user-9",
        )
        fact = self.ledger.reconstruct("o-1")
        self.assertEqual([1, 2], [item["offer_version"] for item in fact["displayed"]])
        self.assertEqual("page-v1", fact["displayed"][0]["display"]["display_version"])
        self.assertEqual(1248.0, fact["displayed"][1]["fee_breakdown"]["total_cost"])
        deciders = {(d["kind"], d.get("decided_by")) for d in fact["decisions"]}
        self.assertIn(("suitability", "system:rule-v3"), deciders)
        self.assertIn(("consent", "user-9"), deciders)
        payment, disbursement = fact["fund_flow"]
        self.assertEqual("platform_merchant", payment["payee"])
        self.assertEqual("bank-01", payment["funded_by"])
        self.assertEqual("bank-01", disbursement["funding_party"])
        self.assertEqual("bank-01", fact["parties"]["funding_party"])
        self.assertEqual("bank-01-collection", fact["parties"]["collection_party"])
        self.assertIsNone(fact["dispute"])


if __name__ == "__main__":
    unittest.main()
