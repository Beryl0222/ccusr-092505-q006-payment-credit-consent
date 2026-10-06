from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from payment_credit_consent.service import ConsentLedger
from payment_credit_consent.views import ROLES, project_for_role

FEE = {"principal": 1200.0, "interest": 36.0, "service_fee": 0.0, "total_cost": 1236.0, "apr": 0.12}
DISPLAY = {
    "display_version": "page-v1",
    "copies": [{"id": "cost_notice", "text": "分 3 期，总成本 1236 元"}],
    "risk_disclosures": ["逾期将产生罚息并影响征信"],
}
CONSENT = {
    "comprehensive_cost": True,
    "risk_disclosure": True,
    "contract_parties": True,
    "revocation_window": True,
}


class ViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        clock = lambda: datetime(2026, 10, 6, 10, 0, tzinfo=timezone(timedelta(hours=8)))  # noqa: E731
        self.ledger = ConsentLedger.open(Path(self._tmp.name), clock=clock)
        self.ledger.create_order("r-1", "o-1", items=[{"sku": "手机", "price": 1200.0, "qty": 1}], amount=1200.0)
        self.ledger.show_offer(
            "r-2",
            "o-1",
            fee_breakdown=dict(FEE),
            risk_level="low",
            lender={"id": "bank-01", "name": "示例银行"},
            funding_party="bank-01",
            collection_party="bank-01-collection",
            display=dict(DISPLAY),
        )
        self.ledger.review_suitability("r-3", "o-1", outcome="approved", reasons=["评分通过"])
        self.ledger.confirm(
            "r-4",
            "o-1",
            offer_version=1,
            display_version="page-v1",
            consent_items=dict(CONSENT),
            confirmed_by="user-9",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_lender_cannot_see_cart_items(self) -> None:
        view = self.ledger.view("lender", "o-1")
        self.assertNotIn("items", view["order"])
        self.assertEqual(1200.0, view["order"]["amount"])
        self.assertIn("suitability_detail", view["offer"])

    def test_platform_and_service_cannot_see_assessment_internals(self) -> None:
        for role in ("platform", "customer_service"):
            view = self.ledger.view(role, "o-1")
            self.assertNotIn("suitability_detail", view["offer"])
            self.assertNotIn("disbursement", view["credit"])

    def test_customer_service_sees_display_and_parties(self) -> None:
        view = self.ledger.view("customer_service", "o-1")
        self.assertEqual("page-v1", view["offer"]["display"]["display_version"])
        self.assertEqual("bank-01", view["offer"]["funding_party"])
        self.assertEqual("bank-01-collection", view["offer"]["collection_party"])
        self.assertTrue(view["consent"]["items"]["risk_disclosure"])

    def test_regulator_sees_everything(self) -> None:
        view = self.ledger.view("regulator", "o-1")
        self.assertIn("suitability_detail", view["offer"])
        self.assertIn("disbursement", view["credit"])
        self.assertIn("items", view["order"])

    def test_unknown_role_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.ledger.view("ghost", "o-1")

    def test_project_for_role_does_not_mutate_full_view(self) -> None:
        full = {"order": {"items": [1], "amount": 2}, "offer": None}
        trimmed = project_for_role("lender", full)
        self.assertEqual({"items": [1], "amount": 2}, full["order"])
        self.assertNotIn("items", trimmed["order"])
        self.assertIsNone(trimmed["offer"])

    def test_roles_cover_the_four_parties(self) -> None:
        self.assertEqual(("platform", "lender", "customer_service", "regulator"), ROLES)


if __name__ == "__main__":
    unittest.main()
