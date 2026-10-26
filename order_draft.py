"""Offline PayPal sandbox request preparation. Does not contact PayPal."""
import argparse
from decimal import Decimal
import json
from pathlib import Path
import re

SANDBOX_CREATE_URL = "https://api-m.sandbox.paypal.com/v2/checkout/orders"


def money(value):
    # This prototype deliberately supports two-decimal EUR/USD only.
    if not isinstance(value, str) or not re.fullmatch(r"(0|[1-9][0-9]{0,6})\.[0-9]{2}", value):
        raise ValueError("Use an explicit two-decimal amount string")
    amount = Decimal(value)
    if amount <= 0:
        raise ValueError("Amount must be positive")
    return amount


def build_order(agreement, milestone_id, *, confirmed=False):
    """Build one milestone's body from terms already reviewed by a human."""
    if confirmed is not True:
        raise ValueError("Human confirmation required")
    if agreement.get("review_flags") or any(m.get("review_flags") for m in agreement["milestones"]):
        raise ValueError("Resolve uncertain terms before confirming payment")
    currency = agreement["currency"]
    if currency not in ("EUR", "USD"):
        raise ValueError("Prototype supports only EUR and USD")
    milestones = agreement["milestones"]
    if not milestones:
        raise ValueError("At least one milestone required")
    ids = [m["id"] for m in milestones]
    if any(not isinstance(i, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", i) for i in ids):
        raise ValueError("Invalid milestone ID")
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate milestone ID")
    amounts = [money(m["amount"]) for m in milestones]
    if sum(amounts, Decimal("0.00")) != money(agreement["total"]):
        raise ValueError("Milestones do not sum to approved total")
    if milestone_id not in ids:
        raise ValueError("Unknown milestone")
    milestone = milestones[ids.index(milestone_id)]
    description = milestone["title"]
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 127:
        raise ValueError("Description must contain 1-127 characters")
    return {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": milestone_id,
            "description": description,
            "amount": {"currency_code": currency, "value": milestone["amount"]},
        }],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("agreement", type=Path)
    parser.add_argument("milestone_id")
    parser.add_argument("--confirm", action="store_true", help="Confirm reviewed synthetic terms")
    args = parser.parse_args()
    agreement = json.loads(args.agreement.read_text(encoding="utf-8"))
    body = build_order(agreement, args.milestone_id, confirmed=args.confirm)
    print(json.dumps({
        "status": "OFFLINE_DRAFT_ONLY",
        "target": SANDBOX_CREATE_URL,
        "body": body,
        "network_request_sent": False,
    }, ensure_ascii=False, indent=2))
