from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
import sys
import time

from .ebay import EbayClient, EbayError
from .models import SaleComp
from .store import Store
from .valuation import analyze, estimate_value, screen_listings


def load_comps(path: str) -> list[SaleComp]:
    comps = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            comps.append(SaleComp(r["title"], datetime.fromisoformat(r["sold_at"].replace("Z", "+00:00")),
                                  Decimal(r["price"]), Decimal(r.get("shipping") or "0"), r.get("condition") or "unknown", r.get("source") or "manual"))
    return comps


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
    arm.add_argument("--lead-seconds", type=int, default=8); arm.add_argument("--confirm", required=True, help="Type the exact item id to confirm the scheduled bid")
    sub.add_parser("intents", help="List armed bid intents")
    c = sub.add_parser("cancel", help="Cancel an armed bid intent")
    c.add_argument("intent_id", type=int)
    w = sub.add_parser("worker", help="Submit due intents once, or poll when --loop is set")
    w.add_argument("--loop", action="store_true"); w.add_argument("--poll-seconds", type=float, default=1.0)
    w.add_argument("--live", action="store_true", help="Required for production API calls")
    args = p.parse_args(argv)
    store = Store(args.db)
    try:
        if args.cmd == "search":
            client = EbayClient()
            queries = list(dict.fromkeys([args.query, *args.variant]))
            by_id = {}
            for query in queries:
                for item in client.search_auctions(query, args.limit):
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
                for item in client.search_auctions(query, args.limit):
                    by_id.setdefault(item.item_id, item)
            listings = list(by_id.values())
            for item in listings: store.save_listing(item)
            opportunities = screen_listings(listings, load_comps(args.comps), tax_rate=Decimal(args.tax),
                buyer_fee_rate=Decimal(args.buyer_fee), reserve_buffer=Decimal(args.buffer), max_hours=args.max_hours)
            print(json.dumps([{"item_id": o.listing.item_id, "title": o.listing.title, "url": o.listing.url,
                "current_bid": str(o.listing.current_price), "shipping": str(o.listing.shipping),
                "end_time": o.listing.end_time.isoformat(), "fair_value": str(o.valuation.fair_value),
                "value_range": [str(o.valuation.low), str(o.valuation.high)], "confidence": o.valuation.confidence,
                "comp_count": o.valuation.comp_count, "max_hammer": str(o.max_hammer),
                "bid_headroom": str(o.bid_headroom), "estimated_edge": str(o.estimated_edge),
                "eligible": o.eligible, "risk_flags": o.risk_flags} for o in opportunities], indent=2))
            store.audit("opportunities", None, {"queries": queries, "listing_count": len(listings), "scored_count": len(opportunities)})
        elif args.cmd == "analyze":
            listing = store.get_listing(args.item_id)
            if not listing: raise ValueError("listing not found; run search first")
            value = estimate_value(listing, load_comps(args.comps))
            if not value: raise ValueError("no sufficiently similar sold comps; refusing to invent a valuation")
            o = analyze(listing, value, tax_rate=Decimal(args.tax), buyer_fee_rate=Decimal(args.buyer_fee), reserve_buffer=Decimal(args.buffer))
            print(json.dumps({"item_id": listing.item_id, "fair_value": str(value.fair_value), "range": [str(value.low), str(value.high)], "confidence": value.confidence, "comps": value.comp_count, "all_in_cap": str(o.all_in_cap), "max_hammer": str(o.max_hammer), "bid_headroom": str(o.bid_headroom), "eligible": o.eligible, "estimated_edge": str(o.estimated_edge), "risk_flags": o.risk_flags}, indent=2))
        elif args.cmd == "arm":
            item = store.get_listing(args.item_id)
            if not item: raise ValueError("listing not found; run search first")
            if args.confirm != args.item_id: raise ValueError("confirmation must exactly match item id")
            amount = Decimal(args.max_amount)
            if amount <= 0 or amount > Decimal("1000000"): raise ValueError("maximum must be positive and at most 1,000,000")
            if args.currency != item.currency: raise ValueError(f"currency mismatch: listing is {item.currency}")
            if args.lead_seconds < 3 or args.lead_seconds > 120: raise ValueError("lead must be 3..120 seconds")
            if item.end_time <= datetime.now(timezone.utc): raise ValueError("auction already ended")
            store.arm_intent(item.item_id, amount, args.currency, item.end_time, args.lead_seconds)
            print(f"Armed item={item.item_id} max={amount:.2f} {args.currency}; cancel with `auction-scout cancel <intent_id>` before submission.")
        elif args.cmd == "intents":
            for r in store.armed_intents(): print(json.dumps(dict(r)))
        elif args.cmd == "cancel":
            store.cancel_intent(args.intent_id); print("Cancelled.")
        elif args.cmd == "worker":
            env = os.getenv("EBAY_API_ENV", "sandbox").casefold()
            if env == "production" and (not args.live or os.getenv("ENABLE_LIVE_BIDDING") != "YES"):
                raise ValueError("production bidding requires --live and ENABLE_LIVE_BIDDING=YES")
            client = None
            while True:
                due = store.due_intents(datetime.now(timezone.utc))
                for row in due:
                    if datetime.now(timezone.utc) >= datetime.fromisoformat(row["end_time"]):
                        store.transition_intent(row["id"], "armed", "expired", {"reason": "auction already ended"}); continue
                    # Claim before network I/O. If the result is ambiguous, never retry automatically.
                    store.transition_intent(row["id"], "armed", "submitting", {"max_amount": row["max_amount"], "currency": row["currency"]})
                    try:
                        client = client or EbayClient()
                        response = client.place_proxy_bid(row["item_id"], Decimal(row["max_amount"]), row["currency"])
                        store.transition_intent(row["id"], "submitting", "submitted", {"response": response})
                        print(f"Submitted proxy bid for {row['item_id']} at max {row['max_amount']} {row['currency']}")
                    except Exception as e:
                        store.transition_intent(row["id"], "submitting", "unknown", {"error": str(e), "action": "verify in eBay before any manual retry"})
                        print(f"Outcome unknown for {row['item_id']}; verify in eBay. No retry sent. Error: {e}", file=sys.stderr)
                if not args.loop: break
                time.sleep(max(0.25, min(args.poll_seconds, 10)))
    finally:
        store.close()


if __name__ == "__main__": main()

