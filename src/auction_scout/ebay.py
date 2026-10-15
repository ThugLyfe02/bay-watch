from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

from .models import Listing
from .money import format_money


class EbayError(RuntimeError): pass


class EbayClient:
    """Thin official eBay Buy API client. No HTML scraping, browser automation, or credential capture."""
    ROOTS = {"sandbox": "https://api.sandbox.ebay.com", "production": "https://api.ebay.com"}

    def __init__(self, token: str | None = None, env: str | None = None, marketplace: str | None = None):
        self.token = token or os.getenv("EBAY_USER_ACCESS_TOKEN", "")
        self.env = (env or os.getenv("EBAY_API_ENV", "sandbox")).casefold()
        self.marketplace = marketplace or os.getenv("EBAY_MARKETPLACE", "EBAY_US")
        if self.env not in self.ROOTS: raise ValueError("EBAY_API_ENV must be sandbox or production")
        if not self.token: raise ValueError("EBAY_USER_ACCESS_TOKEN is required")
        self.root = self.ROOTS[self.env]

    def _request(self, method: str, path: str, *, query: dict | None = None, body: dict | None = None, retries: int = 2):
        url = self.root + path + (("?" + urlencode(query)) if query else "")
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json",
                   "X-EBAY-C-MARKETPLACE-ID": self.marketplace}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        for attempt in range(retries + 1):
            req = Request(url, data=data, headers=headers, method=method)
            try:
                with urlopen(req, timeout=8) as response:
                    content = response.read()
                    return json.loads(content) if content else {}
            except HTTPError as e:
                message = e.read().decode(errors="replace")[:1000]
                if e.code == 429 and attempt < retries:
                    time.sleep(min(2 ** attempt, 4)); continue
                # Never retry bid submission on 5xx/ambiguous responses.
                raise EbayError(f"eBay returned HTTP {e.code}: {message}") from e
            except (URLError, TimeoutError) as e:
                if method != "POST" and attempt < retries:
                    time.sleep(min(2 ** attempt, 4)); continue
                raise EbayError(f"eBay request outcome uncertain: {e}") from e
        raise EbayError("request exhausted retries")

    def search_auctions(self, query: str, limit: int = 30) -> list[Listing]:
        payload = self._request("GET", "/buy/browse/v1/item_summary/search",
                                query={"q": query, "filter": "buyingOptions:{AUCTION}", "limit": min(max(limit, 1), 200)})
        out = []
        for row in payload.get("itemSummaries", []):
            price = row.get("currentBidPrice") or row.get("price") or {}
            shipping = Decimal("0")
            opts = row.get("shippingOptions") or []
            if opts and opts[0].get("shippingCost", {}).get("value"):
                shipping = Decimal(opts[0]["shippingCost"]["value"])
            end = row.get("itemEndDate")
            if not end or not price.get("value"): continue
            out.append(Listing(
                item_id=row["itemId"], title=row.get("title", ""), url=row.get("itemWebUrl", ""),
                currency=price.get("currency", "USD"), current_price=Decimal(price["value"]),
                end_time=datetime.fromisoformat(end.replace("Z", "+00:00")), shipping=shipping,
                condition=row.get("condition", "unknown"),
                seller_feedback=row.get("seller", {}).get("feedbackScore"),
                seller_score=Decimal(str(row["seller"]["feedbackPercentage"])) if row.get("seller", {}).get("feedbackPercentage") is not None else None,
                location=(row.get("itemLocation") or {}).get("city")))
        return out

    def place_proxy_bid(self, item_id: str, amount: Decimal, currency: str):
        # The endpoint validates bidder eligibility and auction state. Production access is gated by eBay.
        return self._request("POST", f"/buy/offer/v1_beta/bidding/{quote(item_id, safe='')}/place_proxy_bid",
                             body={"maxAmount": {"currency": currency, "value": format_money(amount, currency)}}, retries=0)

