# Bay Watch

Bay Watch is an eBay-first auction research and bid workbench. It searches the official Browse API, scores a listing against user-supplied sold comps, calculates a conservative all-in ceiling, and can schedule one exact proxy-bid maximum through eBay's official Offer API.

## Guardrails

- Uses official eBay Buy APIs only. No page scraping, private endpoints, CAPTCHA handling, browser/password automation, or anti-bot evasion.
- No valuation is fabricated: `analyze` requires a sold-comps CSV and visibly reports confidence and comp count.
- A bid intent requires the exact item ID and exact maximum amount. The maximum cannot be escalated by the worker.
- Each currency requires an explicit open-auction exposure cap before arming. Budget reservations are atomic across local CLI attempts and include the full maximum bid for active auctions.
- Sold comps with a different currency are excluded. Bid rounding and amount precision follow currency minor units, including zero- and three-decimal currencies.
- Worker claims an intent before network I/O and never auto-retries a bid with an ambiguous outcome. Verify the account on eBay before taking any further action.
- Before the one-way bid request, the worker refreshes the exact item through Browse and checks auction state, item ID, currency, minimum bid, Buy It Now ceiling, and remaining time. A failed preflight means no bid POST was sent.
- Browse uses an application token; Offer uses a buyer-consented user token. Configure the environment-specific Client ID/Secret, then provide the buyer's long-lived OAuth refresh token with the Offer auction scope. Bay Watch mints/refreshes short-lived tokens in memory; it never writes tokens to SQLite. Static access-token overrides are supported for short manual checks only.
- Production mode requires `EBAY_API_ENV=production`, `ENABLE_LIVE_BIDDING=YES`, and `--live`. eBay's Buy Offer API production access is partner-gated; sandbox is the default.
- Keep tokens in a secrets manager. Do not use your eBay password or commit credentials.
- Prediction snapshots and verified outcome evidence are append-only. A timeout or expired local intent remains unresolved; it is never recorded as a loss.
- `reconcile` uses a read-only Offer API request and records only a definitive ended result. It never retries or submits a bid. Production reconciliation requires `--live-read`.
- Calibration compares saved predictions only with a later, explicitly linked `market_reference` completed sale in the same currency. Auction hammer prices and resale results remain separate targets.

## Install and run

```bash
cd auction-scout
python -m venv .venv && . .venv/bin/activate
pip install -e .
cp .env.example .env  # populate values in your shell or a local env loader
# Configure Sandbox credentials and buyer consent. The user refresh token must be minted
# for this environment and include the Offer auction scope.
export EBAY_CLIENT_ID='...'
export EBAY_CLIENT_SECRET='...'
export EBAY_USER_REFRESH_TOKEN='...'        # buyer consent for Offer auction scope
export EBAY_API_ENV=sandbox
auction-scout api-check --query "Pokemon Umbreon PSA 7"
auction-scout search "2000 Japanese Umbreon holo PSA 7" --limit 25
# Search alternate catalog wording and deduplicate by item ID:
auction-scout search "Umbreon PSA 7" --variant "Japanese Umbreon grade 7" --variant "Umbreon #197 holo" --limit 20
```

Search stores normalized results in SQLite and prints JSONL. Create `sold-comps.csv` with columns:

```csv
title,sold_at,price,shipping,condition,source,currency
Pokemon Umbreon holo PSA 7,2026-09-12T18:00:00Z,210.00,0,graded,manual,USD
```

Before exercising the worker, run `api-check`. It performs read-only GETs; add `--offer-item-id` to verify Offer read authorization against a known Sandbox auction. It never places a bid and cannot establish production approval. For production read checks, explicitly pass `--live-read`; the worker still requires separate bidding guards. The Sandbox buyer refresh token must come from an eBay consent grant configured for Sandbox and the `buy.offer.auction` scope. Production uses separate credentials, redirect/RuName settings, and consent.

Follow [the Sandbox qualification runbook](docs/EBAY_SANDBOX_RUNBOOK.md) before testing the worker against a Sandbox auction. A successful read-only check is not a bid-lifecycle test.

Then analyze, set the total open-auction exposure cap, and arm a hard ceiling:

