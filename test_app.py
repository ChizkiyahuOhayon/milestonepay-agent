import json
from pathlib import Path
import tempfile
import unittest
import uuid

from extract import grounded_amount, line_amount, validate_extraction
from order_draft import build_order
from paypal import PayPal


class ExtractionValidationTests(unittest.TestCase):
    def test_two_unpriced_deliverables_on_same_line_survive_validation(self):
        brief = ("Total budget: EUR 120.00.\n"
                 "First research the topic, then write a report. "
                 "No amount has been agreed for either milestone.")
        result = validate_extraction(brief, {"milestones": [
            {"title": "Research the topic", "line": 1},
            {"title": "Write a report", "line": 1}]})
        self.assertEqual(len(result["milestones"]), 2)
        self.assertEqual([m["amount"] for m in result["milestones"]], [None, None])
        self.assertTrue(all(m["quote"] == brief.splitlines()[1] for m in result["milestones"]))
        self.assertEqual(sum("Confirm the amount" in w for w in result["warnings"]), 2)

    def test_fabricated_quote_cannot_supply_money(self):
        self.assertIsNone(grounded_amount("999", "Budget EUR 999", "Budget EUR 300"))

    def test_amount_cannot_be_substring_of_larger_number(self):
        self.assertIsNone(grounded_amount("30", "Budget EUR 130", "Budget EUR 130"))

    def test_chinese_adjacent_amount(self):
        self.assertEqual(grounded_amount("300", "总预算300欧元", "总预算300欧元"), "300.00")

    def test_ambiguous_dollar_does_not_become_usd(self):
        result = validate_extraction("Total $300. Build a page.", {
            "currency": "USD", "total": "300", "total_quote": "Total $300", "milestones": []})
        self.assertIsNone(result["currency"])

    def test_missing_total_cannot_be_computed_from_split(self):
        result = validate_extraction("Design EUR 100. Build EUR 200.", {
            "currency": "EUR", "total": "300", "total_quote": "Design EUR 100. Build EUR 200.",
            "milestones": []})
        self.assertIsNone(result["total"])

    def test_unquoted_milestone_removed(self):
        result = validate_extraction("Total EUR 300. Design EUR 300.", {
            "currency": "EUR", "total": "300", "total_quote": "Total EUR 300",
            "milestones": [{"title": "Invented", "line": 99}]})
        self.assertEqual(result["milestones"], [])

    def test_mismatch_is_flagged(self):
        result = validate_extraction("Total EUR 300.\nDesign EUR 100.", {
            "currency": "EUR", "total": "300", "total_quote": "Total EUR 300",
            "milestones": [{"title": "Design", "line": 1}]})
        self.assertTrue(any("do not match" in x for x in result["warnings"]))

    def test_conflicting_totals_require_manual_review(self):
        result = validate_extraction("Total EUR 100. Revised total EUR 150.",
                                     {"milestones": []})
        self.assertIsNone(result["total"])

    def test_bare_dollar_is_ambiguous_even_if_model_says_usd(self):
        result = validate_extraction("Total $120. Milestone 1: Draft $120.",
                                     {"currency": "USD", "milestones": []})
        self.assertIsNone(result["currency"])

    def test_milestone_ordinal_is_not_a_price(self):
        self.assertEqual(line_amount("Milestone 2: Deliver files, EUR 80.00."), "80.00")

    def test_two_prices_on_same_line_are_ambiguous(self):
        self.assertIsNone(line_amount("Design EUR 40.00 or EUR 80.00"))

    def test_model_generated_price_is_ignored(self):
        result = validate_extraction("Total EUR 100.\nDesign EUR 100.", {
            "milestones": [{"title": "Design", "line": 1, "amount": "9999"}]})
        self.assertEqual(result["milestones"][0]["amount"], "100.00")

    def test_intro_not_payable_when_stages_are_labelled(self):
        result = validate_extraction("Create a poster.\nTotal EUR 100.\nMilestone 1: Design EUR 100.", {
            "milestones": [{"title": "Create a poster", "line": 0}, {"title": "Design", "line": 2}]})
        self.assertEqual(len(result["milestones"]), 1)
        self.assertEqual(result["milestones"][0]["amount"], "100.00")

    def test_omitted_explicit_stage_is_flagged(self):
        result = validate_extraction("Milestone 1: Draft EUR 40.\nMilestone 2: Deliver EUR 60.", {
            "milestones": [{"title": "Draft", "line": 0}]})
        self.assertTrue(any("Some labelled milestones" in w for w in result["warnings"]))


