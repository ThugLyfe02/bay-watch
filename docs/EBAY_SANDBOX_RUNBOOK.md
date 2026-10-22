# eBay Sandbox Qualification Runbook

This runbook qualifies the authorized API integration in Sandbox. It does not perform production operations or prove production approval. No credentials, access tokens, or refresh tokens belong in GitHub, audit logs, screenshots, or test fixtures.

## 1. Configure environment-specific credentials

In the eBay Developer Program, use the **Sandbox** application keys. Configure the OAuth RuName/redirect settings for the app and grant buyer consent to a dedicated Sandbox test buyer with the `https://api.ebay.com/oauth/api_scope/buy.offer.auction` scope. Keep the resulting refresh token in a local secret manager or protected shell environment. Do not paste it into an issue, PR, or chat.

The current CLI supports one explicitly configured buyer per process. The operator must complete eBay's user-consent flow in the Developer Program for that buyer and environment; this repository does not host a multi-user OAuth callback or token vault.

```bash
export EBAY_API_ENV=sandbox
export EBAY_MARKETPLACE=EBAY_US
export EBAY_CLIENT_ID='sandbox-client-id'
export EBAY_CLIENT_SECRET='sandbox-client-secret'
export EBAY_USER_REFRESH_TOKEN='sandbox-buyer-refresh-token'
```

Bay Watch mints the Browse application token and refreshes the buyer access token in memory. The refresh-token grant preserves the consented scopes. Tokens are not saved in SQLite. Production requires separate environment keys, a separate consent grant, and the required eBay approvals; do not reuse Sandbox credentials or tokens.

## 2. Verify read-only API access

Run a small Browse query:

```bash
auction-scout api-check --query 'Pokemon Umbreon PSA 7'
```

To verify the buyer's Offer read scope, provide a known Sandbox auction item ID:

```bash
auction-scout api-check --query 'Pokemon Umbreon PSA 7' --offer-item-id 'v1|SANDBOX_ITEM_ID|0'
```

`api-check` only sends GET requests. It validates Browse search, the exact returned item when available, and optionally the buyer's Offer view for the specified auction. A passing check is API connectivity evidence, not a bid test or approval signal.

## 3. Demonstrate the Sandbox bid lifecycle

Use a dedicated Sandbox seller to create a disposable auction and the dedicated Sandbox buyer authorized above. Search for that exact listing and inspect its ID, currency, and end time:

```bash
auction-scout search 'unique sandbox test listing'
```

Set a small Sandbox-only exposure cap, arm the exact user-chosen maximum, and double-check that `EBAY_API_ENV=sandbox` before running the worker:

```bash
auction-scout budget-set --currency USD --amount 25
auction-scout arm 'v1|SANDBOX_ITEM_ID|0' --max 2.00 --currency USD --lead-seconds 30 --confirm 'v1|SANDBOX_ITEM_ID|0'
auction-scout worker
auction-scout intents
```

This sends a bid to a Sandbox listing using fictional funds. It is not a real-world purchase, but it is an external Sandbox action. Confirm the resulting bidder state in the Sandbox account and reconcile only after the auction ends:

```bash
auction-scout reconcile
auction-scout evidence-report
```

If the bid request times out or returns an ambiguous result, Bay Watch records `unknown`; never rerun the worker to retry it. Verify the Sandbox buyer account first. Unit tests cover the ambiguous POST behavior; that is separate from a Sandbox timeout exercise.

## 4. Acceptance record

Capture the branch SHA and mark each item with evidence (redacting account identifiers and tokens):

- [ ] Browse OAuth application token minted from Sandbox client credentials.
- [ ] Browse search returned a Sandbox listing and item detail matched its ID.
- [ ] Offer user token refreshed from Sandbox buyer consent with auction scope.
- [ ] Read-only Offer check returned the expected Sandbox auction status.
- [ ] Worker preflight rejected a deliberately stale/ended test listing without a bid POST.
- [ ] One explicitly authorized proxy maximum was submitted to the disposable Sandbox auction.
- [ ] Auction close was reconciled to definitive Offer API evidence; unknown/conflicting data stayed unresolved.
- [ ] Restart/ambiguous-submission tests passed and no intent was silently retried.

Mocked tests and API connectivity checks must not be described as completing the Sandbox bid lifecycle. Keep a distinction between locally verified behavior, Sandbox verified behavior, and production verified behavior.

## 5. Production gate

The Offer API is [limited release](https://developer.ebay.com/api-docs/buy/static/api-offer.html). eBay requires an approved business use case for Offer API production access, asks applicants to provide their marketplace/user-base use case, and may request an end-to-end Sandbox demonstration. eBay also lists an [Application Growth Check](https://developer.ebay.com/develop/get-started/get-started-on-a-buying-application) as a required step for restricted Buy API production access. Do not switch to production until approvals, required agreements, production keyset access, and a separate production validation plan are confirmed by eBay. A successful OAuth request or `api-check --live-read` cannot establish any of those approvals.
