# Security policy

## Reporting

Please report security issues privately to the repository owner rather than posting credentials, access tokens, or account data in public issues.

## Operational boundaries

- Never commit `.env`, OAuth tokens, cookies, account passwords, or personal purchase data.
- The local SQLite database contains search history, listing metadata, bid maxima, and audit events. Keep it on a protected device; share only a sanitized copy. Audit serialization redacts common credential fields, and the worker stores a small bid-response summary rather than the full API payload.
- Keep `.env` files and local SQLite databases out of version control; `.gitignore` protects the standard local filenames. Check `git status` before committing any custom export or renamed secret file.
- Use eBay's supported APIs and OAuth scopes. Do not add scraping, private endpoints, CAPTCHA bypass, or browser-session automation.
- Keep bidding disabled by default. Production calls require an explicitly configured production environment and a separately set `ENABLE_LIVE_BIDDING=YES` guard.
- Bid amounts are user-authored hard maxima. Do not introduce autonomous escalation or retries after an ambiguous bid response.
- Require a currency-specific open-auction exposure cap before arming; all active intents reserve their full maximum.
- SQLite worker mode is single-process. Do not horizontally scale it or place the database on an unreliable network filesystem.

