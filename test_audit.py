import json
from pathlib import Path
import tempfile
import unittest

from audit import Audit
from test_lifecycle import FakeLifecyclePayPal, PLAN
from workflow import Workflow


class AuditTests(unittest.TestCase):
    def test_persisted_chronology_links_human_signature_and_redacted_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            paypal = FakeLifecyclePayPal(Path(directory) / 'state.sqlite3')
            flow = Workflow(paypal, 'http://127.0.0.1:8765')
            state = flow.save_plan(PLAN)
            proposal = flow.propose(state['version'], 'drawing', 'authorize')
            token = flow.sign(proposal['proposal_id'], 'Synthetic Buyer', True)['signature']
            flow.execute(proposal['proposal_id'], token)
            entries = Audit(paypal).entries()
            self.assertEqual([e['kind'] for e in entries],
                ['plan_confirmed', 'human_proposal', 'human_signed', 'payment_attempt', 'paypal_response'])
            self.assertEqual(entries[-1]['details']['order_id'], '…2345')
            self.assertNotIn(token, json.dumps(entries))
            self.assertNotIn('approval_url', entries[-1]['details'])
            self.assertEqual([e['seq'] for e in entries], list(range(1, 6)))
            self.assertTrue(all(e['utc'].endswith('+00:00') for e in entries))

    def test_secret_and_payer_fields_never_enter_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            audit = Audit(FakeLifecyclePayPal(Path(directory) / 'state.sqlite3'))
            audit.append('test_receipt', {'status': 'COMPLETED', 'access_token': 'SECRET',
                'signature': 'TOKEN', 'payer': {'email_address': 'PRIVATE'},
                'arguments': {'amount': '8.00', 'client_secret': 'NESTED_SECRET'}})
            encoded = json.dumps(audit.entries())
            for private in ('SECRET', 'TOKEN', 'PRIVATE', 'client_secret'):
                self.assertNotIn(private, encoded)
            self.assertIn('8.00', encoded)

    def test_failures_preserve_attempt_and_uncertain_status(self):
        with tempfile.TemporaryDirectory() as directory:
            paypal = FakeLifecyclePayPal(Path(directory) / 'state.sqlite3')
            flow = Workflow(paypal, 'http://127.0.0.1:8765')
            state = flow.save_plan(PLAN)
            proposal = flow.propose(state['version'], 'drawing', 'authorize')
            with self.assertRaises(PermissionError):
                flow.execute(proposal['proposal_id'], 'forged')
            token = flow.sign(proposal['proposal_id'], 'Test', True)['signature']
            paypal.lose_response = '/v2/checkout/orders'
            with self.assertRaises(TimeoutError):
                flow.execute(proposal['proposal_id'], token)
            kinds = [e['kind'] for e in flow.audit.entries()]
            self.assertIn('signature_rejected', kinds)
            self.assertEqual(kinds[-2:], ['payment_attempt', 'payment_uncertain'])
