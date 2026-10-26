from pathlib import Path
import tempfile
import unittest

from checkout import CheckoutFlow, approval_url
from test_app import FakePayPal


class RedirectPayPal(FakePayPal):
    def __init__(self, path):
        super().__init__(path)
        self.status = "CREATED"

    def call(self, method, path, body=None, request_id=None):
        if method == "GET":
            self.calls.append((method, path, request_id))
            result = {"id": "TESTORDER123", "status": self.status,
                      "purchase_units": [{"amount": self.amount}],
                      "links": [{"rel": "approve", "href": "https://www.sandbox.paypal.com/checkoutnow?token=TESTORDER123"}]}
            if self.status == "COMPLETED":
                result["purchase_units"][0]["payments"] = {"captures": [{"id": "TESTCAPTURE123", "status": "COMPLETED", "amount": self.amount}]}
            return result
        return super().call(method, path, body, request_id)


class RedirectCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paypal = RedirectPayPal(Path(self.temp.name) / "test.sqlite3")
        self.flow = CheckoutFlow(self.paypal, "http://127.0.0.1:8765")
        self.plan = {"currency": "EUR", "total": "70.20", "milestones": [
            {"id": "m1", "title": "Synthetic deliverable", "amount": "70.20"}]}
        self.version = self.flow.confirm("session", self.plan)
        self.created = self.flow.create("session", self.version, "m1")

    def tearDown(self):
        self.temp.cleanup()

    def captures(self):
        return [c for c in self.paypal.calls if c[0] == "POST" and c[1].endswith("/capture")]

    def test_retries_do_not_create_or_capture_twice(self):
        self.assertEqual(self.flow.create("session", self.version, "m1"), self.created)
        self.paypal.status = "APPROVED"
        receipt = self.flow.finalize("session", self.created["checkout_id"], self.created["id"])
        self.assertEqual(self.flow.finalize("session", self.created["checkout_id"], self.created["id"]), receipt)
        self.assertEqual(len(self.captures()), 1)

    def test_history_is_scoped_to_persistent_local_identity(self):
        restarted = CheckoutFlow(self.paypal, "http://127.0.0.1:8765")
        self.assertEqual(restarted.list_records("session")[0]["order_id"], self.created["id"])
        self.assertEqual(restarted.list_records("other"), [])
        self.assertNotIn("approval_url", restarted.list_records("session")[0])

    def test_lost_capture_response_reconciles_once_after_flow_restart(self):
        original = self.paypal.call
        lost = False
        def lose_response(method, path, body=None, request_id=None):
            nonlocal lost
            result = original(method, path, body, request_id)
            if method == "POST" and path.endswith("/capture") and not lost:
                lost = True
                raise TimeoutError("Simulated lost response after remote completion")
            return result
        self.paypal.call = lose_response
        self.paypal.status = "APPROVED"
        with self.assertRaises(TimeoutError):
            self.flow.finalize("session", self.created["checkout_id"], self.created["id"])
        self.assertEqual(self.paypal.status, "COMPLETED")
        restarted = CheckoutFlow(self.paypal, "http://127.0.0.1:8765")
        receipt = restarted.finalize("session", self.created["checkout_id"], self.created["id"])
        self.assertEqual(receipt["status"], "SANDBOX_COMPLETED")
        self.assertEqual(len(self.captures()), 1)

    def test_cancelled_order_cannot_be_reconciled(self):
        self.flow.cancel("session", self.created["checkout_id"])
        self.paypal.status = "APPROVED"
        with self.assertRaises(ValueError):
            self.flow.finalize("session", self.created["checkout_id"], self.created["id"])
        self.assertEqual(self.captures(), [])

    def test_created_is_not_approved(self):
        with self.assertRaises(ValueError):
            self.flow.finalize("session", self.created["checkout_id"], self.created["id"])
        self.assertEqual(self.captures(), [])

    def test_edited_plan_rejects_old_approval_without_capture(self):
        self.plan["total"] = self.plan["milestones"][0]["amount"] = "80.00"
        self.flow.confirm("session", self.plan)
        self.paypal.status = "APPROVED"
        with self.assertRaises(ValueError):
            self.flow.finalize("session", self.created["checkout_id"], self.created["id"])
        self.assertEqual(self.captures(), [])

    def test_explicit_edit_invalidation_blocks_old_version(self):
        self.flow.invalidate("session", self.version)
        with self.assertRaises(ValueError):
            self.flow.create("session", self.version, "m1")
        self.assertEqual(self.flow.record("session", self.created["checkout_id"])["status"], "STALE")

    def test_unknown_or_different_session_tokens_rejected(self):
        for session, checkout, token in [("session", "unknown", "TESTORDER123"),
                                         ("other", self.created["checkout_id"], self.created["id"]),
                                         ("session", self.created["checkout_id"], "OTHERORDER")]:
            with self.subTest(session=session, checkout=checkout, token=token), self.assertRaises(ValueError):
                self.flow.finalize(session, checkout, token)
        self.assertEqual(self.captures(), [])

    def test_remote_amount_mismatch_prevents_capture(self):
        self.paypal.amount = {"currency_code": "EUR", "value": "99.00"}
        self.paypal.status = "APPROVED"
        with self.assertRaises(ValueError):
            self.flow.finalize("session", self.created["checkout_id"], self.created["id"])
        self.assertEqual(self.captures(), [])

    def test_cancel_requires_a_new_plan_version(self):
        self.flow.cancel("session", self.created["checkout_id"])
        with self.assertRaises(ValueError):
            self.flow.create("session", self.version, "m1")
        self.assertGreater(self.flow.confirm("session", self.plan), self.version)

    def test_approval_url_cannot_be_attacker_or_live_host(self):
        for url in ("https://www.paypal.com/checkoutnow?token=X", "https://www.sandbox.paypal.com.evil.test/checkoutnow?token=X",
                    "javascript:alert(1)", "https://www.sandbox.paypal.com/checkoutnow?token=OTHER"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                approval_url({"id": "X", "links": [{"rel": "approve", "href": url}]})

    def test_loaded_page_can_invalidate_current_plan_without_edit_confirmation(self):
        self.assertEqual(self.flow.active_version("session"), self.version)
        self.flow.invalidate("session", self.flow.active_version("session"))
        self.assertIsNone(self.flow.active_version("session"))
        self.paypal.status = "APPROVED"
        with self.assertRaises(ValueError):
            self.flow.finalize("session", self.created["checkout_id"], self.created["id"])
        self.assertEqual(self.captures(), [])

    def test_completed_receipt_remains_truthful_after_later_plan_edit(self):
        self.paypal.status = "APPROVED"
        receipt = self.flow.finalize("session", self.created["checkout_id"], self.created["id"])
        self.flow.invalidate("session", self.version)
        self.assertEqual(self.flow.finalize("session", self.created["checkout_id"], self.created["id"]), receipt)
        self.assertEqual(len(self.captures()), 1)
