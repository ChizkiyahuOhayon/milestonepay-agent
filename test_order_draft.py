import copy
import json
from pathlib import Path
import unittest

from order_draft import build_order, money


class OrderDraftTests(unittest.TestCase):
    def setUp(self):
        self.agreement = json.loads(
            Path(__file__).with_name("synthetic_agreement.json").read_text()
        )

    def test_correct_milestone_amount_and_currency(self):
        body = build_order(self.agreement, "m2", confirmed=True)
        self.assertEqual(body["purchase_units"][0]["amount"],
                         {"currency_code": "EUR", "value": "70.20"})
        self.assertEqual(body["purchase_units"][0]["reference_id"], "m2")
        self.assertEqual(body["intent"], "CAPTURE")

    def test_confirmation_required(self):
        with self.assertRaises(ValueError):
            build_order(self.agreement, "m1")

    def test_truthy_string_is_not_confirmation(self):
        with self.assertRaises(ValueError):
            build_order(self.agreement, "m1", confirmed="yes")

    def test_total_mismatch_rejected(self):
        self.agreement["total"] = "100.31"
        with self.assertRaises(ValueError):
            build_order(self.agreement, "m1", confirmed=True)

    def test_duplicate_ids_rejected(self):
        self.agreement["milestones"][1]["id"] = "m1"
        with self.assertRaises(ValueError):
            build_order(self.agreement, "m1", confirmed=True)

    def test_unknown_milestone_rejected(self):
        with self.assertRaises(ValueError):
            build_order(self.agreement, "missing", confirmed=True)

    def test_unsupported_currency_rejected(self):
        self.agreement["currency"] = "JPY"
        with self.assertRaises(ValueError):
            build_order(self.agreement, "m1", confirmed=True)

    def test_invalid_money_rejected(self):
        for value in ("-1.00", "0.00", "1.001", "NaN", "1e2", 10.10, "01.00"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                money(value)

    def test_no_input_mutation(self):
        original = copy.deepcopy(self.agreement)
        build_order(self.agreement, "m1", confirmed=True)
        self.assertEqual(self.agreement, original)


if __name__ == "__main__":
    unittest.main()
