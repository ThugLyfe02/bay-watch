import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from auction_scout.models import Listing, SaleComp, Valuation
from auction_scout.store import Store
from auction_scout.valuation import analyze, estimate_value


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.listing = Listing("ebay:1", "Pokemon Umbreon holo PSA 7", "https://example.test/1", "USD",
                               Decimal("100"), self.now + timedelta(minutes=5), Decimal("10"), "graded")

    def test_estimator_uses_similar_recent_comps_and_marks_thin_data(self):
        comps = [SaleComp("Pokemon Umbreon holo PSA 7", self.now - timedelta(days=i), Decimal("180") + i, condition="graded") for i in (1, 3, 5)]
        value = estimate_value(self.listing, comps, now=self.now)
        self.assertIsNotNone(value)
        self.assertEqual(value.comp_count, 3)
        self.assertEqual(value.confidence, "low")
        self.assertGreater(value.fair_value, Decimal("180"))

    def test_estimator_rejects_wrong_card_grade_language_and_variant(self):
        listing = Listing("ebay:2", "Pokemon Umbreon holo PSA 7 Japanese #197", "https://example.test/2", "USD",
                          Decimal("100"), self.now + timedelta(minutes=5), Decimal("10"), "graded")
        comps = [
            SaleComp("Pokemon Umbreon holo PSA 8 #197 Japanese", self.now, Decimal("400"), condition="graded"),
            SaleComp("Pokemon Umbreon holo BGS 7 #197 Japanese", self.now, Decimal("100"), condition="graded"),
            SaleComp("Pokemon Umbreon holo PSA 7 #197 English", self.now, Decimal("100"), condition="graded"),
            SaleComp("Pokemon Umbreon reverse holo PSA 7 #197 Japanese", self.now, Decimal("100"), condition="graded"),
            SaleComp("Pokemon Umbreon holo PSA 7 #197 Japanese", self.now, Decimal("210"), condition="graded"),
        ]
        value = estimate_value(listing, comps, now=self.now)
        self.assertEqual(value.comp_count, 1)
        self.assertEqual(value.fair_value, Decimal("210"))

    def test_all_in_ceiling_respects_shipping_tax_fee_and_buffer(self):
        value = Valuation(Decimal("200"), Decimal("180"), Decimal("220"), 10, "medium")
        opportunity = analyze(self.listing, value, tax_rate=Decimal("0.08"), buyer_fee_rate=Decimal("0.05"), reserve_buffer=Decimal("0.1"))
        self.assertEqual(opportunity.all_in_cap, Decimal("162.00"))
        self.assertEqual(opportunity.max_hammer, Decimal("133.33"))
        self.assertLess(opportunity.max_hammer + self.listing.shipping, opportunity.all_in_cap)

    def test_store_rejects_second_armed_intent_and_concurrent_transition(self):
        with tempfile.NamedTemporaryFile() as f:
            store = Store(f.name)
            store.save_listing(self.listing)
            store.arm_intent(self.listing.item_id, Decimal("150"), "USD", self.listing.end_time, 8)
            with self.assertRaises(ValueError): store.arm_intent(self.listing.item_id, Decimal("160"), "USD", self.listing.end_time, 8)
            row = store.armed_intents()[0]
            store.transition_intent(row["id"], "armed", "submitting", {})
            with self.assertRaises(ValueError): store.transition_intent(row["id"], "armed", "submitting", {})
            store.close()


if __name__ == "__main__": unittest.main()

