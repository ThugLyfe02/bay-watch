from __future__ import annotations

import json
import hashlib
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .models import Listing
from .money import validate_precision


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS listings (
 item_id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL, currency TEXT NOT NULL,
 current_price TEXT NOT NULL, shipping TEXT NOT NULL, end_time TEXT NOT NULL, condition TEXT NOT NULL,
 listing_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bid_intents (
 id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL REFERENCES listings(item_id),
 max_amount TEXT NOT NULL, currency TEXT NOT NULL, end_time TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('armed','submitting','submitted','cancelled','failed','expired','unknown')),
 submit_before TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 CHECK(length(item_id) > 0)
);
CREATE TABLE IF NOT EXISTS audit_log (
 id INTEGER PRIMARY KEY AUTOINCREMENT, occurred_at TEXT NOT NULL, action TEXT NOT NULL,
 item_id TEXT, details_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS budget_limits (
 currency TEXT PRIMARY KEY, amount TEXT NOT NULL CHECK(length(amount) > 0),
 updated_at TEXT NOT NULL
);
"""

LEARNING_SCHEMA = """
CREATE TABLE prediction_snapshots (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 item_id TEXT NOT NULL REFERENCES listings(item_id),
 created_at TEXT NOT NULL,
 currency TEXT NOT NULL,
 fair_value TEXT NOT NULL,
 value_low TEXT NOT NULL,
 value_high TEXT NOT NULL,
 confidence TEXT NOT NULL,
 comp_count INTEGER NOT NULL CHECK(comp_count >= 0),
 decision TEXT NOT NULL,
 policy_version TEXT NOT NULL,
 snapshot_hash TEXT NOT NULL UNIQUE,
 snapshot_json TEXT NOT NULL
);
CREATE INDEX prediction_snapshots_item_created ON prediction_snapshots(item_id, created_at);
CREATE TABLE learning_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 item_id TEXT NOT NULL REFERENCES listings(item_id),
 prediction_id INTEGER REFERENCES prediction_snapshots(id),
 event_type TEXT NOT NULL CHECK(event_type IN ('auction','purchase','resale','market_reference')),
 state TEXT NOT NULL,
 currency TEXT,
 amount TEXT,
 fees TEXT NOT NULL DEFAULT '0',
 shipping TEXT NOT NULL DEFAULT '0',
 refunds TEXT NOT NULL DEFAULT '0',
 source TEXT NOT NULL,
 evidence_ref TEXT NOT NULL,
 observed_at TEXT NOT NULL,
 idempotency_key TEXT NOT NULL UNIQUE,
 supersedes_event_id INTEGER REFERENCES learning_events(id),
 details_json TEXT NOT NULL
);
CREATE INDEX learning_events_item_type_observed ON learning_events(item_id, event_type, observed_at);
CREATE INDEX learning_events_prediction_type ON learning_events(prediction_id, event_type);
CREATE TRIGGER prediction_snapshots_no_update BEFORE UPDATE ON prediction_snapshots
 BEGIN SELECT RAISE(ABORT, 'prediction snapshots are immutable'); END;
CREATE TRIGGER prediction_snapshots_no_delete BEFORE DELETE ON prediction_snapshots
 BEGIN SELECT RAISE(ABORT, 'prediction snapshots are immutable'); END;
CREATE TRIGGER learning_events_no_update BEFORE UPDATE ON learning_events
 BEGIN SELECT RAISE(ABORT, 'learning events are append-only'); END;
CREATE TRIGGER learning_events_no_delete BEFORE DELETE ON learning_events
 BEGIN SELECT RAISE(ABORT, 'learning events are append-only'); END;
"""

DISCOVERY_SCHEMA = """
CREATE TABLE discovery_runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 query TEXT NOT NULL,
 source TEXT NOT NULL,
 searched_at TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('ok','error')),
 result_count INTEGER NOT NULL CHECK(result_count >= 0),
 unique_count INTEGER NOT NULL CHECK(unique_count >= 0),
 duplicate_count INTEGER NOT NULL CHECK(duplicate_count >= 0),
 duration_ms INTEGER NOT NULL CHECK(duration_ms >= 0),
 error_class TEXT
);
CREATE INDEX discovery_runs_query_time ON discovery_runs(query,searched_at);
CREATE TABLE discovery_hits (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 run_id INTEGER NOT NULL REFERENCES discovery_runs(id),
 item_id TEXT NOT NULL REFERENCES listings(item_id),
 result_position INTEGER NOT NULL CHECK(result_position >= 0),
 UNIQUE(run_id,item_id)
);
CREATE INDEX discovery_hits_item ON discovery_hits(item_id,run_id);
"""

MISSED_LISTING_SCHEMA = """
CREATE TABLE missed_listing_feedback (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 item_id TEXT NOT NULL CHECK(length(trim(item_id)) > 0),
 query TEXT NOT NULL CHECK(length(trim(query)) > 0),
 profile TEXT,
 source TEXT NOT NULL CHECK(length(trim(source)) > 0),
 evidence_ref TEXT NOT NULL CHECK(length(trim(evidence_ref)) > 0),
 observed_at TEXT NOT NULL,
 reason TEXT,
 idempotency_key TEXT NOT NULL UNIQUE
);
CREATE INDEX missed_listing_feedback_query_time ON missed_listing_feedback(query,observed_at);
CREATE TRIGGER missed_listing_feedback_no_update BEFORE UPDATE ON missed_listing_feedback
 BEGIN SELECT RAISE(ABORT, 'missed listing feedback is append-only'); END;
