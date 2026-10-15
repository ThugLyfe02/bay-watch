from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
from statistics import median
import re

from .models import Listing, Opportunity, SaleComp, Valuation
from .identity import compatible_titles


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.casefold()) if len(t) > 1}


def title_similarity(a: str, b: str) -> float:
    """Token Jaccard similarity; intentionally transparent, not a black-box value claim."""
    left, right = _tokens(a), _tokens(b)
    return len(left & right) / len(left | right) if left and right else 0.0


def estimate_value(listing: Listing, comps: list[SaleComp], *, now: datetime | None = None) -> Valuation | None:
    now = now or datetime.now(timezone.utc)
    relevant: list[tuple[Decimal, float]] = []
    for comp in comps:
        if comp.price <= 0 or comp.sold_at.tzinfo is None:
            continue
        if not compatible_titles(listing.title, comp.title):
            continue
        sim = title_similarity(listing.title, comp.title)
        if comp.condition.casefold() not in {"unknown", listing.condition.casefold()}:
            sim *= 0.65
        age_days = max(0.0, (now - comp.sold_at).total_seconds() / 86400)
        recency = 0.5 ** (age_days / 120.0)
        if sim >= 0.25:
            relevant.append((comp.price + comp.shipping, sim * recency))
    if not relevant:
        return None
    prices = [p for p, _ in relevant]
    med = median(prices)
    deviations = [abs(p - med) for p in prices]
    mad = median(deviations) if deviations else Decimal(0)
    # Downweight outliers rather than pretending thin collectible data is precise.
    clipped = [min(max(p, med - 3 * mad), med + 3 * mad) for p in prices] if mad else prices
    weights = [w for _, w in relevant]
    fair = sum((p * Decimal(str(w)) for p, w in zip(clipped, weights)), Decimal(0)) / Decimal(str(sum(weights)))
    spread = max(mad * Decimal("1.5"), fair * Decimal("0.08"))
    count = len(relevant)
    weight_sum = sum(weights)
    effective_count = weight_sum * weight_sum / sum(w * w for w in weights)
    confidence = "high" if effective_count >= 12 else "medium" if effective_count >= 5 else "low"
    return Valuation(fair_value=fair, low=max(Decimal(0), fair - spread), high=fair + spread,
                     comp_count=count, confidence=confidence)


def analyze(listing: Listing, valuation: Valuation, *, tax_rate: Decimal = Decimal("0"),
            reserve_buffer: Decimal = Decimal("0.08"), buyer_fee_rate: Decimal = Decimal("0"),
            risk_discount: Decimal = Decimal("0")) -> Opportunity:
    """Compute a conservative hammer ceiling from an all-in cap, never a suggested bid escalation."""
    if not (Decimal(0) <= tax_rate < Decimal(1) and Decimal(0) <= buyer_fee_rate < Decimal(1)):
        raise ValueError("tax and fee rates must be between 0 and 1")
    if not Decimal(0) <= reserve_buffer < Decimal(1) or not Decimal(0) <= risk_discount < Decimal(1):
        raise ValueError("buffer and risk discount must be between 0 and 1")
    all_in_cap = (valuation.low * (Decimal(1) - reserve_buffer) * (Decimal(1) - risk_discount)).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
    # Model buyer fee on hammer; sales tax on hammer + shipping + buyer fee.
    max_hammer = ((all_in_cap / (Decimal(1) + tax_rate) - listing.shipping) /
                  (Decimal(1) + buyer_fee_rate)).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
    max_hammer = max(Decimal(0), max_hammer)
    estimated_edge = valuation.fair_value - (listing.current_price + listing.shipping)
    bid_headroom = max_hammer - listing.current_price
    flags: list[str] = []
    if valuation.confidence == "low": flags.append("thin_comps")
    if listing.seller_feedback is not None and listing.seller_feedback < 25: flags.append("limited_seller_history")
    if listing.seller_score is not None and listing.seller_score < Decimal("98.0"): flags.append("seller_score_below_98")
    if listing.condition.casefold() in {"unknown", "not specified"}: flags.append("condition_unverified")
    if bid_headroom < 0: flags.append("already_above_ceiling")
    eligible = bid_headroom >= 0 and valuation.confidence != "low" and "condition_unverified" not in flags
    return Opportunity(listing, valuation, max_hammer, all_in_cap, estimated_edge, bid_headroom, eligible, tuple(flags))


def screen_listings(listings: list[Listing], comps: list[SaleComp], *, tax_rate: Decimal = Decimal("0"),
                    buyer_fee_rate: Decimal = Decimal("0"), reserve_buffer: Decimal = Decimal("0.08"),
                    max_hours: int = 168, now: datetime | None = None) -> list[Opportunity]:
    now = now or datetime.now(timezone.utc)
    deadline = now.timestamp() + max_hours * 3600
    results = []
    for listing in listings:
        if listing.end_time.timestamp() <= now.timestamp() or listing.end_time.timestamp() > deadline:
            continue
        value = estimate_value(listing, comps, now=now)
        if value is None:
            continue
        results.append(analyze(listing, value, tax_rate=tax_rate, buyer_fee_rate=buyer_fee_rate,
                               reserve_buffer=reserve_buffer))
    return sorted(results, key=lambda x: (x.eligible, x.bid_headroom, x.estimated_edge), reverse=True)

