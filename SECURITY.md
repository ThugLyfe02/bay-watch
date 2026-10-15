# Security policy

## Reporting

Please report security issues privately to the repository owner rather than posting credentials, access tokens, or account data in public issues.

## Operational boundaries

- Never commit `.env`, OAuth tokens, cookies, account passwords, or personal purchase data.
- The local SQLite database contains search history, listing metadata, bid maxima, and audit events. Keep it on a protected device; share only a sanitized copy. Audit serialization redacts common credential fields, and the worker stores a small bid-response summary rather than the full API payload.
- Keep `.env` files and local SQLite databases out of version control; `.gitignore` protects the standard local filenames. Check `git status` before committing any custom export or renamed secret file.
- Use eBay's supported APIs and OAuth scopes. Do not add scraping, private endpoints, CAPTCHA bypass, or browser-session automation.
- Keep bidding disabled by default. Production calls require an explicitly configured production environment and a separately set `ENABLE_LIVE_BIDDING=YES` guard.
- Read-only closeout reconciliation is separate from bidding. Production reads require an explicit `--live-read`; API failures remain unknown and are never treated as losses.
- Bid amounts are user-authored hard maxima. Do not introduce autonomous escalation or retries after an ambiguous bid response.
- Prediction snapshots and learning events are immutable/append-only. Use a sourced correction event; never edit prior evidence in place. Do not store complete API responses, receipts, or unnecessary personal purchase details.
- Calibration references must be linked to a prediction, observed later than it, and use the same currency. Keep completed-market references, auction outcomes, purchase basis, and resale proceeds as distinct evidence types.
- Require a currency-specific open-auction exposure cap before arming; all active intents reserve their full maximum.
- Keep ended `submitting`, `submitted`, and `unknown` intents reserved until a sourced definitive outcome settles them. Elapsed time alone is not resolution.
- SQLite worker mode is single-process. Do not horizontally scale it or place the database on an unreliable network filesystem.

