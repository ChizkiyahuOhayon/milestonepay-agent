from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
import uuid

from lifecycle import Lifecycle
from paypal import PayPal

PLAN = {'currency': 'EUR', 'total': '8.00', 'milestones': [
    {'id': 'drawing', 'title': 'Drawing', 'amount': '8.00',
     'acceptance_criteria': 'PDF\nPNG', 'source_quote': 'Human entered: drawing EUR 8.00'}]}


class FakeLifecyclePayPal(PayPal):
    """Deterministic fake, including response-loss after a remote side effect."""
    def __init__(self, database):
        super().__init__(database)
        self.calls, self.responses = [], {}
        self.remote = None
        self.authorization = None
        self.captured = None
        self.refunds = {}
        self.lose_response = None
        self.pending_refund = False

    @property
    def configured(self):
        return True

    def call(self, method, path, body=None, request_id=None):
        self.calls.append((method, path, deepcopy(body), request_id))
        if method == 'POST' and request_id in self.responses:
            return deepcopy(self.responses[request_id])
        amount = {'value': '8.00', 'currency_code': 'EUR'}
        if path == '/v2/checkout/orders':
            self.remote = {'id': 'ORDER12345', 'intent': body['intent'], 'status': 'CREATED',
                'purchase_units': deepcopy(body['purchase_units']), 'links': [
                {'rel': 'payer-action', 'href': 'https://www.sandbox.paypal.com/checkoutnow?token=ORDER12345'}]}
            result = self.remote
        elif method == 'GET' and path == '/v2/checkout/orders/ORDER12345':
            return deepcopy(self.remote)
        elif path.endswith('/authorize'):
            self.authorization = {'id': 'AUTH12345', 'status': 'CREATED', 'amount': amount}
            self.remote['status'] = 'COMPLETED'
            self.remote['purchase_units'][0]['payments'] = {'authorizations': [self.authorization]}
            result = self.remote
        elif method == 'GET' and path.endswith('/authorizations/AUTH12345'):
            return deepcopy(self.authorization)
        elif path.endswith('/capture'):
            self.authorization['status'] = 'CAPTURED'
            self.captured = {'id': 'CAPTURE12345', 'status': 'COMPLETED', 'amount': amount}
            self.remote['purchase_units'][0]['payments']['captures'] = [self.captured]
            result = self.captured
        elif path.endswith('/void'):
            self.authorization['status'] = 'VOIDED'
            result = {}  # Real void can return empty 204.
        elif method == 'GET' and path.endswith('/captures/CAPTURE12345'):
            return deepcopy(self.captured)
        elif path.endswith('/refund'):
            ident = 'REFUND' + str(len(self.refunds) + 1).zfill(5)
            result = {'id': ident, 'status': 'PENDING' if self.pending_refund else 'COMPLETED',
                      'amount': deepcopy(body['amount'])}
            self.refunds[ident] = result
        elif method == 'GET' and '/refunds/' in path:
            return deepcopy(self.refunds[path.rsplit('/', 1)[1]])
        else:
            raise AssertionError((method, path))
        self.responses[request_id] = deepcopy(result)
        if self.lose_response and path.endswith(self.lose_response):
            self.lose_response = None
            raise TimeoutError('Simulated lost response')
        return deepcopy(result)

    def approve(self):
        self.remote['status'] = 'APPROVED'


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paypal = FakeLifecyclePayPal(Path(self.temp.name) / 'payments.sqlite3')
        self.flow = Lifecycle(self.paypal, 'http://127.0.0.1:8765')
        self.record = {'milestone_id': 'drawing', 'amount': {'value': '8.00', 'currency_code': 'EUR'}, 'refunded': '0.00'}
        self.record.update(self.flow.create(PLAN, 'drawing', 'version1', str(uuid.uuid4())))

    def tearDown(self):
        self.temp.cleanup()

    def authorize(self):
        self.paypal.approve()
        self.record.update(self.flow.authorize(self.record, str(uuid.uuid4())))

    def test_authorize_then_capture_and_recover_without_second_charge(self):
        self.authorize()
        self.paypal.lose_response = '/capture'
        with self.assertRaises(TimeoutError):
            self.flow.capture(self.record, str(uuid.uuid4()))
        self.record.update(self.flow.capture(self.record, str(uuid.uuid4())))
        self.assertEqual(self.record['status'], 'CAPTURED')
        self.assertEqual(sum(c[0] == 'POST' and c[1].endswith('/capture') for c in self.paypal.calls), 1)

    def test_uncaptured_authorization_voids_and_rechecks_read_only(self):
        self.authorize()
        self.record.update(self.flow.void(self.record, str(uuid.uuid4())))
        self.assertEqual(self.record['status'], 'VOIDED')
        before = len(self.paypal.calls)
        self.assertEqual(self.flow.recheck(self.record)['remote_authorization_status'], 'VOIDED')
        self.assertTrue(all(c[0] == 'GET' for c in self.paypal.calls[before:]))

    def test_partial_then_remaining_refund(self):
        self.authorize()
        self.record.update(self.flow.capture(self.record, str(uuid.uuid4())))
        self.record.update(self.flow.refund(self.record, '3.00', str(uuid.uuid4())))
        self.assertEqual(self.record['status'], 'PARTIALLY_REFUNDED')
        self.record.update(self.flow.refund(self.record, '5.00', str(uuid.uuid4())))
        self.assertEqual(self.record['status'], 'REFUNDED')
        self.assertEqual(self.record['refunded'], '8.00')

    def test_unapproved_order_cannot_authorize(self):
        with self.assertRaises(ValueError):
            self.flow.authorize(self.record, str(uuid.uuid4()))
        self.assertFalse(any(c[1].endswith('/authorize') for c in self.paypal.calls))

    def test_lost_authorize_response_is_reconciled_without_new_authorization(self):
        self.paypal.approve()
        self.paypal.lose_response = '/authorize'
        with self.assertRaises(TimeoutError):
            self.flow.authorize(self.record, str(uuid.uuid4()))
        self.assertEqual(self.flow.authorize(self.record, str(uuid.uuid4()))['status'], 'AUTHORIZED')
        self.assertEqual(sum(c[1].endswith('/authorize') for c in self.paypal.calls), 1)

    def test_remote_money_and_milestone_mismatch_refuse(self):
        for field, value in [('amount', {'value': '9.00', 'currency_code': 'EUR'}), ('reference_id', 'other')]:
            unit = self.paypal.remote['purchase_units'][0]
            original = unit[field]
            unit[field] = value
            with self.assertRaises(ValueError):
                self.flow.authorize(self.record, str(uuid.uuid4()))
            unit[field] = original

    def test_lost_refund_response_reuses_same_key_and_no_duplicate(self):
        self.authorize()
        self.record.update(self.flow.capture(self.record, str(uuid.uuid4())))
        key = str(uuid.uuid4())
        self.paypal.lose_response = '/refund'
        with self.assertRaises(TimeoutError):
            self.flow.refund(self.record, '2.00', key)
        self.record.update(self.flow.refund(self.record, '2.00', key))
        self.assertEqual(self.record['refunded'], '2.00')
        self.assertEqual(len(self.paypal.refunds), 1)

    def test_pending_refund_does_not_claim_completion(self):
        self.authorize()
        self.record.update(self.flow.capture(self.record, str(uuid.uuid4())))
        self.paypal.pending_refund = True
        self.record.update(self.flow.refund(self.record, '8.00', str(uuid.uuid4())))
        self.assertEqual(self.record['status'], 'REFUND_PENDING')
        self.assertEqual(self.record['refunded'], '0.00')
        self.paypal.refunds[self.record['refund_id']]['status'] = 'COMPLETED'
        self.record.update(self.flow.recheck(self.record))
        self.assertEqual(self.record['status'], 'REFUNDED')
