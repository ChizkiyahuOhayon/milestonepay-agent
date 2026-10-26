"""Versioned, single-user Sandbox redirect checkout. No live-money path."""
from datetime import datetime, timezone
from decimal import Decimal
import json
import sqlite3
from urllib.parse import parse_qs, urlparse
import uuid

from order_draft import build_order


def approval_url(order):
    for link in order.get("links", []):
        if link.get("rel") not in ("approve", "payer-action"):
            continue
        href = link.get("href", "")
        url = urlparse(href)
        if (url.scheme == "https" and url.hostname in ("www.sandbox.paypal.com", "sandbox.paypal.com")
                and url.port in (None, 443) and not url.username and not url.password
                and url.path == "/checkoutnow" and not url.fragment
                and parse_qs(url.query).get("token") == [order["id"]]):
            return href
    raise ValueError("PayPal did not return an approved Sandbox checkout URL")


class CheckoutFlow:
    def __init__(self, paypal, origin):
        self.paypal, self.origin = paypal, origin
        with paypal.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS plans (session TEXT PRIMARY KEY, version INTEGER, agreement TEXT, active INTEGER)")
            db.execute("CREATE TABLE IF NOT EXISTS checkouts (checkout TEXT PRIMARY KEY, session TEXT, version INTEGER, milestone TEXT, order_id TEXT, amount TEXT, approval_url TEXT, status TEXT, created_at TEXT, receipt TEXT)")
            if "field_sources" not in [row[1] for row in db.execute("PRAGMA table_info(checkouts)")]:
                db.execute("ALTER TABLE checkouts ADD COLUMN field_sources TEXT")

    def invalidate(self, session, version):
        with self.paypal.connect() as db:
            db.execute("UPDATE plans SET active=0 WHERE session=? AND version=?", (session, version))
            db.execute("UPDATE checkouts SET status='STALE' WHERE session=? AND version=? AND status NOT IN ('CAPTURED','CANCELLED')", (session, version))

    def confirm(self, session, agreement):
        if not isinstance(agreement, dict) or not agreement.get("milestones"):
            raise ValueError("Review a nonempty plan first")
        for milestone in agreement["milestones"]:
            build_order(agreement, milestone["id"], confirmed=True)
        encoded = json.dumps(agreement, sort_keys=True)
        with self.paypal.connect() as db:
            row = db.execute("SELECT version,agreement,active FROM plans WHERE session=?", (session,)).fetchone()
        if row and row[1] == encoded and row[2]:
            return row[0]
        if row:
            self.invalidate(session, row[0])
        version = row[0] + 1 if row else 1
        with self.paypal.connect() as db:
            db.execute("INSERT OR REPLACE INTO plans VALUES (?,?,?,1)", (session, version, encoded))
        return version

    def current(self, session, version):
        with self.paypal.connect() as db:
            row = db.execute("SELECT version,agreement,active FROM plans WHERE session=?", (session,)).fetchone()
        if type(version) is not int or not row or not row[2] or row[0] != version:
            raise ValueError("Plan changed or expired. Review the terms and create a new payment request.")
        return json.loads(row[1])

    def active_version(self, session):
        with self.paypal.connect() as db:
            row = db.execute("SELECT version FROM plans WHERE session=? AND active=1", (session,)).fetchone()
        return row[0] if row else None

    def record(self, session, checkout):
        with self.paypal.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM checkouts WHERE session=? AND checkout=?", (session, checkout)).fetchone()
        if not row:
            raise ValueError("Unknown local checkout")
        return dict(row)

    def list_records(self, session):
        with self.paypal.connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT checkout,version,milestone,order_id,amount,status,created_at,field_sources FROM checkouts WHERE session=? AND order_id IS NOT NULL ORDER BY created_at DESC", (session,)).fetchall()
        return [{**dict(row), "amount": json.loads(row["amount"])} for row in rows]

    def create(self, session, version, milestone):
        agreement = self.current(session, version)
        body = build_order(agreement, milestone, confirmed=True)
        selected = next(m for m in agreement["milestones"] if m["id"] == milestone)
        sources = {"agreement": agreement.get("field_sources", {}), "milestone": selected.get("field_sources", {})}
        checkout = str(uuid.uuid5(uuid.NAMESPACE_URL, session + ":" + str(version) + ":" + milestone))
        context = {"return_url": self.origin + "/paypal/return?checkout=" + checkout,
                   "cancel_url": self.origin + "/paypal/cancel?checkout=" + checkout,
                   "shipping_preference": "NO_SHIPPING", "user_action": "PAY_NOW"}
        body["payment_source"] = {"paypal": {"experience_context": context}}
        with self.paypal.connect() as db:
            db.execute("INSERT OR IGNORE INTO checkouts(checkout,session,version,milestone,amount,status,created_at,field_sources) VALUES (?,?,?,?,?,'CREATED',?,?)",
                       (checkout, session, version, milestone, json.dumps(body["purchase_units"][0]["amount"]), datetime.now(timezone.utc).isoformat(), json.dumps(sources)))
        row = self.record(session, checkout)
        if row["status"] in ("STALE", "CANCELLED"):
            raise ValueError("Checkout expired. Review the plan again.")
        if not row["order_id"]:
            result = self.paypal.create(checkout, body)
            remote = self.paypal.call("GET", "/v2/checkout/orders/" + result["id"])
            url = approval_url(remote)
            with self.paypal.connect() as db:
                db.execute("UPDATE checkouts SET order_id=?,approval_url=? WHERE checkout=?", (result["id"], url, checkout))
            row = self.record(session, checkout)
        return {"id": row["order_id"], "checkout_id": checkout, "status": row["status"],
                "approval_url": row["approval_url"],
                "receipt_url": "/paypal/return?checkout=" + checkout + "&token=" + row["order_id"]}

    @staticmethod
    def check_amount(order, row):
        units = order.get("purchase_units", [])
        expected = json.loads(row["amount"])
        if (len(units) != 1 or units[0].get("amount", {}).get("currency_code") != expected["currency_code"]
                or Decimal(units[0].get("amount", {}).get("value", "-1")) != Decimal(expected["value"])):
            raise ValueError("PayPal order amount or currency differs from the confirmed plan")

    def finalize(self, session, checkout, token):
        row = self.record(session, checkout)
        if not token or token != row["order_id"]:
            raise ValueError("Unknown PayPal order token")
        if row["status"] in ("CANCELLED", "STALE"):
            raise ValueError("Checkout is cancelled or stale; it will not be captured")
        if row["status"] == "CAPTURED":
            return json.loads(row["receipt"])
        self.current(session, row["version"])
        remote = self.paypal.call("GET", "/v2/checkout/orders/" + token)
        self.check_amount(remote, row)
        if remote.get("status") not in ("APPROVED", "COMPLETED"):
            raise ValueError("Buyer approval is not confirmed; payment is not marked complete")
        with self.paypal.connect() as db:
            db.execute("UPDATE checkouts SET status='APPROVED' WHERE checkout=?", (checkout,))
        receipt = self.paypal.capture(checkout)
        remote = self.paypal.call("GET", "/v2/checkout/orders/" + token)
        self.check_amount(remote, row)
        if remote.get("status") != "COMPLETED":
            raise ValueError("Capture needs reconciliation; completion is not confirmed")
        with self.paypal.connect() as db:
            db.execute("UPDATE checkouts SET status='CAPTURED',receipt=? WHERE checkout=?", (json.dumps(receipt), checkout))
        return receipt

    def cancel(self, session, checkout):
        row = self.record(session, checkout)
        self.current(session, row["version"])
        if row["status"] == "CAPTURED":
            raise ValueError("This test payment was already captured; cancellation cannot undo it")
        with self.paypal.connect() as db:
            db.execute("UPDATE checkouts SET status='CANCELLED' WHERE checkout=?", (checkout,))
        self.invalidate(session, row["version"])
