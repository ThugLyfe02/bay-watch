import tempfile
import unittest
from threading import Barrier
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from auction_scout.models import Listing, SaleComp, Valuation
from auction_scout.store import Store
from auction_scout.valuation import analyze, estimate_value, screen_listings
from auction_scout.worker import submit_due_intents


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.listing = Listing("ebay:1", "Pokemon Umbreon holo PSA 7", "https://example.test/1", "USD",
                               Decimal("100"), self.now + timedelta(minutes=5), Decimal("10"), "graded", 1000, Decimal("99.9"))

    def test_estimator_uses_similar_recent_comps_and_marks_thin_data(self):
        comps = [SaleComp("Pokemon Umbreon holo PSA 7", self.now - timedelta(days=i), Decimal("180") + i, condition="graded") for i in (1, 3, 5)]
        value = estimate_value(self.listing, comps, now=self.now)
        self.assertIsNotNone(value)
        self.assertEqual(value.comp_count, 3)
        self.assertEqual(value.confidence, "low")
        self.assertGreater(value.fair_value, Decimal("180"))

    def test_estimator_never_mixes_currency_comp_prices(self):
        comp = SaleComp(self.listing.title, self.now, Decimal("20000"), condition="graded", currency="JPY")
        self.assertIsNone(estimate_value(self.listing, [comp], now=self.now))

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

    def test_unverified_seller_risk_reduces_ceiling_and_blocks_eligibility(self):
        listing = Listing("risk", self.listing.title, self.listing.url, "USD", Decimal("50"),
                          self.listing.end_time, Decimal("0"), "graded")
        value = Valuation(Decimal("200"), Decimal("180"), Decimal("220"), 10, "medium")
        clean = analyze(self.listing, value, reserve_buffer=Decimal("0"))
        uncertain = analyze(listing, value, reserve_buffer=Decimal("0"))
        self.assertEqual(uncertain.risk_discount, Decimal("0.08"))
        self.assertLess(uncertain.max_hammer, clean.max_hammer)
        self.assertFalse(uncertain.eligible)
        self.assertIn("seller_data_unverified", uncertain.risk_flags)

    def test_store_rejects_second_armed_intent_and_concurrent_transition(self):
        with tempfile.NamedTemporaryFile() as f:
            store = Store(f.name)
            store.save_listing(self.listing)
            store.set_budget("USD", Decimal("400"))
            store.arm_intent(self.listing.item_id, Decimal("150"), "USD", self.listing.end_time, 8)
            with self.assertRaises(ValueError): store.arm_intent(self.listing.item_id, Decimal("160"), "USD", self.listing.end_time, 8)
            row = store.armed_intents()[0]
            store.transition_intent(row["id"], "armed", "submitting", {})
            with self.assertRaisesRegex(ValueError, "active or unresolved intent"):
                store.arm_intent(self.listing.item_id, Decimal("160"), "USD", self.listing.end_time, 8)
            with self.assertRaises(ValueError): store.transition_intent(row["id"], "armed", "submitting", {})
            store.close()

    def test_worker_submits_simultaneous_closes_in_parallel(self):
        class ParallelClient:
            def __init__(self, end_time):
                self.barrier = Barrier(2)
                self.calls = []
                self.end_time = end_time

            def get_item(self, item_id):
                return {"itemId": item_id, "buyingOptions": ["AUCTION"],
                        "itemEndDate": self.end_time.isoformat(),
                        "minimumPriceToBid": {"currency": "USD", "value": "100"},
                        "currentBidPrice": {"currency": "USD", "value": "90"}}

            def place_proxy_bid(self, item_id, amount, currency):
                self.calls.append((item_id, amount, currency))
                self.barrier.wait(timeout=2)
                return {"status": "accepted"}

        with tempfile.NamedTemporaryFile() as f:
            store = Store(f.name)
            store.set_budget("USD", Decimal("400"))
            start = datetime.now(timezone.utc)
            end_time = start + timedelta(seconds=50)
            for item_id in ("ebay:parallel-1", "ebay:parallel-2"):
                listing = Listing(item_id, self.listing.title, f"https://example.test/{item_id}", "USD",
                                  Decimal("100"), end_time, Decimal("0"), "graded")
                store.save_listing(listing)
                store.arm_intent(item_id, Decimal("150"), "USD", end_time, 30)

            client = ParallelClient(end_time)
            submitted, unknown = submit_due_intents(store, client, concurrency=2, now=start + timedelta(seconds=21))
            self.assertEqual((submitted, unknown), (2, 0))
            self.assertEqual(len(client.calls), 2)
            self.assertEqual([row["status"] for row in store.db.execute("SELECT status FROM bid_intents ORDER BY id")],
                             ["submitted", "submitted"])
            store.close()

    def test_worker_preflight_blocks_when_live_minimum_exceeds_hard_cap(self):
        class StalePriceClient:
            def __init__(self, end_time):
                self.end_time = end_time
                self.bid_calls = 0

            def get_item(self, item_id):
                return {"itemId": item_id, "buyingOptions": ["AUCTION"],
                        "itemEndDate": self.end_time.isoformat(),
                        "minimumPriceToBid": {"currency": "USD", "value": "160"},
                        "currentBidPrice": {"currency": "USD", "value": "150"}}

            def place_proxy_bid(self, *args):
                self.bid_calls += 1
                raise AssertionError("preflight should prevent the bid request")

        with tempfile.NamedTemporaryFile() as f:
            store = Store(f.name)
            start = datetime.now(timezone.utc)
            end_time = start + timedelta(seconds=50)
            listing = Listing("ebay:stale", self.listing.title, "https://example.test/stale", "USD",
                              Decimal("100"), end_time, Decimal("0"), "graded")
            store.save_listing(listing)
            store.set_budget("USD", Decimal("200"))
            store.arm_intent(listing.item_id, Decimal("155"), "USD", end_time, 30)
            client = StalePriceClient(end_time)

            submitted, unknown = submit_due_intents(store, client, now=start + timedelta(seconds=21))

            self.assertEqual((submitted, unknown), (0, 0))
            self.assertEqual(client.bid_calls, 0)
            self.assertEqual(store.db.execute("SELECT status FROM bid_intents").fetchone()["status"], "failed")
            store.close()

    def test_worker_preflight_blocks_listing_identity_mismatch(self):
        class WrongItemClient:
            def __init__(self, end_time):
                self.end_time = end_time
                self.bid_calls = 0

            def get_item(self, item_id):
                return {"itemId": "different-item", "buyingOptions": ["AUCTION"],
                        "itemEndDate": self.end_time.isoformat(),
                        "minimumPriceToBid": {"currency": "USD", "value": "100"}}

            def place_proxy_bid(self, *args):
                self.bid_calls += 1
                raise AssertionError("preflight should prevent the bid request")

        with tempfile.NamedTemporaryFile() as f:
            store = Store(f.name)
            start = datetime.now(timezone.utc)
            end_time = start + timedelta(seconds=50)
            listing = Listing("ebay:expected", self.listing.title, "https://example.test/expected", "USD",
                              Decimal("100"), end_time, Decimal("0"), "graded")
            store.save_listing(listing)
            store.set_budget("USD", Decimal("200"))
            store.arm_intent(listing.item_id, Decimal("150"), "USD", end_time, 30)
            client = WrongItemClient(end_time)

            submitted, unknown = submit_due_intents(store, client, now=start + timedelta(seconds=21))

            self.assertEqual((submitted, unknown), (0, 0))
            self.assertEqual(client.bid_calls, 0)
            self.assertEqual(store.db.execute("SELECT status FROM bid_intents").fetchone()["status"], "failed")
            store.close()

    def test_portfolio_budget_reserves_full_bid_max_and_blocks_overcommit(self):
        with tempfile.NamedTemporaryFile() as f:
            store = Store(f.name)
            store.save_listing(self.listing)
            store.set_budget("USD", Decimal("200"))
            store.arm_intent(self.listing.item_id, Decimal("150"), "USD", self.listing.end_time, 8)
            status = store.budget_status("USD")
            self.assertEqual(status["reserved"], Decimal("150.0"))
            self.assertEqual(status["available"], Decimal("50.0"))
            second = Listing("ebay:2", "Pokemon Vaporeon holo PSA 9", "https://example.test/2", "USD",
                             Decimal("20"), self.now + timedelta(minutes=8), Decimal("0"), "graded")
            store.save_listing(second)
            with self.assertRaisesRegex(ValueError, "exposure cap exceeded"):
                store.arm_intent(second.item_id, Decimal("60"), "USD", second.end_time, 8)
            with self.assertRaisesRegex(ValueError, "cannot set cap below"):
                store.set_budget("USD", Decimal("100"))
            store.close()

    def test_zero_decimal_currency_rounds_caps_to_minor_unit(self):
        listing = Listing("jpy", "Pokemon Umbreon", "https://example.test/jpy", "JPY", Decimal("1000"),
                          self.now + timedelta(hours=2), Decimal("100"), "graded", 500, Decimal("99.5"))
        value = Valuation(Decimal("20000"), Decimal("20000"), Decimal("20000"), 20, "high")
        opportunity = analyze(listing, value, reserve_buffer=Decimal("0"))
        self.assertEqual(opportunity.all_in_cap, Decimal("20000"))
        self.assertEqual(opportunity.max_hammer, Decimal("19900"))

    def test_screen_ranks_eligible_headroom_and_excludes_expired_or_unmatched(self):
        now = self.now
        good = Listing("good", "Pokemon Umbreon holo PSA 7", "https://example.test/good", "USD",
                       Decimal("50"), now + timedelta(hours=3), Decimal("0"), "graded", 400, Decimal("99.5"))
        expired = Listing("expired", good.title, "https://example.test/expired", "USD", Decimal("50"),
                          now - timedelta(seconds=1), Decimal("0"), "graded")
        comps = [SaleComp(good.title, now - timedelta(days=i), Decimal("200"), condition="graded") for i in range(1, 7)]
        results = screen_listings([expired, good], comps, reserve_buffer=Decimal("0"), now=now)
        self.assertEqual([x.listing.item_id for x in results], ["good"])
        self.assertTrue(results[0].eligible)
        self.assertGreater(results[0].bid_headroom, Decimal(0))


if __name__ == "__main__": unittest.main()

