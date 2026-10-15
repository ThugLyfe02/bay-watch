from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
import sys
import time
from time import monotonic

from .ebay import EbayClient, EbayError
from .models import SaleComp
from .money import validate_precision
from .store import Store
from .calibration import calibration_report
from .identity import extract_identity, compatible_titles
from .valuation import analyze, estimate_value, screen_listings
from .valuation import title_similarity
from .planning import portfolio_plan
from .worker import submit_due_intents


def load_comps(path: str) -> list[SaleComp]:
    comps = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            comps.append(SaleComp(r["title"], datetime.fromisoformat(r["sold_at"].replace("Z", "+00:00")),
                                  Decimal(r["price"]), Decimal(r.get("shipping") or "0"), r.get("condition") or "unknown", r.get("source") or "manual", (r.get("currency") or "USD").upper()))
    return comps


def snapshot_payload(listing, valuation, opportunity, comps, *, tax, buyer_fee, buffer, query=None):
    return {"prediction_at": datetime.now(timezone.utc).isoformat(), "currency": listing.currency,
            "fair_value": str(valuation.fair_value), "value_low": str(valuation.low),
            "value_high": str(valuation.high), "confidence": valuation.confidence,
            "comp_count": valuation.comp_count, "listing": {"item_id": listing.item_id, "title": listing.title,
            "url": listing.url, "current_price": str(listing.current_price), "shipping": str(listing.shipping),
            "end_time": listing.end_time.isoformat(), "condition": listing.condition,
            "seller_feedback": listing.seller_feedback, "seller_score": str(listing.seller_score) if listing.seller_score is not None else None,
            "location": listing.location}, "identity": extract_identity(listing.title).__dict__,
            "comps": [{"title": c.title, "sold_at": c.sold_at.isoformat(),
            "price": str(c.price), "shipping": str(c.shipping), "condition": c.condition,
            "source": c.source, "currency": c.currency} for c in comps],
            "risk_flags": list(opportunity.risk_flags), "risk_discount": str(opportunity.risk_discount),
            "all_in_ceiling": str(opportunity.all_in_cap), "max_hammer": str(opportunity.max_hammer),
            "bid_headroom": str(opportunity.bid_headroom), "eligible": opportunity.eligible,
            "assumptions": {"tax_rate": str(tax), "buyer_fee_rate": str(buyer_fee), "reserve_buffer": str(buffer)},
            "query": query}


