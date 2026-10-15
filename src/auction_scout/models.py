from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class Listing:
    item_id: str
    title: str
    url: str
    currency: str
    current_price: Decimal
    end_time: datetime
    shipping: Decimal = Decimal("0")
    condition: str = "unknown"
    seller_feedback: int | None = None
    seller_score: Decimal | None = None
    location: str | None = None


@dataclass(frozen=True)
class SaleComp:
    title: str
    sold_at: datetime
    price: Decimal
    shipping: Decimal = Decimal("0")
    condition: str = "unknown"
    source: str = "manual"


@dataclass(frozen=True)
class Valuation:
    fair_value: Decimal
    low: Decimal
    high: Decimal
    comp_count: int
    confidence: str


@dataclass(frozen=True)
class Opportunity:
    listing: Listing
    valuation: Valuation
    max_hammer: Decimal
    all_in_cap: Decimal
    estimated_edge: Decimal
    bid_headroom: Decimal
    eligible: bool
    risk_flags: tuple[str, ...]

