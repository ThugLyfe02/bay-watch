from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from io import StringIO

from auction_scout.calibration import calibration_report
from auction_scout.models import Listing
from auction_scout.store import SCHEMA, Store


class LearningLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "bay-watch.db")
        self.store = Store(self.path)
        self.listing()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def listing(self, item_id="item-1", currency="USD"):
        self.store.save_listing(Listing(item_id, "Test card", "https://example.invalid/item", currency,
            Decimal("10"), datetime.now(timezone.utc) + timedelta(hours=2), Decimal("2"), "Used"))

    def prediction(self, item_id="item-1", at="2026-01-01T00:00:00+00:00", value="100"):
        return self.store.record_prediction(item_id, {"prediction_at": at, "currency": "USD",
            "fair_value": value, "value_low": "80", "value_high": "120", "confidence": "medium", "listing": {"title": "Test card"},
            "comp_count": 4, "comps": [{"title": "Test card", "sold_at": "2025-12-30T00:00:00+00:00",
              "price": "90", "shipping": "0", "currency": "USD"},
              {"title": "Test card", "sold_at": "2026-01-03T00:00:00+00:00",
              "price": "9999", "shipping": "0", "currency": "USD"}]}, decision="watch", policy_version="v1")

    def test_existing_database_migrates_without_losing_rows(self):
        self.store.close()
        db = sqlite3.connect(self.path)
        db.executescript(SCHEMA)
        db.execute("INSERT INTO listings VALUES(?,?,?,?,?,?,?,?,?,?)", ("old", "Old", "url", "USD", "1", "0",
            "2026-01-01T00:00:00+00:00", "unknown", "{}", "2025-01-01T00:00:00+00:00"))
        db.commit(); db.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.schema_version(), 4)
        self.assertIsNotNone(self.store.get_listing("old"))
        self.assertIn("prediction_id", {r["name"] for r in self.store.db.execute("PRAGMA table_info(bid_intents)")})

    def test_predictions_are_idempotent_and_immutable(self):
        first = self.prediction()
        self.assertEqual(first, self.prediction())
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("UPDATE prediction_snapshots SET fair_value='999' WHERE id=?", (first,))

    def test_calibration_uses_only_linked_later_verified_reference(self):
        pred_id = self.prediction()
        # An earlier observation is rejected, avoiding temporal leakage.
        with self.assertRaises(ValueError):
            self.store.record_learning_event("item-1", event_type="market_reference", state="verified",
                source="marketplace", evidence_ref="sale-early", prediction_id=pred_id, currency="USD",
                amount=Decimal("90"), observed_at="2025-12-31T23:59:00+00:00")
        self.store.record_learning_event("item-1", event_type="market_reference", state="verified",
            source="user_import", evidence_ref="completed-sale-1", prediction_id=pred_id, currency="USD",
            amount=Decimal("90"), observed_at="2026-01-02T00:00:00+00:00")
        report = calibration_report(self.store)
        self.assertEqual(report["sample_count"], 1)
        self.assertEqual(report["mean_absolute_error"], "10")
        self.assertEqual(report["uncertainty_range_coverage"], 1.0)
        self.assertEqual(report["warning"], "Insufficient evidence for conclusions")
        self.assertEqual(report["simple_median_baseline"]["sample_count"], 1)
        self.assertEqual(report["simple_median_baseline"]["mean_absolute_error"], "0")

    def test_calibration_target_includes_shipping_like_estimator(self):
        pred_id = self.prediction()
        self.store.record_learning_event("item-1", event_type="market_reference", state="verified",
            source="user_import", evidence_ref="sale-with-shipping", prediction_id=pred_id, currency="USD",
            amount=Decimal("90"), shipping=Decimal("10"), observed_at="2026-01-02T00:00:00+00:00")
        report = calibration_report(self.store)
        self.assertEqual(report["mean_absolute_error"], "0")

    def test_event_replay_is_idempotent_and_correction_is_append_only(self):
        event = dict(event_type="auction", state="won", source="user_confirmed", evidence_ref="receipt-1",
                     currency="USD", amount=Decimal("20"), observed_at="2026-01-03T00:00:00+00:00")
        first = self.store.record_learning_event("item-1", **event)
        self.assertEqual(first, self.store.record_learning_event("item-1", **event))
        correction = self.store.record_learning_event("item-1", **{**event, "amount": Decimal("21"),
            "evidence_ref": "corrected-receipt", "supersedes_event_id": first})
        self.assertNotEqual(first, correction)
        effective = self.store.outcome_events("item-1")
        self.assertEqual([e["id"] for e in effective], [correction])
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("DELETE FROM learning_events WHERE id=?", (first,))

    def test_calibration_rejects_currency_mismatch_and_unknown_is_not_loss(self):
        prediction = self.prediction()
        with self.assertRaises(ValueError):
            self.store.record_learning_event("item-1", event_type="market_reference", state="verified",
                source="marketplace", evidence_ref="sale-eur", prediction_id=prediction, currency="EUR",
                amount=Decimal("90"), observed_at="2026-01-02T00:00:00+00:00")
        self.store.record_learning_event("item-1", event_type="auction", state="unknown",
            source="reconciliation", evidence_ref="timeout")
        self.assertEqual(calibration_report(self.store)["sample_count"], 0)
        self.assertEqual(calibration_report(self.store)["missing_reference_count"], 1)

    def test_reconciliation_records_only_definitive_ended_evidence_and_replays_safely(self):
        ended = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        with self.store.db:
            self.store.db.execute("""INSERT INTO bid_intents(item_id,max_amount,currency,end_time,status,submit_before,created_at,updated_at)
              VALUES(?,?,?,?,?,?,?,?)""", ("item-1", "50", "USD", ended, "submitted", ended, ended, ended))

        class FakeClient:
            calls = 0
            def get_bidding(self, item_id):
                self.calls += 1
                return {"itemId": item_id, "auctionStatus": "ENDED", "highBidder": False,
                        "reservePriceMet": False, "currentPrice": {"currency": "USD", "value": "41.00"}}

        fake = FakeClient()
        output = StringIO()
        with patch("auction_scout.cli.EbayClient", return_value=fake), redirect_stdout(output):
            from auction_scout.cli import main
            main(["--db", self.path, "reconcile"])
            main(["--db", self.path, "reconcile"])
        events = self.store.outcome_events("item-1")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["state"], "no_sale")
        self.assertEqual(fake.calls, 1)
        self.assertIn('"state": "no_sale"', output.getvalue())
        self.assertIsNotNone(self.store.db.execute("SELECT settled_at FROM bid_intents").fetchone()["settled_at"])

    def test_elapsed_unresolved_intent_keeps_full_reservation_until_verified(self):
        self.store.set_budget("USD", Decimal("100"))
        ended = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        with self.store.db:
            cur = self.store.db.execute("""INSERT INTO bid_intents(item_id,max_amount,currency,end_time,status,submit_before,created_at,updated_at)
              VALUES(?,?,?,?,?,?,?,?)""", ("item-1", "75", "USD", ended, "unknown", ended, ended, ended))
        status = self.store.budget_status("USD")
        self.assertEqual(status["reserved"], Decimal("75"))
        with self.assertRaises(ValueError):
            self.store.arm_intent("item-1", Decimal("10"), "USD", datetime.now(timezone.utc) + timedelta(hours=1), 30)
        event = self.store.record_learning_event("item-1", event_type="auction", state="lost", source="user_confirmed",
            evidence_ref="official-history", currency="USD", amount=Decimal("80"))
        self.store.settle_intent(cur.lastrowid, event)
        self.assertEqual(self.store.budget_status("USD")["reserved"], Decimal("0"))


if __name__ == "__main__":
    unittest.main()
