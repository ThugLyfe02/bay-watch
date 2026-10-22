# Bay Watch — Evidence Accumulation and Reviewed Shadow Evaluation Master Prompt

You are continuing engineering inside `ThugLyfe02/bay-watch`, PR #1:
<https://github.com/ThugLyfe02/bay-watch/pull/1>

Work only on the live PR branch `feat/auction-scout-foundation`. At the start of every continuation, fetch the current PR, branch head, changed-file diff, and latest CI runs. Treat any supplied SHA as a navigation hint, never as authority to work from a stale snapshot. Preserve the draft PR. Do not create another PR, merge, publish a release, enable production bidding, or place real bids.

## Mission

Build Bay Watch as an evidence-led auction research and execution workbench that improves decisions only when verified data supports a change. Build a durable advantage through compliant discovery, transparent identity and valuation evidence, calibrated uncertainty, deliberate capital use, exact user-authorized proxy maxima, verified closeout records, and reliable recovery.

The current system has no verified real auction outcome or resale records. Therefore it has no measured calibration error, realized margin, false-positive rate, or missed-close rate. Treat unavailable metrics as unavailable. Do not manufacture example records in a user's operational database or substitute synthetic test fixtures for real-world performance claims. Tests may use clearly artificial fixtures, kept separate from reported product metrics.

Discovery reports describe recorded query runs and returned listings. They do not reveal all listings available on the marketplace. User-reported missed listings can expose specific search gaps, but cannot establish total recall or a population-level false-negative rate. eBay Buy API production access remains approval-gated, and actual production behavior is unverified.

## Verify before changing anything

1. Fetch the live PR #1, branch ref, current head SHA, full diff, CI results, README, SECURITY, ROADMAP, schema, worker, CLI, and tests.
2. Confirm PR #1 is still open, draft, and unmerged. Stop if the branch head moved unexpectedly; fetch and inspect the new head before proceeding.
3. Run the existing complete test suite and compile checks before edits. Record the baseline.
4. Identify the current migration version and transaction strategy. Read the actual schema and APIs; do not assume earlier features exist solely because this prompt mentions them.
5. Trace one saved candidate through search/provenance → stored listing → comps → immutable prediction → dossier → optional armed intent → preflight → submission state → audit/outcome evidence.
6. List concrete invariant gaps and implement only work that improves evidence quality, measurable coverage, calibration readiness, closeout completeness, or operational recovery.

## Preserve these invariants

- Use documented, authorized marketplace APIs and user-provided data only. No scraping, private endpoints, browser-session automation, CAPTCHA handling, traffic camouflage, or platform-control evasion.
- An armed user's maximum is immutable. Models, schedules, shadow evaluations, alerts, and portfolio plans cannot raise it.
- Never retry an ambiguous bid POST automatically. Keep `submitting`/`unknown` unresolved until evidence is reviewed.
- Fail closed when identity, item state, currency, minimum bid, timing, or authorization is uncertain.
- Production bidding stays disabled and approval-gated. Mock bid submission in tests. Never use a real buyer account or place a real bid.
- Never invent item identity, sale price, fill, condition, outcome, margin, or recall denominator. Missing stays missing.
- Keep currency explicit, amounts in Decimal/minor units, and timestamps timezone-aware UTC.
- Do not persist credentials, cookies, token-bearing errors, or unnecessary full API payloads.
- SQLite remains single-process. Do not imply horizontal worker safety.
- No fake scarcity, pressure countdowns, guaranteed returns, or user manipulation.

## Current evidence state and reporting contract

Use `evidence-report`, `calibration`, `margin-report`, `discovery-report`, and source records as the canonical evidence surfaces. As data accumulates, make these counts change from saved evidence rather than hand-maintained claims.

For every report:

- Show the metric definition, target, unit/currency where relevant, sample count, observation period, evidence-source breakdown, and missing/unresolved count.
- Return null/“not measurable” when no valid sample exists. Never report zero error or zero loss merely because no outcomes were recorded.
- Separate linked completed-market reference calibration from auction hammer outcomes and resale economics. These are distinct targets.
- Calculate realized net profit only from actual recorded resale proceeds, same-currency actual purchase basis, and explicit resale fees, shipping, refunds, and other costs. Keep unsold inventory estimates and expected headroom separate.
- Keep verified API observations separate from user-confirmed/manual/imported evidence and make source/time/confidence visible.
- Retain unresolved or conflicting outcomes in the denominator of “missing” until corrected by append-only evidence.
- Count user-evidenced missed listings as examples only. Explicitly state that total marketplace recall and population false-negative rate are unknown.
- Preserve query provenance across deduplication. Coverage measures recorded API queries and results, not all eligible listings.
- Do not claim false-positive rate until a defensible labeling protocol defines a candidate as a false positive and verified outcome records support it. Do not claim missed-close rate until close intent, worker capacity, timing, and verified missed execution labels are consistently captured.

## Next build sequence

### Stage 0 — OAuth and Sandbox qualification

- Mint application credentials with eBay's client-credentials flow and use a buyer-consented authorization-code refresh token for Offer. Keep access tokens in memory; do not write credentials or tokens to the database or audit log.
- Provide a repeatable read-only API check that validates Browse search/item details and, when given a known test auction, Offer buyer-read authorization. It must never send a bid and must require an explicit flag for production GETs.
- Exercise the complete buyer flow in Sandbox using a dedicated test buyer and known test auction. Record which calls were verified and which were blocked. Unit tests with mocked clients do not count as Sandbox acceptance.
- Keep production Offer approval, application growth review, and production token consent as external gates. The application must not infer approval from successful token minting.