```bash
auction-scout analyze 'v1|ITEM_ID|0' --comps sold-comps.csv --tax 0.08875 --buffer 0.10
auction-scout budget-set --currency USD --amount 500
auction-scout budget-status --currency USD
auction-scout arm 'v1|ITEM_ID|0' --max 175.00 --currency USD --lead-seconds 30 --confirm 'v1|ITEM_ID|0'
auction-scout intents
auction-scout worker --loop
```

For a single ranked scan, use `opportunities`; it searches the primary query and aliases, deduplicates listings, screens to a deadline window, applies the comp and cost model, and ranks by eligibility/headroom:

```bash
auction-scout opportunities "Umbreon PSA 7" --variant "Japanese Umbreon grade 7" --comps sold-comps.csv --tax 0.08875 --buffer 0.10 --max-hours 72
```

Only medium/high confidence matches with a nonnegative ceiling headroom and a specified condition are marked eligible. The report still includes lower-confidence candidates as watch-only so thin evidence is visible.

The worker refreshes each listing through Browse immediately before the bid, then posts the exact user-authored maximum through Offer. Its 25-second minimum lead leaves room for the preflight request, bid request, polling, and scheduling margin. A stale or unsafe listing fails closed without sending a bid. Simultaneous intents use bounded parallel submission; an oversized due batch is warned because queued requests may miss close. eBay's proxy bidding system accepts the buyer's maximum and bids only as needed. Do not enter an amount above your personal all-in budget. For a production account, after approval:

```bash
export EBAY_API_ENV=production
export ENABLE_LIVE_BIDDING=YES
auction-scout worker --live --loop
```

Do not run multiple workers against the same database. SQLite state transition prevents double claims for a single local worker, but production deployment should use a single scheduler or move claim semantics to a transactional server database before horizontal scaling.

Review observed execution behavior with `worker-report`. It reports recorded run queue depth/overload, local monotonic preflight and submission durations, stable fail-closed reason codes, sanitized failure classes, and active unresolved intent age. Runs are written only when due work exists, so an idle polling loop does not grow the database at poll frequency. An interrupted run remains visible as incomplete; unresolved intents continue to reserve their exact maximum. The report describes observed samples and does not calculate a missed-close rate until reviewed execution-miss labels and outcomes exist:

```bash
auction-scout worker-report
```

## Closeout learning and review

Every `analyze` and `opportunities` decision saves a prediction snapshot with the listing facts, valuation range, confidence, comp CSV inputs, policy version, risk flags, and cost assumptions. Arming stores the exact user maximum in a separate armed-decision snapshot. Snapshots and evidence events cannot be updated or deleted through the application.

After an auction, reconcile the read-only official API evidence, or enter a user-confirmed result with a source reference. Record checkout basis and eventual resale separately; purchase `--amount` is the actual all-in checkout total. Resale `--amount` is gross proceeds, while fees, shipping, refunds, and optional `--other-costs` are explicit deductions:

```bash
auction-scout reconcile
auction-scout record-outcome ITEM_ID --kind purchase --state recorded --source receipt --evidence-ref receipt-123 --currency USD --amount 212.45
auction-scout record-outcome ITEM_ID --kind resale --state recorded --source completed-sale --evidence-ref order-456 --currency USD --amount 310 --fees 32 --shipping 9 --refunds 0
auction-scout calibration
auction-scout margin-report
auction-scout dossier ITEM_ID --format text
```

`calibration` reports the saved point estimate against only later verified completed-sale references recorded as `market_reference`; its target is item price plus buyer-paid shipping, matching the estimator input. It does not treat an auction win or an estimated resale value as market truth. Cohorts below five remain marked too small. `margin-report` emits realized net profit only when actual purchase basis and a completed resale are recorded in the same currency. Unsold inventory appears with an estimated value and remains unrealized.

`search` and `opportunities` also preserve each alias query run and the listing IDs it surfaced. Use `discovery-report` to inspect recorded coverage, freshness, API errors, and duplicate yield. This describes Bay Watch's query activity; total marketplace recall remains unknown. A reviewed manual auction result can settle a reservation by passing its `--intent-id` to `record-outcome`.

