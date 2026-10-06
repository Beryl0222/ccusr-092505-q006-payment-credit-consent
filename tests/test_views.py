from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from helpers import ORDER_ID, assess, make_flow, open_offer_assess_confirm, open_order, show_offer
from payment_credit_consent.views import project_event, project_events


def events_by_type(events, event_type):
    return next(event for event in events if event["event_type"] == event_type)


class ViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.flow = make_flow(Path(self.dir.name))
        self.assertTrue(open_offer_assess_confirm(self.flow).ok)
        # 追加一条带异常的评估与复核，制造敏感字段
        self.assertTrue(open_order(self.flow, order_id="order-2", command_id="cmd-open-2").ok)
        self.assertTrue(show_offer(self.flow, order_id="order-2", command_id="cmd-offer-2").ok)
        self.assertTrue(
            assess(self.flow, order_id="order-2", command_id="cmd-assess-2",
                   risk_level="high", anomalies=["income_gap"]).ok
        )
        self.events = self.flow.events_for_order(ORDER_ID) + self.flow.events_for_order("order-2")

    def tearDown(self) -> None:
        self.dir.cleanup()

    def test_regulator_sees_everything(self) -> None:
        projected = project_events(self.events, "regulator")
        for original, view in zip(self.events, projected):
            self.assertEqual(original["facts"], view["facts"])
            self.assertEqual([], view["redacted_fields"])

    def test_platform_cannot_see_assessment_internals_or_schedule(self) -> None:
        projected = project_events(self.events, "platform")
        suitability = events_by_type(projected, "SUITABILITY_REVIEWED")
        self.assertNotIn("anomalies", suitability["facts"])
        self.assertNotIn("decided_by", suitability["facts"])
        self.assertIn("risk_level", suitability["facts"])
        self.assertEqual(["anomalies", "decided_by"], suitability["redacted_fields"])
        contract = events_by_type(projected, "CONTRACT_ISSUED")
        self.assertNotIn("schedule", contract["facts"])
        self.assertIn("lender_party_id", contract["facts"])

    def test_lender_cannot_see_cart_items_or_display_copy(self) -> None:
        projected = project_events(self.events, "lender")
        opened = events_by_type(projected, "ORDER_OPENED")
        self.assertNotIn("items", opened["facts"])
        self.assertIn("payable_minor", opened["facts"])
        shown = events_by_type(projected, "OFFER_SHOWN")
        self.assertNotIn("display_copy", shown["facts"])
        self.assertIn("terms", shown["facts"])

    def test_customer_service_sees_display_but_not_risk_details(self) -> None:
        projected = project_events(self.events, "customer_service")
        shown = events_by_type(projected, "OFFER_SHOWN")
        # 客服能拿出用户当时看到的综合成本与风险提示
        block_keys = {block["key"] for block in shown["facts"]["display_copy"]["blocks"]}
        self.assertIn("comprehensive_cost", block_keys)
        self.assertIn("risk_warning", block_keys)
        self.assertIn("lender", {p["role"] for p in shown["facts"]["parties"]})
        suitability = events_by_type(projected, "SUITABILITY_REVIEWED")
        self.assertNotIn("risk_level", suitability["facts"])
        self.assertNotIn("anomalies", suitability["facts"])
        review = events_by_type(projected, "MANUAL_REVIEW_RESOLVED") if any(
            e["event_type"] == "MANUAL_REVIEW_RESOLVED" for e in projected
        ) else None
        self.assertIsNone(review)

    def test_projection_does_not_mutate_original(self) -> None:
        original = events_by_type(self.events, "SUITABILITY_REVIEWED")
        snapshot = dict(original["facts"])
        project_event(original, "platform")
        self.assertEqual(snapshot, original["facts"])

    def test_unknown_role_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            project_event(self.events[0], "intern")


if __name__ == "__main__":
    unittest.main()
