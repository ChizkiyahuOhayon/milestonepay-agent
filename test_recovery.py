from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from paypal import PayPal
from test_lifecycle import FakeLifecyclePayPal, PLAN
from workflow import Workflow


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paypal = FakeLifecyclePayPal(Path(self.temp.name) / 'state.sqlite3')
        self.flow = Workflow(self.paypal, 'http://127.0.0.1:8765')
        self.state = self.flow.save_plan(deepcopy(PLAN))
        self.version = self.state['version']

    def tearDown(self):
        self.temp.cleanup()

    def act(self, operation):
        p = self.flow.propose(self.version, 'drawing', operation)
        token = self.flow.sign(p['proposal_id'], 'Synthetic Buyer', True)['signature']
        return self.flow.execute(p['proposal_id'], token)

    def test_simultaneous_token_consumption_has_exactly_one_winner(self):
        p = self.flow.propose(self.version, 'drawing', 'authorize')
        payload = self.flow.current_proposal(p['proposal_id'])
        token = self.flow.sign(p['proposal_id'], 'Synthetic Buyer', True)['signature']
        barrier = threading.Barrier(2)
        def consume():
            barrier.wait()
            try:
                self.flow.signatures.consume(p['proposal_id'], payload, token)
                return True
            except PermissionError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: consume(), range(2)))
        self.assertEqual(sum(results), 1)
        with self.paypal.connect() as db:
            self.assertNotIn(token, repr(db.execute('SELECT * FROM permissions').fetchall()))

    def test_saved_receipt_recovers_after_crash_before_local_state_commit(self):
        self.act('authorize')
        self.paypal.approve()
        self.act('authorize')
        self.flow.delivery(self.version, 'drawing', 'PDF and PNG delivered')
        original_persist = self.flow.persist
        calls = 0
        def crash_on_completed_state(state):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('Simulated process interruption')
            original_persist(state)
        with patch.object(self.flow, 'persist', side_effect=crash_on_completed_state):
            with self.assertRaises(RuntimeError):
                self.act('capture')
        recovered = Workflow(self.paypal, 'http://127.0.0.1:8765')
        before = len(self.paypal.calls)
        state = recovered.recheck(self.version, 'drawing')
        self.assertEqual(state['milestones']['drawing']['status'], 'CAPTURED')
        self.assertNotIn('pending_operation', state['milestones']['drawing'])
        self.assertTrue(all(c[0] == 'GET' for c in self.paypal.calls[before:]))
        self.assertEqual(sum(c[0] == 'POST' and c[1].endswith('/capture') for c in self.paypal.calls), 1)

    def test_milestone_keys_and_amounts_are_isolated(self):
        plan = deepcopy(PLAN)
        plan['total'] = '19.00'
        plan['milestones'].append({**plan['milestones'][0], 'id': 'source', 'amount': '11.00'})
        version = self.flow.save_plan(plan)['version']
        a = self.flow.propose(version, 'drawing', 'authorize')
        b = self.flow.propose(version, 'source', 'authorize')
        self.assertNotEqual(a['request_id'], b['request_id'])
        self.assertEqual(a['arguments']['amount'], '8.00')
        self.assertEqual(b['arguments']['amount'], '11.00')
        token = self.flow.sign(a['proposal_id'], 'Test', True)['signature']
        with self.assertRaises(PermissionError):
            self.flow.signatures.consume(b['proposal_id'], self.flow.current_proposal(b['proposal_id']), token)

    def test_adapter_rejects_excess_refund_before_any_paypal_request(self):
        record = {'amount': {'value': '8.00', 'currency_code': 'EUR'}, 'refunded': '7.00'}
        with self.assertRaises(ValueError):
            self.flow.lifecycle.refund(record, '1.01', 'unused')
        self.assertEqual(self.paypal.calls, [])

    def test_missing_credentials_does_not_lock_unfunded_terms(self):
        with patch.object(FakeLifecyclePayPal, 'configured', False):
            with self.assertRaises(ValueError):
                self.act('authorize')
        self.assertEqual(self.paypal.calls, [])
        self.assertNotIn('pending_operation', self.flow.state()['milestones']['drawing'])
        self.flow.save_plan(deepcopy(PLAN))

    def test_real_adapter_accepts_empty_void_204_without_json_failure(self):
        paypal = PayPal(Path(self.temp.name) / 'adapter.sqlite3')
        with patch.dict(os.environ, {'PAYPAL_CLIENT_ID': 'synthetic-id', 'PAYPAL_CLIENT_SECRET': 'synthetic-secret'}), patch('paypal.urllib.request.urlopen') as mocked:
            mocked.return_value.__enter__.return_value.read.side_effect = [b'{"access_token":"synthetic-token"}', b'']
            result = paypal.call('POST', '/v2/payments/authorizations/TESTAUTH123/void', {}, 'test-request-id')
            self.assertEqual(result, {})
            request = mocked.call_args.args[0]
            self.assertTrue(request.full_url.startswith('https://api-m.sandbox.paypal.com/'))
            self.assertEqual(request.get_header('Paypal-request-id'), 'test-request-id')