Save a reusable category search profile to keep its category vocabulary and identity fields versioned. Every profile edit creates a new immutable version; `profile-search --version N` can replay an earlier version, with each alias query and result linked to that exact version. Reports count only recorded API results and user-evidenced misses, never inferred marketplace recall:

```bash
auction-scout profile-save --name pokemon-cards --category "Trading cards" \
  --query "Umbreon PSA 7" --query "Umbreon #197 holo" \
  --identity-field grade --identity-field card_number --identity-field language
auction-scout profiles
auction-scout profile-search pokemon-cards --limit 30
auction-scout profile-search pokemon-cards --version 1 --limit 30
auction-scout discovery-report
```

`profile-search` is user initiated and uses only the official Browse API. Identity fields document which attributes the user expects to review; they do not authenticate an item or override the existing identity and eligibility checks. After reviewing a concrete miss, associate feedback with the exact profile version used via `record-missed --profile pokemon-cards --profile-version 1`. Historical reports retain old profile versions and show legacy/unversioned miss examples separately.

Use `record-missed` only when you have a concrete listing and a reviewable source reference. It appends a user-evidenced example against the query/profile you expected to find it; this helps refine aliases but does not establish a marketplace-wide recall denominator. `evidence-report` tracks prediction snapshots still missing verified market references, calibration sample/error availability, resolved versus unresolved intent evidence, recorded resale economics, query coverage, and the count of user-evidenced misses. Zero samples are reported as unmeasurable rather than as zero error or zero missed closes:

```bash
auction-scout record-missed 'v1|123456|0' --query 'Umbreon PSA 7' --source user_report \
  --evidence-ref 'https://www.ebay.com/itm/123456' --profile pokemon-cards --reason 'title omitted card number'
auction-scout evidence-report
```

Use `plan` to review saved candidates alongside currency-specific caps, active reservations, close timing, and worker-queue warnings. It uses visible ranking components and never edits a user maximum or submits bids.

## What makes the research layer useful

- Search title variants separately (graded/raw, set/number, language, first edition) so near-miss nomenclature is surfaced without blending distinct assets into one valuation.
- Comp matching uses transparent token overlap, recency decay, condition penalty, and median/MAD outlier resistance. Treat it as triage; authenticate and inspect the item yourself.
- For Pokémon cards, known mismatches in card number, grading company, grade, language, edition, and holo variant are excluded before valuation. Missing identity details remain a confidence limitation rather than being silently inferred.
- Risk flags now change the bid ceiling: thin comps apply a 12% haircut, incomplete seller data 8%, seller feedback below 25 adds 8%, seller score below 98% adds 10%, and unknown condition adds 12%. Haircuts compound, are reported, and make seller/condition-uncertain candidates watch-only.
- Low sample count is explicit. For cards, include grading company, grade, language, edition, card number, and variant in comp titles. Add more columns or a typed catalog before using this with other collectibles.
- Seller score, feedback count, condition, shipping, tax, buyer fees, and uncertainty should all reduce the ceiling. Shipping and tax model are simplified and should be calibrated to marketplace checkout totals.

The prioritized path to a durable edge is in [ROADMAP.md](ROADMAP.md): closeout learning, evidence-first opportunity dossiers, compliant search coverage, portfolio-aware scheduling, and production reliability gates.

## Current limits

The starter intentionally does not scrape sold listings or claim API production approval. Sold comps are imported by the user. eBay production access to the Buy APIs requires approval, and API availability/terms can change. Verify current requirements before deploying or bidding. This is not a guarantee of winning or profit.

## Database upgrades

The SQLite database migrates transactionally using `PRAGMA user_version`. Version 7 adds an append-only user-evidenced missed-listing ledger to the v6 prediction, outcome, discovery, intent-settlement, and cost-accounting tables. OAuth tokens are ephemeral and do not require a database migration. Opening a pre-migration database preserves its listings, intents, budget limits, and audit history. Ended unresolved intents keep their full maximum reserved until definitive evidence settles them. Back up the local database before upgrading. SQLite remains single-process.

## Tests

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

CI runs the standard-library regression suite on Python 3.11 and 3.12. The current learning loop supports read-only closeout reconciliation and user-entered evidence. The worker remains single-process; production Offer API access remains subject to eBay approval.

