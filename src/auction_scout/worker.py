from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sys
from time import monotonic
from uuid import uuid4

from .ebay import EbayClient
from .store import Store


MINIMUM_POST_WINDOW_SECONDS = 10


def _preflight_reason(item: dict, item_id: str, currency: str, max_amount: Decimal,
                      now: datetime) -> tuple[str | None, str | None, str | None]:
    """Return (reason, terminal_state, stable reason code); fail closed before any bid POST."""
    if item.get("itemId") != item_id:
        return "fresh listing response did not match the armed item ID", "failed", "item_id_mismatch"
    buying_options = set(item.get("buyingOptions") or [])
    if "AUCTION" not in buying_options:
        return "listing is no longer an auction", "failed", "not_an_auction"
    end_value = item.get("itemEndDate")
    if not end_value:
        return "fresh listing response has no auction end time", "failed", "missing_end_time"
    try:
        end_time = datetime.fromisoformat(end_value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return "fresh listing response has an invalid auction end time", "failed", "invalid_end_time"
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    if now >= end_time:
        return "auction has already ended", "expired", "auction_ended"
    if end_time - now <= timedelta(seconds=MINIMUM_POST_WINDOW_SECONDS):
        return f"only {max(0, int((end_time - now).total_seconds()))} seconds remain; less than the safe bid-request window", "failed", "insufficient_time_runway"

    amounts = [item.get("minimumPriceToBid"), item.get("currentBidPrice"), item.get("price")]
    reference = next((amount for amount in amounts if amount), None)
    if reference is None or not reference.get("currency") or reference.get("value") is None:
        return "fresh listing response has no usable bid amount or currency", "failed", "missing_price_currency"
    if reference["currency"].upper() != currency.upper():
        return f"listing currency changed to {reference['currency']}; armed intent is {currency}", "failed", "listing_currency_changed"
    minimum_bid = item.get("minimumPriceToBid")
    if minimum_bid and minimum_bid.get("value") is not None:
        if minimum_bid.get("currency", currency).upper() != currency.upper():
            return "minimum bid currency does not match the armed intent", "failed", "minimum_bid_currency_mismatch"
        if max_amount < Decimal(minimum_bid["value"]):
            return "armed maximum is below eBay's current minimum bid", "failed", "maximum_below_minimum"
    current_bid = item.get("currentBidPrice")
    if current_bid and current_bid.get("value") is not None:
        if current_bid.get("currency", currency).upper() != currency.upper():
            return "current bid currency does not match the armed intent", "failed", "current_bid_currency_mismatch"
        if max_amount <= Decimal(current_bid["value"]):
            return "armed maximum is not above eBay's current bid", "failed", "maximum_not_above_current"
    # eBay rejects a proxy maximum above Buy It Now on listings offering both options.
    bin_price = item.get("price")
    if "FIXED_PRICE" in buying_options and bin_price and bin_price.get("value") is not None:
        if bin_price.get("currency", currency).upper() != currency.upper():
            return "Buy It Now currency does not match the armed intent", "failed", "buy_it_now_currency_mismatch"
        if max_amount > Decimal(bin_price["value"]):
            return "armed maximum exceeds the listing's Buy It Now price", "failed", "maximum_above_buy_it_now"
    return None, None, None


def submit_due_intents(store: Store, client: EbayClient, *, concurrency: int = 4,
                       now: datetime | None = None) -> tuple[int, int]:
    """Claim intents once, preflight live listings, and post bounded parallel bids."""
    if concurrency < 1 or concurrency > 16:
        raise ValueError("concurrency must be between 1 and 16")
    run_started = monotonic()
    run_started_at = datetime.now(timezone.utc).isoformat()
    supplied_now = now
    now = now or datetime.now(timezone.utc)
    due_rows = store.due_intents(now)
    if not due_rows:
        return 0, 0
    run_id = uuid4().hex
    expected_claims = sum(datetime.fromisoformat(row["end_time"]) > now for row in due_rows)
    store.start_worker_run(run_id, started_at=run_started_at, concurrency=concurrency,
                           due_queue_depth=len(due_rows), claimed_count=expected_claims,
                           overloaded=expected_claims > concurrency)
    claimed = []
    for row in due_rows:
        if now >= datetime.fromisoformat(row["end_time"]):
            store.transition_intent(row["id"], "armed", "expired", {"reason": "auction already ended"})
            observed = datetime.now(timezone.utc)
            store.record_worker_observation(run_id,row["id"],phase="claim",observed_at=observed.isoformat(),
                duration_ms=0,outcome="expired",reason_code="auction_ended_before_claim",
                seconds_to_close=int((datetime.fromisoformat(row["end_time"]).astimezone(timezone.utc)-observed).total_seconds()),
                observation_key=f"{run_id}:{row['id']}:claim")
            continue
        store.transition_intent(row["id"], "armed", "submitting", {"max_amount": row["max_amount"], "currency": row["currency"]})
        claimed.append(row)
        observed = datetime.now(timezone.utc)
        store.record_worker_observation(run_id,row["id"],phase="claim",observed_at=observed.isoformat(),
            duration_ms=0,outcome="claimed",seconds_to_close=int((datetime.fromisoformat(row["end_time"]).astimezone(timezone.utc)-observed).total_seconds()),
            observation_key=f"{run_id}:{row['id']}:claim")
    submitted = unknown = 0
    if len(claimed) > concurrency:
        print(f"{len(claimed)} bids are due with concurrency {concurrency}; excess requests will queue and may miss their auction close.", file=sys.stderr)

    def preflight_and_bid(row):
        preflight_started = monotonic()
        preflight_observed = datetime.now(timezone.utc)
        try:
            live_item = client.get_item(row["item_id"])
            check_time = supplied_now if supplied_now is not None else datetime.now(timezone.utc)
            reason, state, reason_code = _preflight_reason(live_item, row["item_id"], row["currency"],
                                              Decimal(row["max_amount"]), check_time)
        except Exception as exc:
            duration = int((monotonic()-preflight_started)*1000)
            observed = datetime.now(timezone.utc)
            return "failed", {"phase": "preflight", "no_bid_submitted": True,
                               "failure_class": type(exc).__name__, "reason_code": "preflight_api_error",
                               "action": "refresh listing and re-arm if still desired"}, {
                "preflight": {"observed_at": observed.isoformat(), "duration_ms": duration,
                    "outcome": "error", "reason_code": "preflight_api_error", "failure_class": type(exc).__name__,
                    "seconds_to_close": int((datetime.fromisoformat(row["end_time"]).astimezone(timezone.utc)-observed).total_seconds())}}
        preflight_duration = int((monotonic()-preflight_started)*1000)
        preflight_done_at = datetime.now(timezone.utc)
        if reason:
            return state, {"phase": "preflight", "no_bid_submitted": True, "reason": reason,
                           "reason_code": reason_code}, {"preflight": {"observed_at": preflight_done_at.isoformat(),
                "duration_ms": preflight_duration, "outcome": "expired" if state == "expired" else "blocked",
                "reason_code": reason_code, "seconds_to_close": int((datetime.fromisoformat(row["end_time"]).astimezone(timezone.utc)-preflight_done_at).total_seconds())}}
        preflight_observed = preflight_done_at
        submission_started = monotonic()
        try:
            response = client.place_proxy_bid(row["item_id"], Decimal(row["max_amount"]), row["currency"])
            safe_fields = ("auctionStatus", "highBidder", "reservePriceMet")
            summary = {key: response[key] for key in safe_fields if isinstance(response, dict) and key in response}
            target = "submitted"
            details = {"response_summary": summary, "max_amount": row["max_amount"],
                       "currency": row["currency"], "raw_response_stored": False}
            reason_code, failure_class = None, None
        except Exception as exc:
            target = "unknown"
            details = {"phase": "bid_submission", "reason_code": "bid_outcome_ambiguous",
                       "failure_class": type(exc).__name__, "action": "verify in eBay before any manual retry"}
            reason_code, failure_class = "bid_outcome_ambiguous", type(exc).__name__
        submission_done_at = datetime.now(timezone.utc)
        telemetry = {"preflight": {"observed_at": preflight_observed.isoformat(),
                "duration_ms": preflight_duration, "outcome": "passed",
                "seconds_to_close": int((datetime.fromisoformat(row["end_time"]).astimezone(timezone.utc)-preflight_observed).total_seconds())},
            "submission": {"observed_at": submission_done_at.isoformat(),
                "duration_ms": int((monotonic()-submission_started)*1000),
                "outcome": "accepted" if target == "submitted" else "unknown", "reason_code": reason_code,
                "failure_class": failure_class,
                "seconds_to_close": int((datetime.fromisoformat(row["end_time"]).astimezone(timezone.utc)-submission_done_at).total_seconds())}}
        return target, details, telemetry

    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="bay-watch-bid") as pool:
        futures = {pool.submit(preflight_and_bid, row): row for row in claimed}
        for future in as_completed(futures):
            row = futures[future]
            try:
                target, details, telemetry = future.result()
            except Exception as exc:
                target, details = "failed", {"phase": "preflight", "no_bid_submitted": True,
                    "reason_code": "worker_unexpected_error", "failure_class": type(exc).__name__}
                observed = datetime.now(timezone.utc)
                telemetry = {"preflight": {"observed_at": observed.isoformat(), "duration_ms": 0,
                    "outcome": "error", "reason_code": "worker_unexpected_error",
                    "failure_class": type(exc).__name__, "seconds_to_close": int((datetime.fromisoformat(row["end_time"]).astimezone(timezone.utc)-observed).total_seconds())}}
            for phase, sample in telemetry.items():
                store.record_worker_observation(run_id,row["id"],phase=phase,observation_key=f"{run_id}:{row['id']}:{phase}",**sample)
            store.transition_intent(row["id"], "submitting", target, details)
            if target == "submitted":
                submitted += 1
                print(f"Submitted proxy bid for {row['item_id']} at max {row['max_amount']} {row['currency']}")
            elif target == "unknown":
                unknown += 1
                print(f"Outcome unknown for {row['item_id']}; verify in eBay. No retry sent. ({details.get('failure_class', 'unknown failure')})", file=sys.stderr)
            elif target == "expired":
                print(f"Skipped {row['item_id']}: auction ended before submission.", file=sys.stderr)
            else:
                print(f"Bid not sent for {row['item_id']}: {details.get('reason') or details.get('failure_class') or 'preflight rejected'}", file=sys.stderr)
    finished_at = datetime.now(timezone.utc).isoformat()
    no_bid = sum(1 for row in due_rows if row["id"] not in {item["id"] for item in claimed}) + len(claimed)-submitted-unknown
    store.finish_worker_run(run_id,finished_at=finished_at,duration_ms=int((monotonic()-run_started)*1000),
                            submitted_count=submitted,unknown_count=unknown,no_bid_count=no_bid)
    return submitted, unknown
