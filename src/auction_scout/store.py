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
"""


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self): self.db.close()

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
        with self.db:
            # Only one active intent per item; callers must explicitly cancel before replacing.
            if self.db.execute("SELECT 1 FROM bid_intents WHERE item_id=? AND status='armed'", (item_id,)).fetchone():
                raise ValueError("an armed intent already exists for this item; cancel it before replacing")
            self.db.execute("INSERT INTO bid_intents(item_id,max_amount,currency,end_time,status,submit_before,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                            (item_id, str(max_amount), currency, end_time.isoformat(), "armed", submit_before_dt.isoformat(), now.isoformat(), now.isoformat()))
        self.audit("bid_intent_armed", item_id, {"max_amount": str(max_amount), "currency": currency, "submit_before": submit_before_dt.isoformat()})

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

