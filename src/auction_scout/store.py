from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from .models import Listing


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


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self): self.db.close()

    def _reserved_exposure(self, currency: str, now_iso: str) -> Decimal:
        rows = self.db.execute("SELECT max_amount FROM bid_intents WHERE currency=? AND status IN ('armed','submitting','submitted','unknown') AND end_time>?",
                               (currency, now_iso)).fetchall()
        return sum((Decimal(r["max_amount"]) for r in rows), Decimal("0"))

    def audit(self, action: str, item_id: str | None, details: dict):
        with self.db:
            self.db.execute("INSERT INTO audit_log(occurred_at,action,item_id,details_json) VALUES(?,?,?,?)",
                            (datetime.now(timezone.utc).isoformat(), action, item_id, json.dumps(details, sort_keys=True)))

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

    def arm_intent(self, item_id: str, max_amount: Decimal, currency: str, end_time: datetime, lead_seconds: int):
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
            if self.db.execute("SELECT 1 FROM bid_intents WHERE item_id=? AND status='armed'", (item_id,)).fetchone():
                raise ValueError("an armed intent already exists for this item; cancel it before replacing")
            self.db.execute("INSERT INTO bid_intents(item_id,max_amount,currency,end_time,status,submit_before,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                            (item_id, str(max_amount), currency, end_time.isoformat(), "armed", submit_before_dt.isoformat(), now.isoformat(), now.isoformat()))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self.audit("bid_intent_armed", item_id, {"max_amount": str(max_amount), "currency": currency, "submit_before": submit_before_dt.isoformat()})

    def set_budget(self, currency: str, amount: Decimal):
        if amount <= 0: raise ValueError("portfolio cap must be positive")
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