def main(argv: list[str] | None = None):
    p = argparse.ArgumentParser(prog="auction-scout", description="Research auction listings and schedule hard-capped bids through eBay's official API.")
    p.add_argument("--db", default=os.getenv("AUCTION_SCOUT_DB", "auction_scout.sqlite3"))
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search", help="Find auctions through eBay Browse API")
    s.add_argument("query", help="primary title/query")
    s.add_argument("--variant", action="append", default=[], help="additional title spelling or catalog alias; repeatable")
    s.add_argument("--limit", type=int, default=30, help="maximum results per query")
    op = sub.add_parser("opportunities", help="Search, value, filter, and rank auctions in one pass")
    op.add_argument("query")
    op.add_argument("--variant", action="append", default=[])
    op.add_argument("--comps", required=True)
    op.add_argument("--limit", type=int, default=30, help="maximum results per query")
    op.add_argument("--tax", default="0")
    op.add_argument("--buyer-fee", default="0")
    op.add_argument("--buffer", default="0.08")
    op.add_argument("--max-hours", type=int, default=168)
    a = sub.add_parser("analyze", help="Analyze a stored listing against your sold-comp CSV")
    a.add_argument("item_id"); a.add_argument("--comps", required=True); a.add_argument("--tax", default="0")
    a.add_argument("--buyer-fee", default="0"); a.add_argument("--buffer", default="0.08")
    arm = sub.add_parser("arm", help="Arm an exact maximum proxy bid for one item")
    arm.add_argument("item_id"); arm.add_argument("--max", required=True, dest="max_amount"); arm.add_argument("--currency", default="USD")
    arm.add_argument("--lead-seconds", type=int, default=30); arm.add_argument("--confirm", required=True, help="Type the exact item id to confirm the scheduled bid")
    sub.add_parser("intents", help="List armed bid intents")
    c = sub.add_parser("cancel", help="Cancel an armed bid intent")
    c.add_argument("intent_id", type=int)
    bs = sub.add_parser("budget-set", help="Set the currency-specific open-auction exposure cap")
    bs.add_argument("--currency", default="USD"); bs.add_argument("--amount", required=True)
    bg = sub.add_parser("budget-status", help="Show portfolio cap, reserved exposure, and available headroom")
    bg.add_argument("--currency", default="USD")
    w = sub.add_parser("worker", help="Submit due intents once, or poll when --loop is set")
    w.add_argument("--loop", action="store_true"); w.add_argument("--poll-seconds", type=float, default=0.25)
    w.add_argument("--concurrency", type=int, default=4, help="bounded simultaneous API submissions (1..16)")
    w.add_argument("--live", action="store_true", help="Required for production API calls")
    recon = sub.add_parser("reconcile", help="Read ended-auction status from the official Offer API; never resubmits bids")
    recon.add_argument("--live-read", action="store_true", help="Allow read-only reconciliation against production API")
    outcome = sub.add_parser("record-outcome", help="Append explicitly sourced outcome, purchase, resale, or market evidence")
    outcome.add_argument("item_id"); outcome.add_argument("--kind", choices=["auction", "purchase", "resale", "market_reference"], required=True)
    outcome.add_argument("--state", required=True); outcome.add_argument("--source", required=True); outcome.add_argument("--evidence-ref", required=True)
    outcome.add_argument("--currency"); outcome.add_argument("--amount"); outcome.add_argument("--fees", default="0")
    outcome.add_argument("--shipping", default="0"); outcome.add_argument("--refunds", default="0"); outcome.add_argument("--other-costs", default="0")
    outcome.add_argument("--prediction-id", type=int); outcome.add_argument("--observed-at")
    outcome.add_argument("--confidence", default="user_confirmed"); outcome.add_argument("--supersedes", type=int)
    outcome.add_argument("--intent-id", type=int, help="settle this unresolved intent when recording a definitive auction outcome")
    cal = sub.add_parser("calibration", help="Report chronological valuation calibration against verified later sale references")
    cal.add_argument("--since"); cal.add_argument("--until")
    sub.add_parser("margin-report", help="Report recorded purchase/resale economics; projections are excluded")
    sub.add_parser("discovery-report", help="Report recorded Browse query coverage, freshness, errors, and duplicate yield")
    plan = sub.add_parser("plan", help="Rank saved candidates by visible components without changing bids")
    plan.add_argument("--concurrency", type=int, default=4)
    plan.add_argument("--safe-window-seconds", type=int, default=25)
    dossier = sub.add_parser("dossier", help="Export a saved prediction's evidence dossier as JSON or text")
    dossier.add_argument("item_id"); dossier.add_argument("--prediction-id", type=int)
    dossier.add_argument("--format", choices=["json", "text"], default="json")
    args = p.parse_args(argv)
    store = Store(args.db)
    try:
        if args.cmd == "search":
            client = EbayClient()
            queries = list(dict.fromkeys([args.query, *args.variant]))
            by_id = {}
            for query in queries:
                started = monotonic()
                try:
                    results = client.search_auctions(query, args.limit)
                except EbayError as exc:
                    store.record_discovery_run(query, [], status="error", duration_ms=int((monotonic()-started)*1000),
                        error_class="rate_limited" if exc.status_code == 429 else "api_error")
                    raise
                store.record_discovery_run(query, results, status="ok", duration_ms=int((monotonic()-started)*1000))
                for item in results:
                    by_id.setdefault(item.item_id, item)
            listings = list(by_id.values())
            for item in listings:
                store.save_listing(item)
                print(json.dumps({"item_id": item.item_id, "title": item.title, "price": str(item.current_price), "shipping": str(item.shipping), "end_time": item.end_time.isoformat(), "url": item.url}, ensure_ascii=False))
            store.audit("search", None, {"queries": queries, "count": len(listings)})
        elif args.cmd == "opportunities":
            queries = list(dict.fromkeys([args.query, *args.variant]))
            if args.max_hours < 1 or args.max_hours > 24 * 30:
                raise ValueError("max-hours must be between 1 and 720")
            client = EbayClient()
            by_id = {}
            for query in queries:
                started = monotonic()
                try:
                    results = client.search_auctions(query, args.limit)
                except EbayError as exc:
                    store.record_discovery_run(query, [], status="error", duration_ms=int((monotonic()-started)*1000),
                        error_class="rate_limited" if exc.status_code == 429 else "api_error")
                    raise
                store.record_discovery_run(query, results, status="ok", duration_ms=int((monotonic()-started)*1000))
                for item in results:
                    by_id.setdefault(item.item_id, item)
            listings = list(by_id.values())
            for item in listings: store.save_listing(item)
            opportunities = screen_listings(listings, load_comps(args.comps), tax_rate=Decimal(args.tax),
                buyer_fee_rate=Decimal(args.buyer_fee), reserve_buffer=Decimal(args.buffer), max_hours=args.max_hours)
            comps = load_comps(args.comps)
            output = []
            for o in opportunities:
                decision = "watch" if o.eligible else "pass"
                prediction_id = store.record_prediction(o.listing.item_id, snapshot_payload(o.listing, o.valuation, o, comps,
                    tax=args.tax, buyer_fee=args.buyer_fee, buffer=args.buffer, query=queries), decision=decision, policy_version="valuation-v1")
                output.append({"prediction_id": prediction_id, "item_id": o.listing.item_id, "title": o.listing.title, "url": o.listing.url,
                "current_bid": str(o.listing.current_price), "shipping": str(o.listing.shipping),
                "end_time": o.listing.end_time.isoformat(), "fair_value": str(o.valuation.fair_value),
                "value_range": [str(o.valuation.low), str(o.valuation.high)], "confidence": o.valuation.confidence,
                "comp_count": o.valuation.comp_count, "max_hammer": str(o.max_hammer),
                "bid_headroom": str(o.bid_headroom), "estimated_edge": str(o.estimated_edge),
                "risk_discount": str(o.risk_discount), "eligible": o.eligible, "risk_flags": o.risk_flags})
            print(json.dumps(output, indent=2))
            store.audit("opportunities", None, {"queries": queries, "listing_count": len(listings), "scored_count": len(opportunities)})
        elif args.cmd == "analyze":
            listing = store.get_listing(args.item_id)
            if not listing: raise ValueError("listing not found; run search first")
            comps = load_comps(args.comps)
            value = estimate_value(listing, comps)
            if not value: raise ValueError("no sufficiently similar sold comps; refusing to invent a valuation")
            o = analyze(listing, value, tax_rate=Decimal(args.tax), buyer_fee_rate=Decimal(args.buyer_fee), reserve_buffer=Decimal(args.buffer))
            prediction_id = store.record_prediction(listing.item_id, snapshot_payload(listing, value, o, comps, tax=args.tax,
                buyer_fee=args.buyer_fee, buffer=args.buffer), decision="watch" if o.eligible else "pass", policy_version="valuation-v1")
            print(json.dumps({"prediction_id": prediction_id, "item_id": listing.item_id, "fair_value": str(value.fair_value), "range": [str(value.low), str(value.high)], "confidence": value.confidence, "comps": value.comp_count, "all_in_cap": str(o.all_in_cap), "max_hammer": str(o.max_hammer), "bid_headroom": str(o.bid_headroom), "risk_discount": str(o.risk_discount), "eligible": o.eligible, "estimated_edge": str(o.estimated_edge), "risk_flags": o.risk_flags}, indent=2))
        elif args.cmd == "arm":
            item = store.get_listing(args.item_id)
            if not item: raise ValueError("listing not found; run search first")
            if args.confirm != args.item_id: raise ValueError("confirmation must exactly match item id")
            amount = Decimal(args.max_amount)
            if amount <= 0 or amount > Decimal("1000000"): raise ValueError("maximum must be positive and at most 1,000,000")
            validate_precision(amount, args.currency)
            if args.currency != item.currency: raise ValueError(f"currency mismatch: listing is {item.currency}")
            if args.lead_seconds < 25 or args.lead_seconds > 120: raise ValueError("lead must be 25..120 seconds; live listing preflight, API timeout, polling, and scheduling margin require headroom")
            if item.end_time <= datetime.now(timezone.utc): raise ValueError("auction already ended")
            prediction_id = store.latest_prediction_id(item.item_id)
            if prediction_id:
                prior = json.loads(store.prediction(prediction_id)["snapshot_json"])
                prior.update({"prediction_at": datetime.now(timezone.utc).isoformat(), "user_authored_maximum": str(amount),
                              "decision": "armed"})
                prediction_id = store.record_prediction(item.item_id, prior, decision="armed", policy_version="valuation-v1")
            store.arm_intent(item.item_id, amount, args.currency, item.end_time, args.lead_seconds, prediction_id=prediction_id)
            print(f"Armed item={item.item_id} max={amount:.2f} {args.currency}; cancel with `auction-scout cancel <intent_id>` before submission.")
        elif args.cmd == "intents":
            for r in store.armed_intents(): print(json.dumps(dict(r)))
        elif args.cmd == "cancel":
            store.cancel_intent(args.intent_id); print("Cancelled.")
        elif args.cmd == "budget-set":
            currency = args.currency.upper()
            amount = Decimal(args.amount)
            store.set_budget(currency, amount)
            print(f"Set {currency} open-auction exposure cap to {amount:.2f}")
        elif args.cmd == "budget-status":
            status = store.budget_status(args.currency.upper())
            print(json.dumps({k: str(v) if v is not None else None for k, v in status.items()}, indent=2))
        elif args.cmd == "worker":
            env = os.getenv("EBAY_API_ENV", "sandbox").casefold()
            if env == "production" and (not args.live or os.getenv("ENABLE_LIVE_BIDDING") != "YES"):
                raise ValueError("production bidding requires --live and ENABLE_LIVE_BIDDING=YES")
            if not 1 <= args.concurrency <= 16: raise ValueError("concurrency must be between 1 and 16")
            client = EbayClient()
            while True:
                submit_due_intents(store, client, concurrency=args.concurrency)
                if not args.loop: break
                time.sleep(max(0.05, min(args.poll_seconds, 1)))
        elif args.cmd == "reconcile":
            env = os.getenv("EBAY_API_ENV", "sandbox").casefold()
            if env == "production" and not args.live_read:
                raise ValueError("production reconciliation is read-only but requires --live-read")
            client = EbayClient()
            results = []
            for intent in store.reconcilable_intents(datetime.now(timezone.utc)):
                try:
                    payload = client.get_bidding(intent["item_id"])
                    status = str(payload.get("auctionStatus", "")).upper()
                    if payload.get("itemId") and payload["itemId"] != intent["item_id"]:
                        raise ValueError("Offer API returned a different item id")
                    if status != "ENDED" or not isinstance(payload.get("highBidder"), bool):
                        results.append({"intent_id": intent["id"], "state": "unknown", "reason": "auction not definitively ended or bidder status absent"})
                        continue
                    currency = (payload.get("currentPrice") or {}).get("currency")
                    amount = (payload.get("currentPrice") or {}).get("value")
                    reserve_met = payload.get("reservePriceMet")
                    state = "no_sale" if reserve_met is False else ("won" if payload["highBidder"] else "lost")
                    if amount is None or currency != intent["currency"]:
                        results.append({"intent_id": intent["id"], "state": "unknown", "reason": "ended evidence lacks matching currency/price"})
                        continue
                    evidence_ref = "getBidding:" + intent["item_id"]
                    prior = store.event_for_evidence(intent["item_id"], "auction", "ebay_offer_api", evidence_ref)
                    if prior:
                        if (prior["state"], prior["currency"], prior["amount"]) == (state, currency, str(Decimal(amount))):
                            store.settle_intent(intent["id"], prior["id"])
                            results.append({"intent_id": intent["id"], "state": state, "event_id": prior["id"], "idempotent": True})
                        else:
                            results.append({"intent_id": intent["id"], "state": "unknown", "reason": "official outcome conflicts with prior evidence; append a reviewed correction"})
                        continue
                    event_id = store.record_learning_event(intent["item_id"], event_type="auction", state=state,
                        source="ebay_offer_api", evidence_ref=evidence_ref,
                        observed_at=datetime.now(timezone.utc).isoformat(), prediction_id=intent["prediction_id"],
                        currency=currency, amount=Decimal(amount), confidence="official_api",
                        details={"auction_status": status, "high_bidder": payload["highBidder"], "reserve_price_met": reserve_met})
                    store.settle_intent(intent["id"], event_id)
                    results.append({"intent_id": intent["id"], "state": state, "event_id": event_id})
                except (EbayError, ValueError, KeyError, TypeError) as exc:
                    # No API error is converted to a failed bid or a losing outcome.
                    reason = "Offer API read failed" if isinstance(exc, EbayError) else str(exc)
                    results.append({"intent_id": intent["id"], "state": "unknown", "reason": reason[:240]})
            print(json.dumps(results, indent=2))
        elif args.cmd == "record-outcome":
            event_id = store.record_learning_event(args.item_id, event_type=args.kind, state=args.state,
                source=args.source, evidence_ref=args.evidence_ref, observed_at=args.observed_at,
                prediction_id=args.prediction_id, currency=args.currency,
                amount=Decimal(args.amount) if args.amount is not None else None,
                fees=Decimal(args.fees), shipping=Decimal(args.shipping), refunds=Decimal(args.refunds),
                other_costs=Decimal(args.other_costs),
                confidence=args.confidence, supersedes_event_id=args.supersedes)
            if args.kind == "auction" and args.state in {"won", "lost", "no_sale", "cancelled", "unavailable"} and args.intent_id is not None:
                store.settle_intent(args.intent_id, event_id)
            print(json.dumps({"event_id": event_id, "idempotent": True}))
        elif args.cmd == "calibration":
            print(json.dumps(calibration_report(store, since=args.since, until=args.until), indent=2))
        elif args.cmd == "margin-report":
            report = []
            for item in store.db.execute("SELECT item_id FROM listings ORDER BY item_id"):
                events = store.outcome_events(item["item_id"])
                purchases = [e for e in events if e["event_type"] == "purchase" and e["state"] == "recorded"]
                resales = [e for e in events if e["event_type"] == "resale" and e["state"] == "recorded"]
                for sale in resales:
                    if sale["amount"] is None: continue
                    same_currency = [e for e in purchases if e["currency"] == sale["currency"] and e["amount"] is not None]
                    if not same_currency:
                        report.append({"item_id": item["item_id"], "currency": sale["currency"], "status": "unresolved",
                                       "reason": "no recorded purchase basis in resale currency"})
                        continue
                    basis = sum((Decimal(e["amount"]) for e in same_currency), Decimal(0))
                    net = (Decimal(sale["amount"]) - Decimal(sale["fees"]) - Decimal(sale["shipping"])
                           - Decimal(sale["refunds"]) - Decimal(sale["other_costs"]) - basis)
                    report.append({"item_id": item["item_id"], "currency": sale["currency"], "purchase_basis": str(basis),
                                   "resale_gross": sale["amount"], "resale_fees": sale["fees"], "resale_shipping": sale["shipping"],
                                   "refunds": sale["refunds"], "other_costs": sale["other_costs"],
                                   "realized_net_profit": str(net), "status": "realized"})
                if purchases and not resales:
                    latest = store.db.execute("SELECT fair_value,currency FROM prediction_snapshots WHERE item_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
                                              (item["item_id"],)).fetchone()
                    for currency in sorted({e["currency"] for e in purchases if e["currency"]}):
                        basis = sum((Decimal(e["amount"]) for e in purchases if e["currency"] == currency and e["amount"] is not None), Decimal(0))
                        estimate = latest["fair_value"] if latest and latest["currency"] == currency else None
                        report.append({"item_id": item["item_id"], "currency": currency, "purchase_basis": str(basis),
                                       "estimated_value": estimate, "estimated_headroom": str(Decimal(estimate) - basis) if estimate is not None else None,
                                       "realized_net_profit": None, "status": "unsold_inventory_unrealized"})
            print(json.dumps(report, indent=2))
        elif args.cmd == "dossier":
            prediction_id = args.prediction_id or store.latest_prediction_id(args.item_id)
            pred = store.prediction(prediction_id) if prediction_id else None
            if not pred or pred["item_id"] != args.item_id: raise ValueError("prediction snapshot not found for item")
            data = json.loads(pred["snapshot_json"])
            listing = data.get("listing") or {}
            target = listing.get("title", "")
            identity = data.get("identity") or extract_identity(target).__dict__
            comps, excluded = [], []
            prediction_time = datetime.fromisoformat(pred["created_at"].replace("Z", "+00:00"))
            for comp in data.get("comps", []):
                reason = None
                try:
                    comp_time = datetime.fromisoformat(comp.get("sold_at", "").replace("Z", "+00:00"))
                    comp_total = Decimal(comp.get("price", "0")) + Decimal(comp.get("shipping") or "0")
                    if comp_time.tzinfo is None: reason = "sale timestamp has no timezone"
                    elif comp_time.astimezone(timezone.utc) > prediction_time.astimezone(timezone.utc): reason = "sale occurred after prediction"
                    elif comp_total <= 0: reason = "nonpositive comp total"
                except (ValueError, TypeError):
                    reason = "invalid comp date or amount"
                comp_identity = extract_identity(comp.get("title", "")).__dict__
                identity_comparison = {}
                for field, expected in identity.items():
                    observed = comp_identity.get(field)
                    identity_comparison[field] = "unknown" if expected is None or observed is None else ("match" if expected == observed else "conflict")
                if reason is None and comp.get("currency", "").upper() != pred["currency"]: reason = "currency mismatch"
                elif reason is None and not compatible_titles(target, comp.get("title", "")):
                    conflicts = [field for field, value in identity_comparison.items() if value == "conflict"]
                    reason = "identity conflict: " + ", ".join(conflicts)
                elif reason is None and title_similarity(target, comp.get("title", "")) < 0.25: reason = "title similarity below estimator threshold"
                annotated = {**comp, "identity_comparison": identity_comparison}
                if reason: excluded.append({**annotated, "reason": reason})
                else: comps.append(annotated)
            missing = []
            if listing.get("condition", "unknown").casefold() in {"unknown", "not specified"}: missing.append("verified item condition")
            if listing.get("seller_feedback") is None: missing.append("seller feedback history")
            unknown_identity = [field for field, value in identity.items() if value is None]
            if unknown_identity: missing.append("identity attributes unknown: " + ", ".join(unknown_identity))
            if not comps: missing.append("comparable sales matching identity, currency, and title threshold")
            dossier_data = {"item_id": args.item_id, "prediction_id": pred["id"], "prediction_at": pred["created_at"],
                "decision": pred["decision"], "policy_version": pred["policy_version"], "listing": listing,
                "identity": identity, "matching_comps": comps, "excluded_comps": excluded,
                "valuation": {"currency": pred["currency"], "fair_value": pred["fair_value"],
                    "range": [pred["value_low"], pred["value_high"]], "confidence": pred["confidence"],
                    "comp_count": pred["comp_count"]},
                "risks": data.get("risk_flags", []), "all_in_ceiling": data.get("all_in_ceiling"),
                "max_hammer": data.get("max_hammer"), "bid_headroom": data.get("bid_headroom"),
                "eligible": data.get("eligible"), "assumptions": data.get("assumptions", {}),
                "missing_facts": missing, "outcome_events": [dict(e) for e in store.outcome_events(args.item_id)],
                "note": "Estimated headroom is a model output, not realized profit or a guarantee."}
            if args.format == "json":
                print(json.dumps(dossier_data, indent=2))
            else:
                print(f"Bay Watch dossier — {args.item_id}\nPrediction {pred['id']} at {pred['created_at']} ({pred['policy_version']})")
                print(f"Decision: {pred['decision']} | Fair value: {pred['currency']} {pred['fair_value']} ({pred['value_low']}–{pred['value_high']}) | Confidence: {pred['confidence']}")
                print(f"Ceiling: {dossier_data['all_in_ceiling']} all-in / {dossier_data['max_hammer']} hammer | Headroom: {dossier_data['bid_headroom']}")
                print(f"Identity evidence: {json.dumps(identity, sort_keys=True)}")
                print(f"Comparable sales: {len(comps)} included, {len(excluded)} excluded")
                for comp in comps:
                    print(f"  + {comp['sold_at']} {comp['currency']} {comp['price']} + ship {comp['shipping']} — {comp['title']} [{comp['source']}]")
                for comp in excluded: print(f"  - {comp['title']}: {comp['reason']}")
                print("Risks: " + (", ".join(dossier_data["risks"]) or "none recorded"))
                print("Missing facts: " + (", ".join(missing) or "none flagged"))
                print(dossier_data["note"])
        elif args.cmd == "discovery-report":
            print(json.dumps(store.discovery_report(), indent=2))
        elif args.cmd == "plan":
            print(json.dumps(portfolio_plan(store, concurrency=args.concurrency,
                safe_window_seconds=args.safe_window_seconds), indent=2))
    finally:
        store.close()


if __name__ == "__main__": main()