CREATE TRIGGER missed_listing_feedback_no_delete BEFORE DELETE ON missed_listing_feedback
 BEGIN SELECT RAISE(ABORT, 'missed listing feedback is append-only'); END;
"""

PROFILE_SCHEMA = """
CREATE TABLE category_profiles (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 name TEXT NOT NULL,
 version INTEGER NOT NULL CHECK(version > 0),
 category TEXT NOT NULL,
 queries_json TEXT NOT NULL,
 identity_fields_json TEXT NOT NULL,
 created_at TEXT NOT NULL,
 profile_hash TEXT NOT NULL UNIQUE,
 UNIQUE(name,version)
);
CREATE INDEX category_profiles_name_version ON category_profiles(name,version DESC);
CREATE TRIGGER category_profiles_no_update BEFORE UPDATE ON category_profiles
 BEGIN SELECT RAISE(ABORT, 'category profiles are versioned and immutable'); END;
CREATE TRIGGER category_profiles_no_delete BEFORE DELETE ON category_profiles
 BEGIN SELECT RAISE(ABORT, 'category profiles are versioned and immutable'); END;
ALTER TABLE discovery_runs ADD COLUMN profile_name TEXT;
ALTER TABLE discovery_runs ADD COLUMN profile_version INTEGER;
CREATE INDEX discovery_runs_profile ON discovery_runs(profile_name,profile_version,searched_at);
"""

PROFILE_FEEDBACK_SCHEMA = """
ALTER TABLE missed_listing_feedback ADD COLUMN profile_version INTEGER;
CREATE INDEX missed_listing_feedback_profile ON missed_listing_feedback(profile,profile_version,observed_at);
"""

MIGRATIONS = ((1, SCHEMA), (2, LEARNING_SCHEMA),
              (3, "ALTER TABLE bid_intents ADD COLUMN prediction_id INTEGER REFERENCES prediction_snapshots(id);"),
              (4, "ALTER TABLE bid_intents ADD COLUMN settled_at TEXT;\n"
                  "ALTER TABLE bid_intents ADD COLUMN resolution_event_id INTEGER REFERENCES learning_events(id);"),
              (5, DISCOVERY_SCHEMA),
              (6, "ALTER TABLE learning_events ADD COLUMN other_costs TEXT NOT NULL DEFAULT '0';"),
              (7, MISSED_LISTING_SCHEMA), (8, PROFILE_SCHEMA), (9, PROFILE_FEEDBACK_SCHEMA))
OUTCOME_STATES = {
    "auction": {"won", "lost", "no_sale", "cancelled", "unavailable", "unknown"},
    "purchase": {"recorded"},
    "resale": {"recorded"},
    "market_reference": {"verified"},
}

_SENSITIVE_AUDIT_KEYS = {"token", "accesstoken", "refreshtoken", "clientsecret", "secret",
                         "authorization", "password", "cookie", "apikey", "privatekey"}


def _redact_audit(value):
    if isinstance(value, dict):
        def is_sensitive(key):
            normalized = "".join(char for char in key.casefold() if char.isalnum())
            return normalized in _SENSITIVE_AUDIT_KEYS or normalized.endswith("token")
        return {key: ("[REDACTED]" if is_sensitive(key) else _redact_audit(item))
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_audit(item) for item in value]
    return value


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self):
        current = int(self.db.execute("PRAGMA user_version").fetchone()[0])
        if current > MIGRATIONS[-1][0]:
            raise RuntimeError(f"database schema version {current} is newer than this application supports")
        for version, script in MIGRATIONS:
            if version <= current:
                continue
            try:
                self.db.executescript(f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version={version};\nCOMMIT;")
            except Exception:
                self.db.rollback()
                raise

    def schema_version(self) -> int:
        return int(self.db.execute("PRAGMA user_version").fetchone()[0])

    def save_category_profile(self, name: str, category: str, queries: list[str],
                              identity_fields: list[str] | None = None) -> dict:
        """Append a reproducible profile version; existing versions remain immutable."""
        name, category = name.strip(), category.strip()
        queries = list(dict.fromkeys(q.strip() for q in queries if q and q.strip()))
        allowed = {"card_number", "grading_company", "grade", "language", "edition", "variant"}
        fields = sorted(set(identity_fields or []))
        if not name or not category or not queries:
            raise ValueError("profile name, category, and at least one query are required")
        if len(name) > 80 or len(category) > 120 or len(queries) > 32 or any(len(q) > 240 for q in queries):
            raise ValueError("profile field exceeds its supported size")
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError("unsupported identity fields: " + ", ".join(sorted(unknown)))
        with self.db:
            prior = self.db.execute("SELECT MAX(version) AS version FROM category_profiles WHERE name=? COLLATE NOCASE", (name,)).fetchone()
            version = int(prior["version"] or 0) + 1
            created = datetime.now(timezone.utc).isoformat()
            payload = {"name": name, "version": version, "category": category,
                       "queries": queries, "identity_fields": fields, "created_at": created}
            digest = hashlib.sha256(self._canonical(payload).encode()).hexdigest()
            self.db.execute("""INSERT INTO category_profiles
              (name,version,category,queries_json,identity_fields_json,created_at,profile_hash)
              VALUES(?,?,?,?,?,?,?)""", (name,version,category,self._canonical(queries),
              self._canonical(fields),created,digest))
        self.audit("category_profile_saved", None, {"name": name, "version": version, "query_count": len(queries)})
        return self.get_category_profile(name, version)

    def get_category_profile(self, name: str, version: int | None = None) -> dict | None:
        if version is None:
            row = self.db.execute("SELECT * FROM category_profiles WHERE name=? COLLATE NOCASE ORDER BY version DESC LIMIT 1", (name,)).fetchone()
        else:
            row = self.db.execute("SELECT * FROM category_profiles WHERE name=? COLLATE NOCASE AND version=?", (name,version)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["queries"] = json.loads(result.pop("queries_json"))
        result["identity_fields"] = json.loads(result.pop("identity_fields_json"))
        return result

    def list_category_profiles(self) -> list[dict]:
        rows = self.db.execute("""SELECT p.* FROM category_profiles p JOIN
          (SELECT name,MAX(version) version FROM category_profiles GROUP BY name COLLATE NOCASE) latest
          ON latest.name=p.name COLLATE NOCASE AND latest.version=p.version ORDER BY p.name COLLATE NOCASE""").fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["queries"] = json.loads(item.pop("queries_json"))
            item["identity_fields"] = json.loads(item.pop("identity_fields_json"))
            out.append(item)
        return out

    def list_category_profile_versions(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM category_profiles ORDER BY name COLLATE NOCASE,version").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["queries"] = json.loads(item.pop("queries_json"))
            item["identity_fields"] = json.loads(item.pop("identity_fields_json"))
            result.append(item)
        return result

    def category_profile_report(self) -> list[dict]:
        """Report only captured profile runs and user-evidenced misses; never infer recall."""
        reports = []
        for profile in self.list_category_profile_versions():
            name, version = profile["name"], profile["version"]
            runs = self.db.execute("SELECT * FROM discovery_runs WHERE profile_name=? AND profile_version=? ORDER BY searched_at DESC,id DESC", (name,version)).fetchall()
            hit_count = self.db.execute("""SELECT COUNT(DISTINCT h.item_id) FROM discovery_hits h
              JOIN discovery_runs r ON r.id=h.run_id WHERE r.profile_name=? AND r.profile_version=?""", (name,version)).fetchone()[0]
            misses = self.db.execute("SELECT COUNT(*) FROM missed_listing_feedback WHERE profile=? COLLATE NOCASE AND profile_version=?", (name,version)).fetchone()[0]
            unversioned_misses = self.db.execute("SELECT COUNT(*) FROM missed_listing_feedback WHERE profile=? COLLATE NOCASE AND profile_version IS NULL", (name,)).fetchone()[0]
            reports.append({"name": name, "version": version, "category": profile["category"],
                "query_count": len(profile["queries"]), "run_count": len(runs),
                "successful_runs": sum(r["status"] == "ok" for r in runs),
                "error_runs": sum(r["status"] == "error" for r in runs),
                "distinct_returned_listings": hit_count, "user_evidenced_missed_examples": misses,
                "unversioned_missed_examples": unversioned_misses,
                "last_run": runs[0]["searched_at"] if runs else None,
                "recall": "unknown; recorded API results and user examples are not a marketplace denominator"})
        return reports

    def record_discovery_run(self, query: str, results: list[Listing], *, status: str,
                             duration_ms: int, error_class: str | None = None,
                             source: str = "ebay_browse_api", profile_name: str | None = None,
                             profile_version: int | None = None) -> int:
        if status not in {"ok", "error"} or not query.strip() or duration_ms < 0:
            raise ValueError("invalid discovery run")
        if (profile_name is None) != (profile_version is None):
            raise ValueError("profile name and version must be recorded together")
        if profile_name is not None:
            profile = self.get_category_profile(profile_name, profile_version)
            if not profile or query not in profile["queries"]:
                raise ValueError("discovery query must belong to the saved profile version")
        item_ids = [item.item_id for item in results]
        unique_ids = list(dict.fromkeys(item_ids))
        when = datetime.now(timezone.utc).isoformat()
        with self.db:
            cur = self.db.execute("""INSERT INTO discovery_runs(query,source,searched_at,status,result_count,unique_count,duplicate_count,duration_ms,error_class,profile_name,profile_version)
              VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (query,source,when,status,len(item_ids),len(unique_ids),
              len(item_ids)-len(unique_ids),duration_ms,error_class,profile_name,profile_version))
            run_id = int(cur.lastrowid)
            for position, item in enumerate(results):
                self.save_listing(item)
                self.db.execute("INSERT OR IGNORE INTO discovery_hits(run_id,item_id,result_position) VALUES(?,?,?)",
                                (run_id,item.item_id,position))
        return run_id

    def discovery_report(self):
        runs = self.db.execute("SELECT * FROM discovery_runs ORDER BY searched_at DESC,id DESC").fetchall()
        grouped = self.db.execute("""SELECT query,COUNT(*) AS run_count,SUM(result_count) AS result_count,
          SUM(unique_count) AS unique_count,SUM(duplicate_count) AS duplicate_count,
          SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS error_count,
          MAX(searched_at) AS last_searched FROM discovery_runs GROUP BY query ORDER BY query""").fetchall()
        return {"source": "official Browse API query runs", "run_count": len(runs),
                "distinct_listings": self.db.execute("SELECT COUNT(DISTINCT item_id) FROM discovery_hits").fetchone()[0],
                "queries": [dict(r) for r in grouped], "recent_runs": [dict(r) for r in runs[:50]],
                "category_profiles": self.category_profile_report(),
                "recall_note": "Coverage describes recorded query activity and results; total marketplace recall is unknown."}

    def record_missed_listing(self, item_id: str, *, query: str, source: str, evidence_ref: str,
                              profile: str | None = None, observed_at: str | None = None,
                              reason: str | None = None, idempotency_key: str | None = None,
                              profile_version: int | None = None) -> int:
        """Append user-evidenced missed-search feedback; this is not a recall denominator."""
        item_id, query, source, evidence_ref = (value.strip() for value in (item_id, query, source, evidence_ref))
        if not all((item_id, query, source, evidence_ref)):
            raise ValueError("item ID, query, source, and evidence reference are required")
        if profile_version is not None and (not profile or not self.get_category_profile(profile, profile_version)):
            raise ValueError("profile version must identify a saved category profile")
        if profile is not None and not profile.strip():
            raise ValueError("profile name cannot be blank")
        timestamp = observed_at or datetime.now(timezone.utc).isoformat()
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("observation timestamp must include a timezone")
        timestamp = parsed.astimezone(timezone.utc).isoformat()
        stable = {"item_id": item_id, "query": query, "profile": profile, "profile_version": profile_version, "source": source,
                  "evidence_ref": evidence_ref, "observed_at": timestamp, "reason": reason}
        key = idempotency_key or hashlib.sha256(self._canonical(stable).encode()).hexdigest()
        with self.db:
            self.db.execute("""INSERT OR IGNORE INTO missed_listing_feedback
              (item_id,query,profile,source,evidence_ref,observed_at,reason,idempotency_key,profile_version)
              VALUES(?,?,?,?,?,?,?,?,?)""", (item_id, query, profile, source, evidence_ref, timestamp, reason, key, profile_version))
            row = self.db.execute("SELECT id FROM missed_listing_feedback WHERE idempotency_key=?", (key,)).fetchone()
        return int(row["id"])

    def evidence_coverage_report(self):
        """Summarize captured evidence and gaps without implying complete marketplace recall."""
        from .calibration import calibration_report

        calibration = calibration_report(self)
        predictions = int(self.db.execute("SELECT COUNT(*) FROM prediction_snapshots").fetchone()[0])
        refs = self.db.execute("""SELECT COUNT(DISTINCT prediction_id) FROM learning_events e
          WHERE e.event_type='market_reference' AND e.state='verified' AND prediction_id IS NOT NULL
          AND NOT EXISTS(SELECT 1 FROM learning_events n WHERE n.supersedes_event_id=e.id)""").fetchone()[0]
        effective = self.db.execute("""SELECT event_type,state,source,COUNT(*) AS count FROM learning_events e
          WHERE NOT EXISTS(SELECT 1 FROM learning_events n WHERE n.supersedes_event_id=e.id)
          GROUP BY event_type,state,source ORDER BY event_type,state,source""").fetchall()
        unresolved = self.db.execute("""SELECT status,COUNT(*) AS count FROM bid_intents
          WHERE status IN ('submitting','submitted','unknown') AND settled_at IS NULL
          GROUP BY status ORDER BY status""").fetchall()
        discovery = self.discovery_report()
        missed_count = int(self.db.execute("SELECT COUNT(*) FROM missed_listing_feedback").fetchone()[0])
        resale_rows = self.db.execute("""SELECT r.item_id,r.currency,SUM(CAST(r.amount AS NUMERIC)) AS amount,
          SUM(CAST(r.fees AS NUMERIC)) AS fees,SUM(CAST(r.shipping AS NUMERIC)) AS shipping,
          SUM(CAST(r.refunds AS NUMERIC)) AS refunds,SUM(CAST(r.other_costs AS NUMERIC)) AS other_costs
          FROM learning_events r WHERE r.event_type='resale' AND r.state='recorded'
          AND NOT EXISTS(SELECT 1 FROM learning_events n WHERE n.supersedes_event_id=r.id)
          AND r.amount IS NOT NULL GROUP BY r.item_id,r.currency ORDER BY r.item_id,r.currency""").fetchall()
        realized_by_currency: dict[str, Decimal] = {}
        realized_count = 0
        for sale in resale_rows:
            purchases = self.db.execute("""SELECT p.amount FROM learning_events p WHERE p.item_id=?
              AND p.event_type='purchase' AND p.state='recorded' AND p.currency=?
              AND NOT EXISTS(SELECT 1 FROM learning_events n WHERE n.supersedes_event_id=p.id)""",
              (sale["item_id"], sale["currency"])).fetchall()
            if not purchases:
                continue
            basis = sum((Decimal(row["amount"]) for row in purchases if row["amount"] is not None), Decimal(0))
            net = (Decimal(sale["amount"]) - Decimal(sale["fees"]) - Decimal(sale["shipping"])
                   - Decimal(sale["refunds"]) - Decimal(sale["other_costs"]) - basis)
            realized_by_currency[sale["currency"]] = realized_by_currency.get(sale["currency"], Decimal(0)) + net
            realized_count += 1
        missed_by_profile = [dict(row) for row in self.db.execute("""SELECT COALESCE(profile,'(unprofiled)') AS profile,
          COUNT(*) AS evidence_count,MAX(observed_at) AS latest_observed_at
          FROM missed_listing_feedback GROUP BY COALESCE(profile,'(unprofiled)') ORDER BY profile""").fetchall()]
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "predictions": {"snapshot_count": predictions, "with_verified_later_market_reference": int(refs),
                            "without_verified_later_market_reference": max(0, predictions - int(refs))},
            "calibration": {"target": calibration["target"], "sample_count": calibration["sample_count"],
                            "mean_absolute_error": calibration["mean_absolute_error"],
                            "mean_signed_error": calibration["mean_signed_error"],
                            "uncertainty_range_coverage": calibration["uncertainty_range_coverage"],
                            "warning": calibration["warning"]},
            "auction_and_resale_evidence": {"effective_event_counts": [dict(row) for row in effective],
                                             "unresolved_intents_by_status": [dict(row) for row in unresolved]},
            "realized_resale": {"completed_resale_count_with_purchase_basis": realized_count,
                                "net_profit_by_currency": {key: str(value) for key, value in sorted(realized_by_currency.items())},
                                "currencies_are_not_converted": True},
            "discovery": {"run_count": discovery["run_count"], "distinct_returned_listings": discovery["distinct_listings"],
                          "query_count": len(discovery["queries"]),
                          "error_runs": sum(query["error_count"] for query in discovery["queries"]),
                          "recall_note": discovery["recall_note"]},
            "missed_listing_feedback": {"user_evidenced_record_count": missed_count,
                                        "by_profile": missed_by_profile,
                                        "denominator_available": False,
                                        "note": "User-provided examples can guide query improvements; total marketplace recall is unknown."},
            "calibration_status": "measurable" if int(refs) else "not_measurable_no_linked_verified_reference",
            "realized_margin_status": "measurable_only_for_completed_resales_with_purchase_basis",
            "false_positive_rate_status": "not_measurable_no_reviewed_candidate_labeling_protocol",
            "missed_close_rate_status": "not_measurable_no_verified_execution_miss_labels",
            "gaps": ["Marketplace recall is unknown because recorded queries and user examples do not define a complete denominator."]
                     + (["No linked verified completed-market reference yet; valuation calibration error is not measurable."] if not refs else [])
                     + (["No user-evidenced missed listings yet; discovery blind spots are not measurable."] if not missed_count else [])
                     + ["False-positive rate is not measurable until candidates have a reviewed, consistent outcome label.",
                        "Missed-close rate is not measurable until intended close, worker timing, and verified execution-miss labels are captured."]
                     + (["No completed resale with a recorded purchase basis yet; realized net margin is not measurable."]
                        if not self.db.execute("""SELECT 1 FROM learning_events r WHERE r.event_type='resale' AND r.state='recorded'
                           AND NOT EXISTS(SELECT 1 FROM learning_events n WHERE n.supersedes_event_id=r.id)
                           AND EXISTS(SELECT 1 FROM learning_events p WHERE p.item_id=r.item_id AND p.event_type='purchase' AND p.state='recorded'
                             AND p.currency=r.currency AND NOT EXISTS(SELECT 1 FROM learning_events n2 WHERE n2.supersedes_event_id=p.id)) LIMIT 1""").fetchone() else [])
        }

    def close(self): self.db.close()

    def _reserved_exposure(self, currency: str, now_iso: str) -> Decimal:
        rows = self.db.execute("""SELECT max_amount FROM bid_intents WHERE currency=? AND
                               ((status='armed' AND end_time>?) OR
                                (status IN ('submitting','submitted','unknown') AND settled_at IS NULL))""",
                               (currency, now_iso)).fetchall()
        return sum((Decimal(r["max_amount"]) for r in rows), Decimal("0"))

    def audit(self, action: str, item_id: str | None, details: dict):
        with self.db:
            self.db.execute("INSERT INTO audit_log(occurred_at,action,item_id,details_json) VALUES(?,?,?,?)",
                            (datetime.now(timezone.utc).isoformat(), action, item_id,
                             json.dumps(_redact_audit(details), sort_keys=True)))

    def save_listing(self, item: Listing):
        payload = {"seller_feedback": item.seller_feedback, "seller_score": str(item.seller_score) if item.seller_score is not None else None, "location": item.location}
        with self.db:
            self.db.execute("""INSERT INTO listings VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET
              title=excluded.title,url=excluded.url,currency=excluded.currency,current_price=excluded.current_price,
              shipping=excluded.shipping,end_time=excluded.end_time,condition=excluded.condition,
              listing_json=excluded.listing_json,updated_at=excluded.updated_at""",
                (item.item_id, item.title, item.url, item.currency, str(item.current_price), str(item.shipping),
                 item.end_time.isoformat(), item.condition, json.dumps(payload), datetime.now(timezone.utc).isoformat()))

    def get_listing(self, item_id: str) -> Listing | None:
        r = self.db.execute("SELECT * FROM listings WHERE item_id=?", (item_id,)).fetchone()
        if not r: return None
        p = json.loads(r["listing_json"])
        from decimal import Decimal
        return Listing(r["item_id"], r["title"], r["url"], r["currency"], Decimal(r["current_price"]),
                       datetime.fromisoformat(r["end_time"]), Decimal(r["shipping"]), r["condition"],
                       p.get("seller_feedback"), Decimal(p["seller_score"]) if p.get("seller_score") else None, p.get("location"))

    def arm_intent(self, item_id: str, max_amount: Decimal, currency: str, end_time: datetime, lead_seconds: int,
                   prediction_id: int | None = None):
        validate_precision(max_amount, currency)
        now = datetime.now(timezone.utc)
        submit_before = end_time.astimezone(timezone.utc).timestamp() - lead_seconds
        submit_before_dt = datetime.fromtimestamp(submit_before, timezone.utc)
        # Serialize budget read + reservation so concurrent CLI invocations cannot over-allocate.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            limit_row = self.db.execute("SELECT amount FROM budget_limits WHERE currency=?", (currency,)).fetchone()
            if not limit_row:
                raise ValueError(f"set a {currency} portfolio exposure cap with `auction-scout budget-set --currency {currency} --amount ...` before arming bids")
            reserved = self._reserved_exposure(currency, now.isoformat())
            cap = Decimal(limit_row["amount"])
            if max_amount <= 0:
                raise ValueError("maximum bid must be positive")
            if reserved + max_amount > cap:
                raise ValueError(f"portfolio exposure cap exceeded: {reserved:.2f} reserved + {max_amount:.2f} requested > {cap:.2f} {currency}")
            # Only one active intent per item; callers must explicitly cancel before replacing.
            if self.db.execute("""SELECT 1 FROM bid_intents WHERE item_id=? AND
                 ((status='armed' AND end_time>?) OR
                  (status IN ('submitting','submitted','unknown') AND settled_at IS NULL))""",
                               (item_id, now.isoformat())).fetchone():
                raise ValueError("an active or unresolved intent already exists for this item; verify its outcome before scheduling another")
            if prediction_id is None:
                pred = self.db.execute("SELECT id FROM prediction_snapshots WHERE item_id=? ORDER BY created_at DESC,id DESC LIMIT 1", (item_id,)).fetchone()
                prediction_id = pred["id"] if pred else None
            if prediction_id is not None and not self.db.execute("SELECT 1 FROM prediction_snapshots WHERE id=? AND item_id=?", (prediction_id, item_id)).fetchone():
                raise ValueError("prediction snapshot does not belong to this item")
            self.db.execute("INSERT INTO bid_intents(item_id,max_amount,currency,end_time,status,submit_before,created_at,updated_at,prediction_id) VALUES(?,?,?,?,?,?,?,?,?)",
                            (item_id, str(max_amount), currency, end_time.isoformat(), "armed", submit_before_dt.isoformat(), now.isoformat(), now.isoformat(), prediction_id))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self.audit("bid_intent_armed", item_id, {"max_amount": str(max_amount), "currency": currency, "submit_before": submit_before_dt.isoformat()})

    @staticmethod
    def _canonical(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)

    def record_prediction(self, item_id: str, snapshot: dict, *, decision: str, policy_version: str) -> int:
        """Store a reproducible, immutable copy of the inputs and decision made at this time."""
        if decision not in {"pass", "watch", "armed", "bid_submitted"}:
            raise ValueError("decision must be pass, watch, armed, or bid_submitted")
        if not policy_version.strip():
            raise ValueError("policy version is required")
        if not self.db.execute("SELECT 1 FROM listings WHERE item_id=?", (item_id,)).fetchone():
            raise ValueError("prediction requires a stored listing")
        created = snapshot.get("prediction_at") or datetime.now(timezone.utc).isoformat()
        created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if created_dt.tzinfo is None:
            raise ValueError("prediction timestamp must include a timezone")
        currency = str(snapshot["currency"]).upper()
        fv, low, high = (Decimal(str(snapshot[k])) for k in ("fair_value", "value_low", "value_high"))
        if low > fv or fv > high:
            raise ValueError("prediction range must contain fair value")
        if int(snapshot.get("comp_count", 0)) < 0:
            raise ValueError("comp count cannot be negative")
        immutable = {**snapshot, "prediction_at": created_dt.astimezone(timezone.utc).isoformat(),
                     "decision": decision, "policy_version": policy_version}
        digest = hashlib.sha256(self._canonical(immutable).encode("utf-8")).hexdigest()
        with self.db:
            self.db.execute("""INSERT OR IGNORE INTO prediction_snapshots
                (item_id,created_at,currency,fair_value,value_low,value_high,confidence,comp_count,decision,policy_version,snapshot_hash,snapshot_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item_id, immutable["prediction_at"], currency, str(fv), str(low), str(high),
                 str(snapshot.get("confidence", "unknown")), int(snapshot.get("comp_count", 0)), decision,
                 policy_version, digest, self._canonical(immutable)))
            row = self.db.execute("SELECT id FROM prediction_snapshots WHERE snapshot_hash=?", (digest,)).fetchone()
        return int(row["id"])

    def latest_prediction_id(self, item_id: str) -> int | None:
        row = self.db.execute("SELECT id FROM prediction_snapshots WHERE item_id=? ORDER BY created_at DESC,id DESC LIMIT 1", (item_id,)).fetchone()
        return int(row["id"]) if row else None

    def prediction(self, prediction_id: int):
        return self.db.execute("SELECT * FROM prediction_snapshots WHERE id=?", (prediction_id,)).fetchone()

    def record_learning_event(self, item_id: str, *, event_type: str, state: str, source: str,
                              evidence_ref: str, observed_at: str | None = None, prediction_id: int | None = None,
                              currency: str | None = None, amount: Decimal | None = None,
                              fees: Decimal = Decimal("0"), shipping: Decimal = Decimal("0"),
                              refunds: Decimal = Decimal("0"), other_costs: Decimal = Decimal("0"),
                              confidence: str = "user_confirmed",
                              details: dict | None = None, idempotency_key: str | None = None,
                              supersedes_event_id: int | None = None) -> int:
        if event_type not in OUTCOME_STATES or state not in OUTCOME_STATES[event_type]:
            raise ValueError("unsupported learning event type or state")
        if not source.strip() or not evidence_ref.strip():
            raise ValueError("source and evidence reference are required")
        if not self.db.execute("SELECT 1 FROM listings WHERE item_id=?", (item_id,)).fetchone():
            raise ValueError("learning event requires a stored listing")
        if event_type in {"purchase", "resale", "market_reference"} and (amount is None or currency is None):
            raise ValueError("purchase, resale, and market reference events require amount and currency")
        if event_type == "auction" and state in {"won", "lost", "no_sale"} and (amount is None or currency is None):
            raise ValueError("verified auction result requires observed price and currency")
        for value in (amount, fees, shipping, refunds, other_costs):
            if value is not None and value < 0:
                raise ValueError("amounts and costs cannot be negative")
        currency = currency.upper() if currency else None
        if currency and len(currency) != 3:
            raise ValueError("currency must be a three-letter code")
        timestamp = observed_at or datetime.now(timezone.utc).isoformat()
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("observation timestamp must include a timezone")
        timestamp = parsed.astimezone(timezone.utc).isoformat()
        if prediction_id is not None:
            pred = self.db.execute("SELECT item_id,created_at,currency FROM prediction_snapshots WHERE id=?", (prediction_id,)).fetchone()
            if not pred or pred["item_id"] != item_id:
                raise ValueError("prediction snapshot does not belong to this item")
            if event_type == "market_reference":
                if parsed.astimezone(timezone.utc) <= datetime.fromisoformat(pred["created_at"].replace("Z", "+00:00")):
                    raise ValueError("calibration evidence must be observed after the prediction")
                if pred["currency"] != currency:
                    raise ValueError("calibration reference currency must match the prediction")
        if supersedes_event_id is not None:
            prior = self.db.execute("SELECT item_id,event_type FROM learning_events WHERE id=?", (supersedes_event_id,)).fetchone()
            if not prior or prior["item_id"] != item_id or prior["event_type"] != event_type:
                raise ValueError("correction must supersede an event for the same item and type")
            if self.db.execute("SELECT 1 FROM learning_events WHERE supersedes_event_id=?", (supersedes_event_id,)).fetchone():
                raise ValueError("an event can have only one direct correction; supersede the latest correction instead")
        details = {**(details or {}), "confidence": confidence}
        stable = {"item": item_id, "type": event_type, "state": state, "source": source,
                  "evidence_ref": evidence_ref, "prediction_id": prediction_id, "currency": currency,
                  "amount": str(amount) if amount is not None else None, "fees": str(fees),
                  "shipping": str(shipping), "refunds": str(refunds), "other_costs": str(other_costs), "details": details,
                  "supersedes": supersedes_event_id}
        key = idempotency_key or hashlib.sha256(self._canonical(stable).encode()).hexdigest()
        with self.db:
            self.db.execute("""INSERT OR IGNORE INTO learning_events
              (item_id,prediction_id,event_type,state,currency,amount,fees,shipping,refunds,source,evidence_ref,observed_at,idempotency_key,supersedes_event_id,details_json,other_costs)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (item_id,prediction_id,event_type,state,currency,str(amount) if amount is not None else None,
               str(fees),str(shipping),str(refunds),source,evidence_ref,timestamp,key,supersedes_event_id,self._canonical(details),str(other_costs)))
            row = self.db.execute("SELECT id FROM learning_events WHERE idempotency_key=?", (key,)).fetchone()
        return int(row["id"])

    def event_for_evidence(self, item_id: str, event_type: str, source: str, evidence_ref: str):
        return self.db.execute("""SELECT * FROM learning_events WHERE item_id=? AND event_type=? AND source=? AND evidence_ref=?
          ORDER BY id DESC LIMIT 1""", (item_id, event_type, source, evidence_ref)).fetchone()

    def reconcilable_intents(self, now: datetime):
        return self.db.execute("""SELECT * FROM bid_intents WHERE status IN ('submitted','unknown','submitting') AND settled_at IS NULL AND end_time<=?
          ORDER BY end_time,id""", (now.astimezone(timezone.utc).isoformat(),)).fetchall()

    def settle_intent(self, intent_id: int, event_id: int):
        event = self.db.execute("SELECT item_id,event_type,state FROM learning_events WHERE id=?", (event_id,)).fetchone()
        if not event or event["event_type"] != "auction" or event["state"] not in {"won", "lost", "no_sale", "cancelled", "unavailable"}:
            raise ValueError("only a definitive auction evidence event can settle an intent")
        now = datetime.now(timezone.utc).isoformat()
        with self.db:
            cur = self.db.execute("""UPDATE bid_intents SET settled_at=?,resolution_event_id=?
              WHERE id=? AND item_id=? AND settled_at IS NULL AND status IN ('submitting','submitted','unknown')""",
              (now, event_id, intent_id, event["item_id"]))
            if cur.rowcount != 1:
                raise ValueError("intent was already settled or does not match the evidence")
        self.audit("bid_intent_outcome_verified", event["item_id"], {"intent_id": intent_id, "event_id": event_id, "outcome": event["state"]})

    def outcome_events(self, item_id: str):
        return self.db.execute("""SELECT e.* FROM learning_events e WHERE e.item_id=?
          AND NOT EXISTS(SELECT 1 FROM learning_events n WHERE n.supersedes_event_id=e.id)
          ORDER BY e.observed_at,e.id""", (item_id,)).fetchall()

    def set_budget(self, currency: str, amount: Decimal):
        if amount <= 0: raise ValueError("portfolio cap must be positive")
        validate_precision(amount, currency)
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            reserved = self._reserved_exposure(currency, now)
            if reserved > amount:
                raise ValueError(f"cannot set cap below currently reserved exposure ({reserved:.2f} {currency})")
            self.db.execute("INSERT INTO budget_limits(currency,amount,updated_at) VALUES(?,?,?) ON CONFLICT(currency) DO UPDATE SET amount=excluded.amount,updated_at=excluded.updated_at",
                            (currency, str(amount), now))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self.audit("portfolio_cap_set", None, {"currency": currency, "amount": str(amount)})

    def budget_status(self, currency: str):
        now = datetime.now(timezone.utc).isoformat()
        limit_row = self.db.execute("SELECT amount FROM budget_limits WHERE currency=?", (currency,)).fetchone()
        reserved = self._reserved_exposure(currency, now)
        cap = Decimal(limit_row["amount"]) if limit_row else None
        return {"currency": currency, "cap": cap, "reserved": reserved, "available": cap - reserved if cap is not None else None}

    def due_intents(self, now: datetime):
        return self.db.execute("SELECT * FROM bid_intents WHERE status='armed' AND submit_before<=? ORDER BY submit_before", (now.isoformat(),)).fetchall()

    def transition_intent(self, intent_id: int, expected: str, target: str, details: dict):
        now = datetime.now(timezone.utc).isoformat()
        with self.db:
            cur = self.db.execute("UPDATE bid_intents SET status=?,updated_at=? WHERE id=? AND status=?", (target, now, intent_id, expected))
            if cur.rowcount != 1: raise ValueError("intent state changed concurrently; refusing duplicate action")
            row = self.db.execute("SELECT item_id FROM bid_intents WHERE id=?", (intent_id,)).fetchone()
        self.audit("bid_intent_" + target, row["item_id"], {"intent_id": intent_id, **details})

    def cancel_intent(self, intent_id: int):
        self.transition_intent(intent_id, "armed", "cancelled", {})

    def armed_intents(self):
        return self.db.execute("SELECT * FROM bid_intents WHERE status='armed' ORDER BY submit_before").fetchall()

