# Bay Watch strategic roadmap

Bay Watch should win through better decisions and dependable execution, not through hidden endpoints or artificial urgency. The durable advantage is a learning loop: find more relevant auctions, reject false bargains early, and improve estimates from what actually happened.

## 1. Closeout learning loop — implemented foundation; expand with evidence

Record each screened candidate's immutable evidence snapshot, estimate, confidence, ceiling, and decision. Reconcile definitive ended status through a read-only official API call; keep missing or conflicting results unresolved. For purchases, users can append checkout basis and eventual resale proceeds so reports calculate realized net margin after fees, shipping, and refunds. The first calibration report compares predictions with later linked completed-market references and keeps auction and resale outcomes separate.

**Why it compounds:** the user's own, category-specific error history reveals where the valuation model is optimistic or too conservative. Better calibration improves every later bid without raising anyone's maximum automatically.

**Acceptance bar:** every prediction is reproducible from saved inputs; reconciliation distinguishes confirmed, missing, and unknown outcomes; calibration reports sample size, absolute/signed error, interval coverage, and monthly drift; no automated bid increase. Remaining work: enrich identity-match evidence and add reviewed shadow policy comparisons before proposing any estimator change.

## 2. Evidence-first opportunity dossier — initial export implemented

Export each saved prediction as JSON or readable text with listing facts, identity attributes, comp sources/dates/currencies, transparent exclusions, seller and condition risks, all-in ceiling, expected headroom, confidence, and missing facts. Expand it into the full interactive operator dossier after per-comp estimator audit trails and freshness measurements exist.

**Why it compounds:** careful bidders can review a lot more candidates without trusting a mysterious score. Strong evidence is persuasive; uncertain listings remain visibly uncertain. Time pressure comes from the auction clock, never fabricated scarcity or guaranteed-return claims.

**Acceptance bar:** every score links to its inputs; missing data reduces confidence; exact identity conflicts block eligibility; users can export the dossier for later review. The first CLI export is present; auction countdown and richer identity-conflict explanations remain future work.

## 3. Compliant discovery coverage

Expand saved query profiles and alias sets, then track which searches found each listing and which search gaps produced missed candidates. Add sources only through documented, permitted APIs or user-provided data, with source and freshness recorded on every observation.

**Why it compounds:** broad title vocabulary and category-specific language expose under-described listings earlier, while coverage telemetry makes search improvements measurable.

**Acceptance bar:** deduplicated listings retain all matching query provenance; API limits and freshness are visible; no scraping, private endpoints, or anti-bot bypass.

## 4. Portfolio-aware close and capital allocator

When many auctions overlap, rank due intents by estimated risk-adjusted surplus and available capital, while preserving the user's explicit per-item maximum and currency exposure cap. Add closeout reconciliation before releasing an unresolved reservation. Show any intents that may miss their close because the configured worker capacity is insufficient.

**Why it compounds:** capital and API capacity go to the best-supported opportunities, not simply the earliest rows in a queue. The worker now refreshes each listing before bidding and warns when due work exceeds its concurrency; scheduling should later use measured latency and expected risk-adjusted surplus.

**Acceptance bar:** allocations are deterministic and auditable; hard caps cannot be exceeded; unknown bid outcomes retain their reservation until verified; overload is visible before a close window.

## 5. Production reliability gate

Move from the current single-process SQLite worker to a transactional server-side queue only when measured concurrency requires it. Add idempotent claims, monitored API latency, clock-skew checks, alerting, credential rotation guidance, and a paper-trading period before any production bid flow.

**Why it compounds:** reliability preserves the value of the research layer and prevents duplicate or late submissions as the number of watched auctions grows.

**Acceptance bar:** fault-injection coverage for timeout, process restart, clock skew, and duplicate workers; production remains disabled until official API access, end-to-end reconciliation, and operator runbooks are verified.

## Build order

1. Closeout learning loop and evidence snapshots.
2. Evidence-first opportunity dossier.
3. Search coverage telemetry and more permitted data sources.
4. Portfolio-aware scheduling, informed by real concurrency and outcome data.
5. Server-side queue and production reliability gates when usage justifies them.

The competitive edge should be measured as improved candidate coverage, lower valuation error, fewer bad buys, realized net margin, and missed-close rate. Bid speed alone is not the success metric.
