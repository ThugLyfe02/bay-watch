# Bay Watch

Bay Watch is an eBay-first auction research and bid workbench. It searches the official Browse API, scores a listing against user-supplied sold comps, calculates a conservative all-in ceiling, and can schedule one exact proxy-bid maximum through eBay's official Offer API.

## Guardrails

- Uses official eBay Buy APIs only. No page scraping, private endpoints, CAPTCHA handling, browser/password automation, or anti-bot evasion.
- No valuation is fabricated: `analyze` requires a sold-comps CSV and visibly reports confidence and comp count.
- A bid intent requires the exact item ID and exact maximum amount. The maximum cannot be escalated by the worker.
- Each currency requires an explicit open-auction exposure cap before arming. Budget reservations are atomic across local CLI attempts and include the full maximum bid for active auctions.
- Worker claims an intent before network I/O and never auto-retries a bid with an ambiguous outcome. Verify the account on eBay before taking any further action.
- Production mode requires `EBAY_API_ENV=production`, `ENABLE_LIVE_BIDDING=YES`, and `--live`. eBay's Buy Offer API production access is partner-gated; sandbox is the default.
- Keep tokens in a secrets manager. Do not use your eBay password or commit credentials.

## Install and run

```bash
cd auction-scout
python -m venv .venv && . .venv/bin/activate
pip install -e .
cp .env.example .env  # populate values in your shell or a local env loader
export EBAY_USER_ACCESS_TOKEN='...'
export EBAY_API_ENV=sandbox
auction-scout search "2000 Japanese Umbreon holo PSA 7" --limit 25
# Search alternate catalog wording and deduplicate by item ID:
auction-scout search "Umbreon PSA 7" --variant "Japanese Umbreon grade 7" --variant "Umbreon #197 holo" --limit 20
```

Search stores normalized results in SQLite and prints JSONL. Create `sold-comps.csv` with columns:

```csv
title,sold_at,price,shipping,condition,source
Pokemon Umbreon holo PSA 7,2026-09-12T18:00:00Z,210.00,0,graded,manual
```

Then analyze, set the total open-auction exposure cap, and arm a hard ceiling:

```bash
auction-scout analyze 'v1|ITEM_ID|0' --comps sold-comps.csv --tax 0.08875 --buffer 0.10
auction-scout budget-set --currency USD --amount 500
auction-scout budget-status --currency USD
auction-scout arm 'v1|ITEM_ID|0' --max 175.00 --currency USD --lead-seconds 8 --confirm 'v1|ITEM_ID|0'
auction-scout intents
auction-scout worker --loop
```

For a single ranked scan, use `opportunities`; it searches the primary query and aliases, deduplicates listings, screens to a deadline window, applies the comp and cost model, and ranks by eligibility/headroom:

```bash
auction-scout opportunities "Umbreon PSA 7" --variant "Japanese Umbreon grade 7" --comps sold-comps.csv --tax 0.08875 --buffer 0.10 --max-hours 72
```

Only medium/high confidence matches with a nonnegative ceiling headroom and a specified condition are marked eligible. The report still includes lower-confidence candidates as watch-only so thin evidence is visible.

The worker posts the eBay proxy bid at the configured lead time. eBay's proxy bidding system accepts the buyer's maximum and bids only as needed. Do not enter an amount above your personal all-in budget. For a production account, after approval:

```bash
export EBAY_API_ENV=production
export ENABLE_LIVE_BIDDING=YES
auction-scout worker --live --loop
```

Do not run multiple workers against the same database. SQLite state transition prevents double claims for a single local worker, but production deployment should use a single scheduler or move claim semantics to a transactional server database before horizontal scaling.

## What makes the research layer useful

- Search title variants separately (graded/raw, set/number, language, first edition) so near-miss nomenclature is surfaced without blending distinct assets into one valuation.
- Comp matching uses transparent token overlap, recency decay, condition penalty, and median/MAD outlier resistance. Treat it as triage; authenticate and inspect the item yourself.
- For Pokémon cards, known mismatches in card number, grading company, grade, language, edition, and holo variant are excluded before valuation. Missing identity details remain a confidence limitation rather than being silently inferred.
- Risk flags now change the bid ceiling: thin comps apply a 12% haircut, incomplete seller data 8%, seller feedback below 25 adds 8%, seller score below 98% adds 10%, and unknown condition adds 12%. Haircuts compound, are reported, and make seller/condition-uncertain candidates watch-only.
- Low sample count is explicit. For cards, include grading company, grade, language, edition, card number, and variant in comp titles. Add more columns or a typed catalog before using this with other collectibles.
- Seller score, feedback count, condition, shipping, tax, buyer fees, and uncertainty should all reduce the ceiling. Shipping and tax model are simplified and should be calibrated to marketplace checkout totals.

## Current limits

The starter intentionally does not scrape sold listings or claim API production approval. Sold comps are imported by the user. eBay production access to the Buy APIs requires approval, and API availability/terms can change. Verify current requirements before deploying or bidding. This is not a guarantee of winning or profit.

## Tests

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

CI runs the standard-library regression suite on Python 3.11 and 3.12. Production use still needs auction closeout reconciliation and a transactional server-side store; SQLite worker mode is intentionally single-process.

