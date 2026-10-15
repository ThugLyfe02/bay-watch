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

MIGRATIONS = ((1, SCHEMA), (2, LEARNING_SCHEMA),
              (3, "ALTER TABLE bid_intents ADD COLUMN prediction_id INTEGER REFERENCES prediction_snapshots(id);"),
              (4, "ALTER TABLE bid_intents ADD COLUMN settled_at TEXT;\n"
                  "ALTER TABLE bid_intents ADD COLUMN resolution_event_id INTEGER REFERENCES learning_events(id);"))
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
                              refunds: Decimal = Decimal("0"), confidence: str = "user_confirmed",
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
        for value in (amount, fees, shipping, refunds):
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
                  "shipping": str(shipping), "refunds": str(refunds), "details": details,
                  "supersedes": supersedes_event_id}
        key = idempotency_key or hashlib.sha256(self._canonical(stable).encode()).hexdigest()
        with self.db:
            self.db.execute("""INSERT OR IGNORE INTO learning_events
              (item_id,prediction_id,event_type,state,currency,amount,fees,shipping,refunds,source,evidence_ref,observed_at,idempotency_key,supersedes_event_id,details_json)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (item_id,prediction_id,event_type,state,currency,str(amount) if amount is not None else None,
               str(fees),str(shipping),str(refunds),source,evidence_ref,timestamp,key,supersedes_event_id,self._canonical(details)))
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

