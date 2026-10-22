"""Reproducible calibration against explicitly linked, later-observed sale references."""
from __future__ import annotations

from decimal import Decimal
import json
from statistics import median
from datetime import datetime, timezone

from .identity import compatible_titles
from .valuation import title_similarity


MIN_COHORT = 5


def calibration_report(store, *, since: str | None = None, until: str | None = None) -> dict:
    where, params = [], []
    if since:
        where.append("p.created_at>=?"); params.append(since)
    if until:
        where.append("p.created_at<=?"); params.append(until)
    filter_sql = ("WHERE " + " AND ".join(where)) if where else ""
    predictions = store.db.execute(f"SELECT p.* FROM prediction_snapshots p {filter_sql} ORDER BY p.created_at,p.id", params).fetchall()
    refs = store.db.execute("""SELECT e.* FROM learning_events e WHERE e.event_type='market_reference'
      AND e.state='verified' AND NOT EXISTS(SELECT 1 FROM learning_events n WHERE n.supersedes_event_id=e.id)
      ORDER BY e.observed_at,e.id""").fetchall()
    by_prediction = {}
    for event in refs:
        if event["prediction_id"] is not None:
            by_prediction.setdefault(event["prediction_id"], []).append(event)
    rows = []
    for pred in predictions:
        candidates = [e for e in by_prediction.get(pred["id"], [])
                      if e["observed_at"] > pred["created_at"] and e["currency"] == pred["currency"]]
        # The first later verified completed sale is the declared reference target.
        if not candidates:
            continue
        event = candidates[0]
        value, low, high = Decimal(pred["fair_value"]), Decimal(pred["value_low"]), Decimal(pred["value_high"])
        # The valuation estimator models hammer/item price plus buyer-paid shipping.
        actual = Decimal(event["amount"]) + Decimal(event["shipping"])
        error = value - actual
        snapshot = json.loads(pred["snapshot_json"])
        title = (snapshot.get("listing") or {}).get("title", "")
        baseline_comps = []
        for comp in snapshot.get("comps", []):
            try:
                comp_time = datetime.fromisoformat(comp["sold_at"].replace("Z", "+00:00"))
                comp_price = Decimal(comp["price"]) + Decimal(comp.get("shipping") or "0")
            except (KeyError, ValueError, TypeError):
                continue
            if (comp_time.tzinfo is not None and comp_time.astimezone(timezone.utc) <= datetime.fromisoformat(pred["created_at"].replace("Z", "+00:00"))
                    and comp_price > 0 and comp.get("currency", "").upper() == pred["currency"]
                    and compatible_titles(title, comp.get("title", "")) and title_similarity(title, comp.get("title", "")) >= 0.25):
                baseline_comps.append(comp_price)
        baseline = Decimal(str(median(baseline_comps))) if baseline_comps else None
        rows.append({"prediction_id": pred["id"], "created_at": pred["created_at"], "policy_version": pred["policy_version"],
                     "currency": pred["currency"], "prediction": value, "actual": actual, "signed_error": error,
                     "absolute_error": abs(error), "interval_covered": low <= actual <= high,
                     "confidence": pred["confidence"], "source": event["source"], "simple_baseline": baseline,
                     "simple_baseline_absolute_error": abs(baseline - actual) if baseline is not None else None,
                     "simple_baseline_signed_error": baseline - actual if baseline is not None else None})
    abs_errors = [r["absolute_error"] for r in rows]
    signed = [r["signed_error"] for r in rows]
    by_currency: dict[str, list[dict]] = {}
    by_confidence: dict[str, list[dict]] = {}
    for row in rows:
        by_currency.setdefault(row["currency"], []).append(row)
        by_confidence.setdefault(row["confidence"], []).append(row)

    def segment(groups):
        result = {}
        for key, cohort in sorted(groups.items()):
            result[key] = {"sample_count": len(cohort), "status": "reported" if len(cohort) >= MIN_COHORT else "too_small",
                           "mean_absolute_error": str(sum((r["absolute_error"] for r in cohort), Decimal(0)) / len(cohort)) if len(cohort) >= MIN_COHORT else None}
        return result

    source_counts = {}
    for row in rows:
        source_counts[row["source"]] = source_counts.get(row["source"], 0) + 1
    covered_start = min((r["created_at"] for r in rows), default=None)
    covered_end = max((r["created_at"] for r in rows), default=None)
    # Monthly cohorts keep deterioration visible instead of hiding it in an all-time metric.
    monthly: dict[str, list[dict]] = {}
    for row in rows:
        monthly.setdefault(row["created_at"][:7], []).append(row)
    periods = [{"period": month, "sample_count": len(cohort),
                "mean_absolute_error": str(sum((r["absolute_error"] for r in cohort), Decimal(0)) / len(cohort)),
                "mean_signed_error": str(sum((r["signed_error"] for r in cohort), Decimal(0)) / len(cohort))}
               for month, cohort in sorted(monthly.items())]
    baseline_rows = [r for r in rows if r["simple_baseline"] is not None]
    baseline_abs = [r["simple_baseline_absolute_error"] for r in baseline_rows]
    abs_sorted = sorted(abs_errors)
    distribution = {"minimum_absolute_error": str(abs_sorted[0]) if rows else None,
                    "p25_absolute_error": str(abs_sorted[int((len(abs_sorted) - 1) * 0.25)]) if rows else None,
                    "p75_absolute_error": str(abs_sorted[int((len(abs_sorted) - 1) * 0.75)]) if rows else None,
                    "maximum_absolute_error": str(abs_sorted[-1]) if rows else None}
    return {"target": "verified completed market_reference item price plus shipping; excludes auction hammer and resale outcomes",
            "sample_count": len(rows), "prediction_count": len(predictions), "missing_reference_count": len(predictions) - len(rows),
            "coverage_period": {"from": covered_start, "through": covered_end},
            "mean_absolute_error": str(sum(abs_errors, Decimal(0)) / len(rows)) if rows else None,
            "median_absolute_error": str(median(abs_errors)) if rows else None,
            "mean_signed_error": str(sum(signed, Decimal(0)) / len(rows)) if rows else None,
            "absolute_error_distribution": distribution,
            "uncertainty_range_coverage": sum(r["interval_covered"] for r in rows) / len(rows) if rows else None,
            "source_breakdown": source_counts, "by_currency": segment(by_currency),
            "by_confidence": segment(by_confidence), "monthly_drift": periods,
            "warning": "Insufficient evidence for conclusions" if len(rows) < MIN_COHORT else None,
            "simple_median_baseline": {"sample_count": len(baseline_rows),
                "mean_absolute_error": str(sum(baseline_abs, Decimal(0)) / len(baseline_rows)) if baseline_rows else None,
                "mean_signed_error": str(sum((r["simple_baseline_signed_error"] for r in baseline_rows), Decimal(0)) / len(baseline_rows)) if baseline_rows else None,
                "definition": "median of same-currency, identity-compatible, title-matched comps available by prediction time; no recency or condition weighting"}}
