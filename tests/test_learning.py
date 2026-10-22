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
from auction_scout.planning import portfolio_plan
from auction_scout.store import MIGRATIONS, SCHEMA, Store


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
        self.assertEqual(self.store.schema_version(), 10)
        self.assertIsNotNone(self.store.get_listing("old"))
        self.assertIn("prediction_id", {r["name"] for r in self.store.db.execute("PRAGMA table_info(bid_intents)")})

    def test_schema_v9_database_upgrades_to_worker_observability_without_data_loss(self):
        self.store.close()
        v9_path = os.path.join(self.tmp.name, "schema-v9.db")
        db = sqlite3.connect(v9_path)
        for version, script in MIGRATIONS:
            if version > 9:
                break
            db.executescript(f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version={version};\nCOMMIT;")
        db.execute("INSERT INTO listings VALUES(?,?,?,?,?,?,?,?,?,?)", ("v9-item", "V9 item", "url", "USD", "1", "0",
            "2026-01-01T00:00:00+00:00", "unknown", "{}", "2025-01-01T00:00:00+00:00"))
        db.commit(); db.close()
        self.store = Store(v9_path)
        self.assertEqual(self.store.schema_version(), 10)
        self.assertIsNotNone(self.store.get_listing("v9-item"))
        self.assertEqual(self.store.worker_observability_report()["run_count"], 0)

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

    def test_discovery_provenance_preserves_alias_runs_and_duplicate_yield(self):
        listing = self.store.get_listing("item-1")
        self.store.record_discovery_run("Test card", [listing, listing], status="ok", duration_ms=12)
        self.store.record_discovery_run("test-card alt", [listing], status="ok", duration_ms=9)
        report = self.store.discovery_report()
        self.assertEqual(report["run_count"], 2)
        self.assertEqual(report["distinct_listings"], 1)
        primary = next(row for row in report["queries"] if row["query"] == "Test card")
        self.assertEqual(primary["duplicate_count"], 1)
        self.assertIn("recall is unknown", report["recall_note"])

    def test_category_profiles_are_immutable_versioned_and_report_only_observed_coverage(self):
        first = self.store.save_category_profile("pokemon-cards", "Trading cards",
            ["Umbreon PSA 7", "Umbreon #197 holo"], ["card_number", "grade"])
        second = self.store.save_category_profile("POKEMON-CARDS", "Trading cards",
            ["Umbreon Japanese PSA 7"], ["language", "grade"])
        self.assertEqual((first["version"], second["version"]), (1, 2))
        self.assertEqual(second["name"], "pokemon-cards")
        self.assertEqual(self.store.get_category_profile("pokemon-cards", 1)["queries"],
                         ["Umbreon PSA 7", "Umbreon #197 holo"])
        with self.assertRaises(ValueError):
            self.store.save_category_profile("bad", "cards", ["query"], ["invented_identity"])
        listing = self.store.get_listing("item-1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("UPDATE category_profiles SET category='changed' WHERE name='pokemon-cards'")
        with self.assertRaises(ValueError):
            self.store.record_discovery_run("not-an-alias", [listing], status="ok", duration_ms=2,
                profile_name="pokemon-cards", profile_version=2)
        self.store.record_discovery_run("Umbreon Japanese PSA 7", [listing, listing], status="ok",
            duration_ms=8, profile_name="pokemon-cards", profile_version=2)
        self.store.record_missed_listing("item-missed", query="Umbreon Japanese PSA 7", source="user_report",
            evidence_ref="reviewable-ref", profile="pokemon-cards", profile_version=2)
        profile_report = next(row for row in self.store.category_profile_report() if row["version"] == 2)
        self.assertEqual(profile_report["version"], 2)
        self.assertEqual(profile_report["run_count"], 1)
        self.assertEqual(profile_report["distinct_returned_listings"], 1)
        self.assertEqual(profile_report["user_evidenced_missed_examples"], 1)
        self.assertEqual(profile_report["unversioned_missed_examples"], 0)
        self.assertEqual(profile_report["recall"].split(";")[0], "unknown")

    def test_profile_search_runs_saved_aliases_and_preserves_profile_provenance(self):
        self.store.save_category_profile("pokemon-cards", "Trading cards",
            ["Umbreon PSA 7", "Umbreon #197 holo"], ["grade"])
        listing = self.store.get_listing("item-1")
        class FakeClient:
            def __init__(self): self.queries = []
            def search_auctions(self, query, limit):
                self.queries.append(query)
                return [listing]
        output = StringIO()
        with patch("auction_scout.cli.EbayClient", FakeClient), redirect_stdout(output):
            from auction_scout.cli import main
            main(["--db", self.path, "profile-search", "pokemon-cards", "--limit", "10"])
        import json
        result = json.loads(output.getvalue())
        self.assertEqual(len(result["report"]["runs"]), 2)
        self.assertEqual(result["report"]["distinct_listing_count"], 1)
        self.assertEqual(result["report"]["recall"].split(";")[0], "unknown")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM discovery_runs WHERE profile_name='pokemon-cards' AND profile_version=1").fetchone()[0], 2)

    def test_portfolio_plan_keeps_currencies_separate_and_changes_no_maxima(self):
        end = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.store.record_prediction("item-1", {"prediction_at": datetime.now(timezone.utc).isoformat(),
            "currency": "USD", "fair_value": "100", "value_low": "80", "value_high": "120",
            "confidence": "high", "comp_count": 8, "listing": {"title": "Test card", "end_time": end},
            "eligible": True, "bid_headroom": "30", "risk_flags": []}, decision="watch", policy_version="v1")
        self.listing("item-eur", "EUR")
        self.store.record_prediction("item-eur", {"prediction_at": datetime.now(timezone.utc).isoformat(),
            "currency": "EUR", "fair_value": "100", "value_low": "80", "value_high": "120",
            "confidence": "medium", "comp_count": 5, "listing": {"title": "Test card", "end_time": end},
            "eligible": True, "bid_headroom": "25", "risk_flags": []}, decision="watch", policy_version="v1")
        report = portfolio_plan(self.store)
        self.assertEqual(set(report["by_currency"]), {"USD", "EUR"})
        self.assertFalse(report["automatic_maximum_changes"])
        self.assertFalse(report["bids_submitted"])
        self.assertEqual(report["by_currency"]["USD"]["candidates"][0]["headroom"], "30")

    def test_margin_report_calculates_realized_net_only_from_actual_events(self):
        self.store.record_learning_event("item-1", event_type="purchase", state="recorded", source="receipt",
            evidence_ref="checkout-1", currency="USD", amount=Decimal("100"))
        self.store.record_learning_event("item-1", event_type="resale", state="recorded", source="order",
            evidence_ref="sale-1", currency="USD", amount=Decimal("150"), fees=Decimal("10"),
            shipping=Decimal("5"), refunds=Decimal("2"), other_costs=Decimal("3"))
        output = StringIO()
        with redirect_stdout(output):
            from auction_scout.cli import main
            main(["--db", self.path, "margin-report"])
        import json
        result = json.loads(output.getvalue())
        self.assertEqual(result[0]["realized_net_profit"], "30")
        self.assertEqual(result[0]["status"], "realized")

    def test_missed_listing_feedback_is_user_evidenced_append_only_and_idempotent(self):
        args = dict(query="Umbreon PSA 7", source="user_report", evidence_ref="https://example.invalid/item/42",
                    profile="pokemon-cards", observed_at="2026-02-01T10:00:00Z", reason="title omitted card name")
        first = self.store.record_missed_listing("v1|42|0", **args)
        self.assertEqual(first, self.store.record_missed_listing("v1|42|0", **args))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("UPDATE missed_listing_feedback SET query='other' WHERE id=?", (first,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("DELETE FROM missed_listing_feedback WHERE id=?", (first,))
        report = self.store.evidence_coverage_report()
        self.assertEqual(report["missed_listing_feedback"]["user_evidenced_record_count"], 1)
        self.assertFalse(report["missed_listing_feedback"]["denominator_available"])
        self.assertIn("recall is unknown", report["missed_listing_feedback"]["note"])

    def test_evidence_report_states_zero_sample_and_outcome_gaps_without_inference(self):
        report = self.store.evidence_coverage_report()
        self.assertEqual(report["predictions"]["snapshot_count"], 0)
        self.assertEqual(report["calibration"]["sample_count"], 0)
        self.assertIsNone(report["calibration"]["mean_absolute_error"])
        self.assertEqual(report["calibration_status"], "not_measurable_no_linked_verified_reference")
        self.assertEqual(report["false_positive_rate_status"], "not_measurable_no_reviewed_candidate_labeling_protocol")
        self.assertEqual(report["missed_close_rate_status"], "not_measurable_no_verified_execution_miss_labels")
        self.assertEqual(report["realized_resale"]["completed_resale_count_with_purchase_basis"], 0)
        self.assertTrue(any("recall is unknown" in note.casefold() for note in report["gaps"]))


if __name__ == "__main__":
    unittest.main()
