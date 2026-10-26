from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from signatures import PersistentSignatures
from test_lifecycle import FakeLifecyclePayPal, PLAN
from workflow import Workflow


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paypal = FakeLifecyclePayPal(Path(self.temp.name) / 'state.sqlite3')
        self.now = 1000
        self.flow = Workflow(self.paypal, 'http://127.0.0.1:8765', lambda: self.now)
        self.state = self.flow.save_plan(deepcopy(PLAN))
        self.version = self.state['version']

    def tearDown(self):
        self.temp.cleanup()

    def proposal(self, operation):
        return self.flow.propose(self.version, 'drawing', operation)

    def act(self, operation):
        p = self.proposal(operation)
        signature = self.flow.sign(p['proposal_id'], 'Synthetic Buyer', True)['signature']
        self.state = self.flow.execute(p['proposal_id'], signature)
        return self.state['milestones']['drawing']

    def authorize(self):
        self.act('authorize')
        self.paypal.approve()
        self.act('authorize')

    def capture(self):
        self.authorize()
        self.flow.delivery(self.version, 'drawing', 'PDF and PNG are delivered')
        self.act('capture')

    def test_complete_signed_flow_and_partial_then_full_refunds(self):
        self.capture()
        self.flow.refund_amount(self.version, 'drawing', '1.25')
        self.assertEqual(self.act('refund')['refunded'], '1.25')
        self.assertEqual(self.act('refund')['status'], 'REFUNDED')
        self.assertEqual(self.flow.state()['milestones']['drawing']['refunded'], '8.00')

    def test_each_money_step_unsigned_and_forged_denied_without_network(self):
        for step in ('authorize', 'authorize', 'void'):
            p = self.proposal(step)
            before = len(self.paypal.calls)
            for token in (None, '', 'model-forged'):
                with self.assertRaises(PermissionError):
                    self.flow.execute(p['proposal_id'], token)
            self.assertEqual(len(self.paypal.calls), before)
            if self.flow.state()['milestones']['drawing']['status'] == 'AWAITING_APPROVAL':
                self.paypal.approve()
            self.act(step)
        self.assertEqual(self.flow.state()['milestones']['drawing']['status'], 'VOIDED')

    def test_edit_invalidates_old_signatures_and_return_versions(self):
        p = self.proposal('authorize')
        token = self.flow.sign(p['proposal_id'], 'Test', True)['signature']
        new = deepcopy(PLAN)
        new['milestones'][0]['acceptance_criteria'] += '\nEditable source'
        self.flow.save_plan(new)
        with self.assertRaises(PermissionError):
            self.flow.execute(p['proposal_id'], token)
        self.assertEqual(self.paypal.calls, [])

    def test_persistent_capture_and_refund_reject_unsigned_and_forged_tokens(self):
        self.authorize()
        self.flow.delivery(self.version, 'drawing', 'PDF and PNG are delivered')
        for operation in ('capture', 'refund'):
            p = self.proposal(operation)
            before = len(self.paypal.calls)
            for token in (None, '', 'forged'):
                with self.assertRaises(PermissionError):
                    self.flow.execute(p['proposal_id'], token)
            self.assertEqual(len(self.paypal.calls), before)
            self.act(operation)

    def test_permission_survives_restart_but_is_single_use_and_short_lived(self):
        p = self.proposal('authorize')
        token = self.flow.sign(p['proposal_id'], 'Test', True)['signature']
        self.flow = Workflow(self.paypal, 'http://127.0.0.1:8765', lambda: self.now)
        self.flow.execute(p['proposal_id'], token)
        with self.assertRaises((PermissionError, ValueError)):
            self.flow.execute(p['proposal_id'], token)
        p = self.proposal('authorize')
        token = self.flow.sign(p['proposal_id'], 'Test', True)['signature']
        self.now += 120
        before = len(self.paypal.calls)
        with self.assertRaises(PermissionError):
            self.flow.execute(p['proposal_id'], token)
        self.assertEqual(len(self.paypal.calls), before)

    def test_every_signature_field_is_bound_atomically(self):
        p = self.proposal('authorize')
        payload = self.flow.current_proposal(p['proposal_id'])
        token = self.flow.sign(p['proposal_id'], 'Test', True)['signature']
        for field, value in {'plan_version': 'other', 'milestone_id': 'other',
                             'operation': 'refund', 'amount': '7.00', 'currency': 'USD'}.items():
            other = deepcopy(payload)
            other['arguments'][field] = value
            with self.assertRaises(PermissionError):
                self.flow.signatures.consume(p['proposal_id'], other, token)
        self.flow.signatures.consume(p['proposal_id'], payload, token)
        with self.assertRaises(PermissionError):
            self.flow.signatures.consume(p['proposal_id'], payload, token)

    def test_lost_response_new_signature_same_persisted_request_id(self):
        self.capture()
        self.flow.refund_amount(self.version, 'drawing', '2.00')
        self.paypal.lose_response = '/refund'
        p = self.proposal('refund')
        token = self.flow.sign(p['proposal_id'], 'Test', True)['signature']
        with self.assertRaises(TimeoutError):
            self.flow.execute(p['proposal_id'], token)
        with self.assertRaises(PermissionError):
            self.flow.execute(p['proposal_id'], token)
        self.flow = Workflow(self.paypal, 'http://127.0.0.1:8765', lambda: self.now)
        with self.assertRaises(ValueError):
            self.flow.refund_amount(self.version, 'drawing', '3.00')
        self.assertEqual(self.proposal('refund')['request_id'], p['request_id'])
        self.assertEqual(self.act('refund')['refunded'], '2.00')
        self.assertEqual(len(self.paypal.refunds), 1)

    def test_late_unknown_operation_cannot_resend_after_idempotency_window(self):
        self.paypal.lose_response = '/v2/checkout/orders'
        with self.assertRaises(TimeoutError):
            self.act('authorize')
        self.now += 5 * 3600
        before = len(self.paypal.calls)
        with self.assertRaises(ValueError):
            self.act('authorize')
        self.assertEqual(len(self.paypal.calls), before)

    def test_locked_terms_and_refund_bounds(self):
        self.capture()
        with self.assertRaises(ValueError):
            self.flow.save_plan(deepcopy(PLAN))
        for amount in ('8.01', '0.00', '-1.00', 2.0):
            with self.assertRaises(ValueError):
                self.flow.refund_amount(self.version, 'drawing', amount)

    def test_delivery_or_refund_edit_invalidates_signatures(self):
        self.capture()
        p = self.proposal('refund')
        token = self.flow.sign(p['proposal_id'], 'Test', True)['signature']
        self.flow.refund_amount(self.version, 'drawing', '1.00')
        with self.assertRaises(PermissionError):
            self.flow.execute(p['proposal_id'], token)

    def test_pending_refund_completion_rechecks_without_reissuing(self):
        self.capture()
        self.paypal.pending_refund = True
        record = self.act('refund')
        self.assertEqual(record['status'], 'REFUND_PENDING')
        self.paypal.refunds[record['refund_id']]['status'] = 'COMPLETED'
        before = len(self.paypal.calls)
        self.flow.recheck(self.version, 'drawing')
        self.assertTrue(all(c[0] == 'GET' for c in self.paypal.calls[before:]))
        self.assertEqual(self.flow.state()['milestones']['drawing']['status'], 'REFUNDED')
