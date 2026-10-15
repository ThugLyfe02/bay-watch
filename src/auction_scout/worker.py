from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sys

from .ebay import EbayClient
from .store import Store


MINIMUM_POST_WINDOW_SECONDS = 10


def _preflight_reason(item: dict, item_id: str, currency: str, max_amount: Decimal,
                      now: datetime) -> tuple[str | None, str | None]:
    """Return (reason, terminal_state); fail closed before any bid POST."""
    if item.get("itemId") != item_id:
        return "fresh listing response did not match the armed item ID", "failed"
    buying_options = set(item.get("buyingOptions") or [])
    if "AUCTION" not in buying_options:
        return "listing is no longer an auction", "failed"
    end_value = item.get("itemEndDate")
    if not end_value:
        return "fresh listing response has no auction end time", "failed"
    try:
        end_time = datetime.fromisoformat(end_value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return "fresh listing response has an invalid auction end time", "failed"
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    if now >= end_time:
        return "auction has already ended", "expired"
    if end_time - now <= timedelta(seconds=MINIMUM_POST_WINDOW_SECONDS):
        return f"only {max(0, int((end_time - now).total_seconds()))} seconds remain; less than the safe bid-request window", "failed"

    amounts = [item.get("minimumPriceToBid"), item.get("currentBidPrice"), item.get("price")]
    reference = next((amount for amount in amounts if amount), None)
    if reference is None or not reference.get("currency") or reference.get("value") is None:
        return "fresh listing response has no usable bid amount or currency", "failed"
    if reference["currency"].upper() != currency.upper():
        return f"listing currency changed to {reference['currency']}; armed intent is {currency}", "failed"
    minimum_bid = item.get("minimumPriceToBid")
    if minimum_bid and minimum_bid.get("value") is not None:
        if minimum_bid.get("currency", currency).upper() != currency.upper():
            return "minimum bid currency does not match the armed intent", "failed"
        if max_amount < Decimal(minimum_bid["value"]):
            return "armed maximum is below eBay's current minimum bid", "failed"
    current_bid = item.get("currentBidPrice")
    if current_bid and current_bid.get("value") is not None:
        if current_bid.get("currency", currency).upper() != currency.upper():
            return "current bid currency does not match the armed intent", "failed"
        if max_amount <= Decimal(current_bid["value"]):
            return "armed maximum is not above eBay's current bid", "failed"
    # eBay rejects a proxy maximum above Buy It Now on listings offering both options.
    bin_price = item.get("price")
    if "FIXED_PRICE" in buying_options and bin_price and bin_price.get("value") is not None:
        if bin_price.get("currency", currency).upper() != currency.upper():
            return "Buy It Now currency does not match the armed intent", "failed"
        if max_amount > Decimal(bin_price["value"]):
            return "armed maximum exceeds the listing's Buy It Now price", "failed"
    return None, None


def submit_due_intents(store: Store, client: EbayClient, *, concurrency: int = 4,
                       now: datetime | None = None) -> tuple[int, int]:
    """Claim intents once, preflight live listings, and post bounded parallel bids."""
    if concurrency < 1 or concurrency > 16:
        raise ValueError("concurrency must be between 1 and 16")
    supplied_now = now
    now = now or datetime.now(timezone.utc)
    claimed = []
    for row in store.due_intents(now):
        if now >= datetime.fromisoformat(row["end_time"]):
            store.transition_intent(row["id"], "armed", "expired", {"reason": "auction already ended"})
            continue
        store.transition_intent(row["id"], "armed", "submitting", {"max_amount": row["max_amount"], "currency": row["currency"]})
        claimed.append(row)
    submitted = unknown = 0
    if len(claimed) > concurrency:
        print(f"{len(claimed)} bids are due with concurrency {concurrency}; excess requests will queue and may miss their auction close.", file=sys.stderr)

    def preflight_and_bid(row):
        try:
            live_item = client.get_item(row["item_id"])
            check_time = supplied_now if supplied_now is not None else datetime.now(timezone.utc)
            reason, state = _preflight_reason(live_item, row["item_id"], row["currency"],
                                              Decimal(row["max_amount"]), check_time)
        except Exception as exc:
            return "failed", {"phase": "preflight", "no_bid_submitted": True,
                               "error": str(exc), "action": "refresh listing and re-arm if still desired"}
        if reason:
            return state, {"phase": "preflight", "no_bid_submitted": True, "reason": reason}
        try:
            response = client.place_proxy_bid(row["item_id"], Decimal(row["max_amount"]), row["currency"])
            safe_fields = ("auctionStatus", "highBidder", "reservePriceMet")
            summary = {key: response[key] for key in safe_fields if isinstance(response, dict) and key in response}
            return "submitted", {"response_summary": summary, "max_amount": row["max_amount"],
                                  "currency": row["currency"], "raw_response_stored": False}
        except Exception as exc:
            return "unknown", {"phase": "bid_submission", "error": str(exc),
                               "action": "verify in eBay before any manual retry"}

    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="bay-watch-bid") as pool:
        futures = {pool.submit(preflight_and_bid, row): row for row in claimed}
        for future in as_completed(futures):
            row = futures[future]
            try:
                target, details = future.result()
            except Exception as exc:
                target, details = "failed", {"phase": "preflight", "no_bid_submitted": True, "error": str(exc)}
            store.transition_intent(row["id"], "submitting", target, details)
            if target == "submitted":
                submitted += 1
                print(f"Submitted proxy bid for {row['item_id']} at max {row['max_amount']} {row['currency']}")
            elif target == "unknown":
                unknown += 1
                print(f"Outcome unknown for {row['item_id']}; verify in eBay. No retry sent. Error: {details.get('error')}", file=sys.stderr)
            elif target == "expired":
                print(f"Skipped {row['item_id']}: auction ended before submission.", file=sys.stderr)
            else:
                print(f"Bid not sent for {row['item_id']}: {details.get('reason') or details.get('error')}", file=sys.stderr)
    return submitted, unknown
