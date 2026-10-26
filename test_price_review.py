"""Regression checks for price ambiguity, allocation and immutable provenance."""
import json
from pathlib import Path
import tempfile
import unittest

from extract import line_amount, normalized_money, validate_extraction
from order_draft import build_order
from checkout import CheckoutFlow
from test_checkout import RedirectPayPal


class PriceReviewTests(unittest.TestCase):
    def plan(self, brief, count=1):
        return validate_extraction(brief, {"milestones": [{"title": "Synthetic task", "line": 0} for _ in range(count)]})

    def test_grouped_prices_do_not_become_fractional_prefixes(self):
        for text, expected in [("Design USD 7,500.", "7500.00"), ("Design EUR 2,400.50.", "2400.50"), ("Design EUR 2.400,50.", "2400.50")]:
            with self.subTest(text=text):
                self.assertEqual(line_amount(text), expected)

    def test_malformed_or_negative_prices_are_not_truncated(self):
        for text in ("Design EUR 7,50.00.", "Design EUR 7.500.00.", "设计 -70美元", "Design USD -70.00"):
            with self.subTest(text=text):
                self.assertIsNone(line_amount(text))
        self.assertIsNone(normalized_money("12,34"))

    def test_hourly_and_piece_rates_require_charge_confirmation(self):
        for text in ("Edit USD 55/hr; at most 12 hours", "Translate USD 0.12 per word; 900 words", "剪辑每条30美元"):
            with self.subTest(text=text):
                self.assertIsNone(line_amount(text))
                self.assertTrue(self.plan(text)["milestones"][0]["review_flags"])

    def test_shared_price_is_not_duplicated(self):
        for text in ("Design and deliver a logo, EUR 250.", "Research then write a report for USD 800."):
            with self.subTest(text=text):
                rows=self.plan(text, 2)["milestones"]
                self.assertEqual([m["amount"] for m in rows], [None, None])
                self.assertTrue(all("Shared price" in " ".join(m["review_flags"]) for m in rows))

    def test_unsupported_or_ambiguous_currency_is_not_selected(self):
        for text in ("Design CAD or USD 700.", "Design EUR 100 or GBP 90.", "Design $90.", "设计预算人民币300元"):
            with self.subTest(text=text):
                self.assertIsNone(self.plan(text)["currency"])
        self.assertEqual(self.plan("Design USD 700.")["currency"], "USD")
        self.assertEqual(self.plan("设计700欧元")["currency"], "EUR")

    def test_total_line_evidence_survives_without_automatic_allocation(self):
        for text, total in [("Total USD 2,700. Design then build, split 30/70.", "2700.00"), ("总预算6,800欧元。设计后交付。", "6800.00")]:
            with self.subTest(text=text):
                plan=self.plan(text, 2)
                self.assertEqual(plan["total"], total)
                self.assertEqual([m["quote"] for m in plan["milestones"]], [text, text])
                self.assertEqual([m["amount"] for m in plan["milestones"]], [None, None])

    def test_revised_prices_on_same_line_are_unresolved(self):
        for text in ("Logo USD 220, revised USD 330.", "Logo EUR 120 or EUR 160."):
            with self.subTest(text=text):
                stage=self.plan(text)["milestones"][0]
                self.assertIsNone(stage["amount"])
                self.assertTrue(any("Multiple candidate" in flag for flag in stage["review_flags"]))

    def test_server_rejects_unresolved_flags_even_with_valid_amounts(self):
        plan={"currency":"EUR", "total":"50.00", "milestones":[{"id":"m1", "title":"Synthetic design", "amount":"50.00", "review_flags":["Confirm allocation"]}]}
        with self.assertRaisesRegex(ValueError, "uncertain"):
            build_order(plan, "m1", confirmed=True)
        plan["milestones"][0]["review_flags"]=[]
        plan["review_flags"]=["Confirm currency"]
        with self.assertRaisesRegex(ValueError, "uncertain"):
            build_order(plan, "m1", confirmed=True)

    def test_order_sources_survive_later_edits_and_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            paypal=RedirectPayPal(Path(temp)/"test.sqlite3")
            # CheckoutFlow migrates the legacy table created by the previous release.
            with paypal.connect() as db:
                db.execute("CREATE TABLE checkouts (checkout TEXT PRIMARY KEY, session TEXT, version INTEGER, milestone TEXT, order_id TEXT, amount TEXT, approval_url TEXT, status TEXT, created_at TEXT, receipt TEXT)")
            flow=CheckoutFlow(paypal,"http://127.0.0.1:8765")
            plan={"currency":"EUR","total":"70.20","field_sources":{"currency":"manual","total":"parser"},"milestones":[{"id":"m1","title":"Synthetic task","amount":"70.20","field_sources":{"title":"model","amount":"manual"}}]}
            version=flow.confirm("s",plan)
            created=flow.create("s",version,"m1")
            original=json.loads(flow.record("s",created["checkout_id"])["field_sources"])
            plan["milestones"][0]["field_sources"]["title"]="manual"
            flow.confirm("s",plan)
            restarted=CheckoutFlow(paypal,"http://127.0.0.1:8765")
            self.assertEqual(json.loads(restarted.record("s",created["checkout_id"])["field_sources"]),original)
            self.assertEqual(original["milestone"]["title"],"model")
            self.assertEqual(original["milestone"]["amount"],"manual")


if __name__ == "__main__":
    unittest.main()