Current implementation: `EbayTokenProvider` mints/refreshes access tokens in memory; `api-check` is read-only; `docs/EBAY_SANDBOX_RUNBOOK.md` defines the manual Sandbox qualification sequence. This does not mean any Sandbox API credentials or end-to-end bid flow have been verified.

### Stage A — Evidence accumulation and gap visibility

Keep extending the existing append-only evidence primitives rather than adding parallel ledgers.

- Make recording a verified auction result, completed-market reference, purchase, resale, or user-evidenced missed listing idempotent and auditable.
- Require a source and reviewable evidence reference. Preserve observation time, confidence/source class, currency, and the prediction/profile linkage where applicable.
- Corrections append a linked event; do not overwrite prior evidence.
- Ensure `evidence-report` shows prediction snapshots without later references, calibration samples and errors only when defined, realized resale counts/net by currency, auction outcomes by source/state, unresolved intents by state/age, discovery freshness/errors, and user-evidenced missed-listing counts by profile/query.
- Reports must distinguish no record, unknown, verified negative, and verified positive outcomes.
- Ensure repeated report generation/reconciliation does not mutate evidence or duplicate events.

### Stage B — Saved category profiles and controlled discovery experiments

Add reusable, versioned category profiles only when the schema/API can preserve reproducibility.

- A profile contains a stable ID, human-readable name, category, repeatable permitted title aliases, typed identity fields, field policies (required/high-impact/optional), source/marketplace, and version history.
- Unknown identity remains distinct from match. A known high-impact conflict blocks eligibility.
- Save which profile version and exact aliases produced each query run and prediction.
- Provide a CLI workflow to create, inspect, revise, and run a profile without deleting earlier versions or query provenance.
- Compare aliases using bounded, reviewable experiments. Report query yield, unique items, overlap, API errors, freshness, and user-evidenced misses; do not label one profile “higher recall” without a valid denominator.
- Continue to use only documented APIs or user-imported files. Record provenance/licensing metadata for imported comps.

### Stage C — Chronological shadow evaluation, human reviewed

Only after verified target data exists, compare candidate policy versions against the current policy without affecting decisions or bids.

- Freeze the evaluated prediction set and outcome target definition before comparison.
- Use only data available at each prediction timestamp. Block future comps, corrected later identities, or later outcomes from leaking into earlier inputs.
- Compare the candidate policy with the current policy and a simple baseline on the same eligible samples; segment only where sample size supports a conclusion.
- Report sample coverage, mean/median absolute error, signed error, interval coverage, drift by time, and exclusions/missing outcomes.
- Keep target types separate: later completed-market references for valuation calibration, auction results for execution/closeout, and completed resale events for realized net economics.
- Store evaluation inputs, code/policy versions, cohort, and output reproducibly.
- Mark all results `shadow_only` and require named human review before changing policy. Never auto-promote a policy, haircut, confidence tier, or maximum bid into the bidding path.
- If no evidence sample exists, report that shadow comparison is blocked by insufficient evidence.

### Stage D — Worker restart and latency observability

Improve recovery without weakening one-shot submission safety.

- Capture monotonic durations and UTC timestamps for claim, preflight, bid request, and state transition. Record bounded, sanitized failure classes rather than raw response bodies or credentials.
- Report latency distributions only with counts, periods, and enough samples; expose queue depth, concurrency, overload, stale preflight, no-bid reason, submitted/rejected/unknown outcomes, and close timing.
- Add restart/fault tests for crash after claim and before POST, timeout after possible server acceptance, duplicate local workers claiming one intent, and restart while `submitting`/`unknown`.
- A restarted process must not silently repost `submitting`/`unknown` intents. Recovery requires read-only reconciliation or explicit user-directed action backed by evidence.
- Preserve complete authorized maxima as reserved capital until definitive evidence settles the intent.
- Keep SQLite single-process. Move to a transactional queue only when measured workload establishes the need; document the migration boundary first.

## Implementation and release discipline

- Use versioned transactional migrations and test upgrades from the prior real schema with existing listing, budget, intent, audit, prediction, and outcome rows preserved.
- Keep domain/report logic separate from CLI formatting. Use small typed functions for capture, reconciliation, calibration, shadow comparison, and reporting.
- Use tests for meaningful invariants, with mocked API clients. Include idempotence, append-only corrections, temporal leakage, currency separation, unknown outcomes, reservation retention, and restart/no-retry behavior.
- Update README, SECURITY, ROADMAP, and this prompt when behavior or acceptance changes.
- Run the complete unittest suite, compileall, CLI help checks, and supported Python CI. Inspect the final diff for privacy, raw payload retention, stale-base hazards, migration risks, and scope drift.
- Confirm the PR remains open, draft, and unmerged. Do not deploy, publish, merge, enable production bidding, or place bids.

## Delivery response

Report:

1. What changed and exact files/commands.
2. Live PR link and final branch head SHA.
3. Baseline and final test/CI evidence.
4. Migration version and upgrade implications.
5. Current evidence counts, including explicit zero/unknown gaps and what is not measurable.
6. Whether any real outcomes or resale records were collected in this work (normally no).
7. Production API approval and behavior status, stated as unverified unless independently verified.
8. The next evidence-backed build step.

Do not claim a competitive advantage, profitability, “jaw-dropping” result, measured calibration, measured recall, false-positive rate, or missed-close rate unless the saved evidence and metric definition support it.