class FakePayPal(PayPal):
    """Test double. No network operation; never evidence of PayPal acceptance."""
    def __init__(self, database):
        super().__init__(database)
        self.calls = []
        self.status = "APPROVED"
        self.amount = {"currency_code": "EUR", "value": "70.20"}
        self.fail_create_once = False

    def call(self, method, path, body=None, request_id=None):
        self.calls.append((method, path, request_id))
        if path == "/v2/checkout/orders":
            if self.fail_create_once:
                self.fail_create_once = False
                raise TimeoutError()
            return {"id": "TESTORDER123"}
        if method == "GET" and self.status != "COMPLETED":
            return {"id": "TESTORDER123", "status": self.status}
        self.status = "COMPLETED"
        return {"id": "TESTORDER123", "status": "COMPLETED", "purchase_units": [{
            "payments": {"captures": [{"id": "TESTCAPTURE123", "status": "COMPLETED", "amount": self.amount}]}}]}


class SandboxAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paypal = FakePayPal(Path(self.temp.name) / "orders.sqlite3")
        self.key = str(uuid.uuid4())
        agreement = json.loads(Path(__file__).with_name("synthetic_agreement.json").read_text())
        self.body = build_order(agreement, "m2", confirmed=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_create_retry_reuses_id_after_timeout(self):
        self.paypal.fail_create_once = True
        with self.assertRaises(TimeoutError):
            self.paypal.create(self.key, self.body)
        self.paypal.create(self.key, self.body)
        self.assertEqual([c[2] for c in self.paypal.calls], [self.key, self.key])
        self.paypal.create(self.key, self.body)
        self.assertEqual(len(self.paypal.calls), 2)

    def test_same_key_cannot_be_rebound_to_changed_terms(self):
        self.paypal.create(self.key, self.body)
        self.body["purchase_units"][0]["amount"]["value"] = "80.00"
        with self.assertRaises(ValueError):
            self.paypal.create(self.key, self.body)

    def test_unapproved_order_not_captured(self):
        self.paypal.create(self.key, self.body)
        self.paypal.status = "CREATED"
        with self.assertRaises(ValueError):
            self.paypal.capture(self.key)
        self.assertFalse(any(c[1].endswith("/capture") for c in self.paypal.calls))

    def test_capture_retry_does_not_duplicate(self):
        self.paypal.create(self.key, self.body)
        receipt = self.paypal.capture(self.key)
        self.assertEqual(receipt["status"], "SANDBOX_COMPLETED")
        self.assertFalse(receipt["real_money_received"])
        self.assertEqual(receipt, self.paypal.capture(self.key))
        self.assertEqual(sum(c[1].endswith("/capture") for c in self.paypal.calls), 1)

    def test_captured_currency_mismatch_not_marked_paid(self):
        self.paypal.create(self.key, self.body)
        self.paypal.amount["currency_code"] = "USD"
        with self.assertRaises(ValueError):
            self.paypal.capture(self.key)
        with self.paypal.connect() as db:
            self.assertIsNone(db.execute("SELECT receipt FROM orders").fetchone()[0])

    def test_completed_order_reconciled_without_recapture(self):
        self.paypal.create(self.key, self.body)
        self.paypal.status = "COMPLETED"
        self.paypal.capture(self.key)
        self.assertFalse(any(c[1].endswith("/capture") for c in self.paypal.calls))


if __name__ == "__main__":
    unittest.main()
