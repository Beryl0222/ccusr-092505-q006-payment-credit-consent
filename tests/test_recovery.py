from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from helpers import (
    ORDER_ID,
    SCHEMA,
    assess,
    confirm_all_items,
    make_flow,
    open_offer_assess_confirm,
    open_order,
    show_offer,
)
from payment_credit_consent.flow import ConsentFlow


class RecoveryTests(unittest.TestCase):
    """服务重启后：未完成的确认、人工复核、到期提醒都能继续。"""

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.store_dir = Path(self.dir.name)
        flow = make_flow(self.store_dir)
        # 订单一：报价与逐项确认已完成，等待综合确认
        self.assertTrue(open_order(flow, order_id=ORDER_ID).ok)
        self.assertTrue(show_offer(flow, order_id=ORDER_ID).ok)
        self.assertTrue(assess(flow, order_id=ORDER_ID).ok)
        for decision in confirm_all_items(flow, order_id=ORDER_ID):
            self.assertTrue(decision.ok)
        # 订单二：信贷确认完成，第一期 2026-10-26 到期
        self.assertTrue(open_offer_assess_confirm(flow, order_id="order-2").ok)
        # 订单三：评估异常，等待人工复核
        self.assertTrue(open_order(flow, order_id="order-3", command_id="cmd-open-3").ok)
        self.assertTrue(show_offer(flow, order_id="order-3", command_id="cmd-offer-3").ok)
        self.assertTrue(
            assess(flow, order_id="order-3", command_id="cmd-assess-3",
                   risk_level="high", anomalies=["device_mismatch"]).ok
        )
        self.flow = flow

    def tearDown(self) -> None:
        self.dir.cleanup()

    def restart(self) -> ConsentFlow:
        return ConsentFlow.restart(self.store_dir, SCHEMA)

    def test_pending_confirmations_survive_restart(self) -> None:
        flow = self.restart()
        pending = {item["order_id"]: item for item in flow.pending_confirmations()}
        self.assertIn(ORDER_ID, pending)
        self.assertIn("order-3", pending)
        self.assertNotIn("order-2", pending)
        # 订单一的逐项确认在重启前完成，重启后不缺任何条目
        self.assertEqual([], pending[ORDER_ID]["missing_items"])

    def test_due_reminders_survive_restart(self) -> None:
        flow = self.restart()
        reminders = flow.due_reminders("2026-10-25T10:00:00+08:00")
        self.assertEqual(1, len(reminders))
        reminder = reminders[0]
        self.assertEqual("order-2", reminder["order_id"])
        self.assertEqual(1, reminder["seq"])
        self.assertEqual(34000, reminder["amount_minor"])
        self.assertEqual("collector-1", reminder["payee"])
        # 窗口拉远后三期全部出现
        later = flow.due_reminders("2026-10-25T10:00:00+08:00", within_hours=24 * 90)
        self.assertEqual([1, 2, 3], [item["seq"] for item in later])

    def test_pending_manual_reviews_survive_restart(self) -> None:
        flow = self.restart()
        reviews = flow.pending_manual_reviews()
        self.assertEqual(["order-3"], [item["order_id"] for item in reviews])
        self.assertEqual(["device_mismatch"], reviews[0]["anomalies"])

    def test_retry_after_restart_returns_original_decision(self) -> None:
        flow = self.restart()
        retry = open_order(flow, order_id=ORDER_ID)
        self.assertTrue(retry.ok)
        original = self.flow.events_for_order(ORDER_ID)[0]
        self.assertEqual([original["event_id"]], [e["event_id"] for e in retry.events])
        # 没有追加新事件
        self.assertEqual(
            len(self.flow.events_for_order(ORDER_ID)),
            len(flow.events_for_order(ORDER_ID)),
        )

    def test_unfinished_flow_continues_after_restart(self) -> None:
        flow = self.restart()
        confirmed = flow.confirm_checkout(
            "cmd-confirm-after-restart",
            order_id=ORDER_ID,
            pay_with_credit=True,
            offer_version=1,
            risk_level="low",
            occurred_at="2026-10-06T12:00:00+08:00",
        )
        self.assertTrue(confirmed.ok)
        resolved = flow.resolve_manual_review(
            "cmd-review-after-restart",
            order_id="order-3",
            decision="approved",
            reviewer="reviewer-1",
            occurred_at="2026-10-06T12:30:00+08:00",
        )
        self.assertTrue(resolved.ok)
        # 再次重启，状态依旧完整
        again = self.restart()
        self.assertEqual("paid", again.order_state(ORDER_ID).order_status)
        self.assertEqual([], again.pending_manual_reviews())


if __name__ == "__main__":
    unittest.main()
