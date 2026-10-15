from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal
import sys

from .ebay import EbayClient
from .store import Store


def submit_due_intents(store: Store, client: EbayClient, *, concurrency: int = 4,
                       now: datetime | None = None) -> tuple[int, int]:
    """Claim due bids once, post bounded parallel requests, and never retry ambiguous outcomes."""
    if concurrency < 1 or concurrency > 16:
        raise ValueError("concurrency must be between 1 and 16")
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
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="bay-watch-bid") as pool:
        futures = {pool.submit(client.place_proxy_bid, row["item_id"], Decimal(row["max_amount"]), row["currency"]): row for row in claimed}
        for future in as_completed(futures):
            row = futures[future]
            try:
                response = future.result()
                store.transition_intent(row["id"], "submitting", "submitted", {"response": response})
                submitted += 1
                print(f"Submitted proxy bid for {row['item_id']} at max {row['max_amount']} {row['currency']}")
            except Exception as exc:
                store.transition_intent(row["id"], "submitting", "unknown", {"error": str(exc), "action": "verify in eBay before any manual retry"})
                unknown += 1
                print(f"Outcome unknown for {row['item_id']}; verify in eBay. No retry sent. Error: {exc}", file=sys.stderr)
    return submitted, unknown
