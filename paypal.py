"""Sandbox-only PayPal adapter with persisted request identities."""
import base64
from contextlib import contextmanager
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import sqlite3
import urllib.error
import urllib.request
import uuid

BASE = "https://api-m.sandbox.paypal.com"


class PayPal:
    def __init__(self, database):
        self.database = str(database)
        Path(database).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS orders (checkout TEXT PRIMARY KEY, body TEXT NOT NULL, order_id TEXT, receipt TEXT)")

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.database)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @property
    def configured(self):
        return bool(os.environ.get("PAYPAL_CLIENT_ID") and os.environ.get("PAYPAL_CLIENT_SECRET"))

    def call(self, method, path, body=None, request_id=None):
        if not self.configured:
            raise ValueError("PayPal Sandbox is not connected. Use request preview for now.")
        credentials = (os.environ["PAYPAL_CLIENT_ID"] + ":" + os.environ["PAYPAL_CLIENT_SECRET"]).encode()
        token_req = urllib.request.Request(
            BASE + "/v1/oauth2/token", b"grant_type=client_credentials",
            {"Authorization": "Basic " + base64.b64encode(credentials).decode(),
             "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(token_req, timeout=30) as response:
            token = json.load(response)["access_token"]
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json",
                   "Prefer": "return=representation"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        req = urllib.request.Request(BASE + path,
            json.dumps(body).encode() if body is not None else None, headers, method=method)
        with urllib.request.urlopen(req, timeout=30) as response:
            payload = response.read()
            return json.loads(payload) if payload else {}

    def create(self, checkout_id, body):
        # A retry must bind the same UUID to exactly the same approved terms.
        checkout_id = str(uuid.UUID(checkout_id))
        encoded = json.dumps(body, sort_keys=True)
        with self.connect() as db:
            row = db.execute("SELECT body, order_id FROM orders WHERE checkout=?", (checkout_id,)).fetchone()
            if row and row[0] != encoded:
                raise ValueError("Terms changed. Start a new checkout before creating an order.")
            if row and row[1]:
                return {"id": row[1], "checkout_id": checkout_id}
            db.execute("INSERT OR IGNORE INTO orders(checkout,body) VALUES (?,?)", (checkout_id, encoded))
        # The row remains on a timeout, so retrying reuses PayPal-Request-Id.
        result = self.call("POST", "/v2/checkout/orders", body, checkout_id)
        order_id = result.get("id", "")
        if not re.fullmatch(r"[A-Za-z0-9]{5,64}", order_id):
            raise ValueError("PayPal did not return an order ID")
        with self.connect() as db:
            db.execute("UPDATE orders SET order_id=? WHERE checkout=?", (order_id, checkout_id))
        return {"id": order_id, "checkout_id": checkout_id}

    def capture(self, checkout_id):
        checkout_id = str(uuid.UUID(checkout_id))
        with self.connect() as db:
            row = db.execute("SELECT body,order_id,receipt FROM orders WHERE checkout=?", (checkout_id,)).fetchone()
        if not row or not row[1]:
            raise ValueError("Create an order first")
        if row[2]:
            return json.loads(row[2])
        order = self.call("GET", "/v2/checkout/orders/" + row[1])
        if order.get("status") == "APPROVED":
            capture_id = str(uuid.uuid5(uuid.UUID(checkout_id), "capture"))
            order = self.call("POST", "/v2/checkout/orders/" + row[1] + "/capture", {}, capture_id)
        if order.get("status") != "COMPLETED":
            raise ValueError("Order is not completed. Approve it with the Sandbox buyer account.")
        expected = json.loads(row[0])["purchase_units"][0]["amount"]
        captures = [c for unit in order.get("purchase_units", [])
                    for c in unit.get("payments", {}).get("captures", [])]
        if (not captures or any(c.get("status") != "COMPLETED" for c in captures)
                or any(c.get("amount", {}).get("currency_code") != expected["currency_code"] for c in captures)
                or sum(Decimal(c["amount"]["value"]) for c in captures) != Decimal(expected["value"])):
            raise ValueError("Payment requires review: captured amount or currency does not match.")
        receipt = {"status": "SANDBOX_COMPLETED", "order_id": row[1],
                   "capture_ids": [c["id"] for c in captures], "amount": expected,
                   "real_money_received": False}
        with self.connect() as db:
            db.execute("UPDATE orders SET receipt=? WHERE checkout=?", (json.dumps(receipt), checkout_id))
        return receipt
