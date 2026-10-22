"""Read-only, currency-separated opportunity planning from saved snapshots."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json


CONFIDENCE_ORDER = {"high": 2, "medium": 1, "low": 0, "unknown": -1}


def portfolio_plan(store, *, concurrency: int = 4, safe_window_seconds: int = 25,
                   now: datetime | None = None) -> dict:
    if concurrency < 1 or safe_window_seconds < 1:
        raise ValueError("concurrency and safe window must be positive")
    now = now or datetime.now(timezone.utc)
    rows = store.db.execute("""SELECT p.* FROM prediction_snapshots p
      JOIN (SELECT item_id,MAX(id) AS latest_id FROM prediction_snapshots GROUP BY item_id) latest
      ON p.id=latest.latest_id ORDER BY p.currency,p.item_id""").fetchall()
    by_currency = {}
    for row in rows:
        data = json.loads(row["snapshot_json"])
        listing = data.get("listing") or {}
        end = datetime.fromisoformat(listing["end_time"].replace("Z", "+00:00")) if listing.get("end_time") else None
        if not end or end.astimezone(timezone.utc) <= now.astimezone(timezone.utc):
            continue
        age_minutes = max(0, int((now.astimezone(timezone.utc) - datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")).astimezone(timezone.utc)).total_seconds() / 60))
        item = {"item_id": row["item_id"], "currency": row["currency"], "decision": row["decision"],
                "confidence": row["confidence"], "comp_count": row["comp_count"],
                "eligible": data.get("eligible"), "headroom": data.get("bid_headroom"),
                "risk_flags": data.get("risk_flags", []), "end_time": end.astimezone(timezone.utc).isoformat(),
                "seconds_to_close": int((end.astimezone(timezone.utc)-now.astimezone(timezone.utc)).total_seconds()),
                "evidence_age_minutes": age_minutes, "prediction_id": row["id"],
                "user_authored_maximum": data.get("user_authored_maximum")}
        by_currency.setdefault(row["currency"], []).append(item)

    reports = {}
    for currency, candidates in sorted(by_currency.items()):
        candidates.sort(key=lambda c: (bool(c["eligible"]),
            Decimal(c["headroom"]) if c["headroom"] is not None else Decimal("-Infinity"), CONFIDENCE_ORDER.get(c["confidence"], -1),
            c["comp_count"], -c["evidence_age_minutes"], -c["seconds_to_close"]), reverse=True)
        for index, candidate in enumerate(candidates, 1):
            candidate["rank"] = index
            candidate["ranking_components"] = ["eligible", "estimated headroom", "confidence",
                                               "comp count", "evidence freshness", "close time"]
            candidate["rank_explanation"] = ("Ranked by visible components; headroom and confidence lead, with evidence depth, freshness, and close timing as tie-breakers."
                if index == 1 else "Compared only with candidates in this currency using the same visible component order.")
        budget = store.budget_status(currency)
        active = store.db.execute("""SELECT id,status,max_amount,end_time,submit_before FROM bid_intents
          WHERE currency=? AND ((status='armed' AND end_time>?) OR
          (status IN ('submitting','submitted','unknown') AND settled_at IS NULL)) ORDER BY submit_before""",
          (currency, now.astimezone(timezone.utc).isoformat())).fetchall()
        due = [r for r in active if r["status"] == "armed" and r["submit_before"] <= now.astimezone(timezone.utc).isoformat()]
        near_close = [c for c in candidates if c["seconds_to_close"] <= safe_window_seconds]
        warnings = []
        if len(due) > concurrency:
            warnings.append(f"{len(due)} armed intents are due for a worker concurrency of {concurrency}; queued submissions may miss closes")
        if near_close:
            warnings.append(f"{len(near_close)} candidates close within the configured {safe_window_seconds}s safe window")
        reports[currency] = {"cap": str(budget["cap"]) if budget["cap"] is not None else None,
            "reserved": str(budget["reserved"]), "available": str(budget["available"]) if budget["available"] is not None else None,
            "active_intents": [{"intent_id": r["id"], "status": r["status"], "max_amount": r["max_amount"],
                                "end_time": r["end_time"], "submit_before": r["submit_before"]} for r in active],
            "due_queue_depth": len(due), "candidates": candidates, "warnings": warnings}
    return {"generated_at": now.astimezone(timezone.utc).isoformat(), "currencies_are_not_converted": True,
            "automatic_maximum_changes": False, "bids_submitted": False,
            "ranking_policy": "visible lexicographic components; no opaque composite score",
            "by_currency": reports}
