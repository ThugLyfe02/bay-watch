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

## 3. Compliant discovery coverage — query provenance implemented

Track each Browse query/alias run and every listing ID it returned, then report run freshness, API error class, and duplicate yield. Users can provide title aliases directly. `record-missed` captures an append-only, source-referenced user example. Saved category profiles now preserve query aliases and declared identity-review fields as immutable versions; profile searches link every API run to the version used, so later edits do not rewrite historical coverage. Total marketplace recall remains explicitly unknown. Remaining work: richer feedback-to-profile analysis and reviewed shadow evaluations after evidence accumulates.

**Why it compounds:** broad title vocabulary and category-specific language expose under-described listings earlier, while coverage telemetry makes search improvements measurable.

**Acceptance bar:** deduplicated listings retain all matching query provenance; API errors and freshness are visible; user-provided missed examples have a source reference and are reproducible; no scraping, private endpoints, or anti-bot bypass. Current query coverage is measured only from official Browse results and user-provided examples, and does not claim complete recall.

## 4. Portfolio-aware close and capital allocator — read-only planning foundation

The `plan` report ranks saved candidates within each currency using visible headroom, confidence, comp count, evidence freshness, and close time. It reports caps, reservations, queue depth, and close-window warnings without changing any user maximum or submitting bids. Further work: intent-level latency evidence and an explicit capacity model before proposing allocation changes.

**Why it compounds:** capital and API capacity go to the best-supported opportunities, not simply the earliest rows in a queue. The worker now refreshes each listing before bidding and warns when due work exceeds its concurrency; scheduling should later use measured latency and expected risk-adjusted surplus.

**Acceptance bar:** rankings are deterministic and auditable; hard caps cannot be exceeded; unknown bid outcomes retain their reservation until verified; overload is visible before a close window. Current report is advisory and currency-separated.

## 5. Production reliability gate

The code now supports ephemeral client-credential token minting and buyer refresh-token renewal, plus a read-only `api-check` and Sandbox qualification runbook. A real Sandbox bid lifecycle has not yet been verified. Richer worker restart and latency observability remains on the roadmap. Capture claim, preflight, and submission timing; no-bid reasons; restart recovery; and ambiguous POST states before using operational rates to tune execution. Move from the current single-process SQLite worker to a transactional server-side queue only when measured concurrency requires it. Add idempotent claims, clock-skew checks, alerting, credential rotation guidance, and a paper-trading period before any production bid flow.

**Why it compounds:** reliability preserves the value of the research layer and prevents duplicate or late submissions as the number of watched auctions grows.

**Acceptance bar:** fault-injection coverage for timeout, process restart, clock skew, and duplicate workers; production remains disabled until official API access, end-to-end reconciliation, and operator runbooks are verified.

## Build order

1. Closeout learning loop and evidence snapshots.
2. Evidence-first opportunity dossier.
3. Search coverage telemetry and more permitted data sources.
4. Portfolio-aware scheduling, informed by real concurrency and outcome data.
5. Server-side queue and production reliability gates when usage justifies them.

## Evidence accumulation gate

Before calibration or portfolio policy is changed, collect real linked market references, definitive auction outcomes, actual checkout records, and completed resale records. Use `evidence-report` to monitor sample counts and missing links. Record missed listings only with user-provided evidence. When samples exist, add chronological, versioned shadow evaluations by saved category profile; compare against the current policy and simple baseline, require explicit review, and never promote automatically into bidding. Measured worker latency and restart/fault recovery remain later observability work; a submitting/unknown intent must stay unresolved and must never be retried on restart.

The competitive edge should be measured as improved candidate coverage, lower valuation error, fewer bad buys, realized net margin, and missed-close rate. Bid speed alone is not the success metric.
