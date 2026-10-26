from copy import deepcopy
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import application
from paypal import PayPal
from test_lifecycle import FakeLifecyclePayPal, PLAN


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paypal = FakeLifecyclePayPal(Path(self.temp.name) / 'state.sqlite3')
        self.server = application(0, self.paypal)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.csrf = self.request('GET', '/api/state')[1]['csrf']

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method, path, data=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        all_headers = {'Content-Type': 'application/json', 'X-MilestonePay-Token': getattr(self, 'csrf', '')}
        all_headers.update(headers or {})
        conn.request(method, path, json.dumps(data) if data is not None else None, all_headers)
        response = conn.getresponse()
        raw = response.read()
        body = json.loads(raw) if response.headers.get_content_type() == 'application/json' else raw.decode()
        result = response.status, body
        conn.close()
        return result

    def post(self, path, data):
        status, result = self.request('POST', path, data)
        self.assertEqual(status, 200, result)
        return result

    def plan(self):
        self.state = self.post('/api/plan', {'agreement': deepcopy(PLAN)})
        self.version = self.state['version']

    def act(self, operation):
        p = self.post('/api/proposal', {'version': self.version, 'milestone': 'drawing', 'operation': operation})
        signed = self.post('/api/sign', {'proposal_id': p['proposal_id'], 'signer': 'Synthetic Buyer', 'accepted': True})
        self.state = self.post('/api/execute', {'proposal_id': p['proposal_id'], 'signature': signed['signature']})
        return self.state['milestones']['drawing']

    def authorize(self):
        self.plan()
        self.act('authorize')
        self.paypal.approve()
        self.act('authorize')

    def capture(self):
        self.authorize()
        self.post('/api/delivery', {'version': self.version, 'milestone': 'drawing', 'notes': 'PDF and PNG delivered.'})
        self.act('capture')

    def test_http_complete_authorize_capture_partial_refund_paths(self):
        self.capture()
        self.post('/api/refund-amount', {'version': self.version, 'milestone': 'drawing', 'amount': '2.50'})
        self.assertEqual(self.act('refund')['status'], 'PARTIALLY_REFUNDED')
        self.assertEqual(self.act('refund')['status'], 'REFUNDED')
        entries = self.request('GET', '/api/audit')[1]
        self.assertTrue(any(e['kind'] == 'human_signed' for e in entries))
        self.assertTrue(any(e['kind'] == 'paypal_response' and e['details']['status'] == 'REFUNDED' for e in entries))

    def test_http_authorize_void(self):
        self.authorize()
        self.assertEqual(self.act('void')['status'], 'VOIDED')
        self.assertEqual(self.request('POST', '/api/proposal', {'version': self.version, 'milestone': 'drawing', 'operation': 'capture'})[0], 400)

    def test_unsigned_forged_and_amount_injection_cannot_reach_paypal(self):
        self.plan()
        p = self.post('/api/proposal', {'version': self.version, 'milestone': 'drawing', 'operation': 'authorize'})
        for token in ('', 'forged'):
            self.assertEqual(self.request('POST', '/api/execute', {'proposal_id': p['proposal_id'], 'signature': token})[0], 403)
        self.assertEqual(self.request('POST', '/api/proposal', {'version': self.version, 'milestone': 'drawing', 'operation': 'authorize', 'amount': '999.00'})[0], 400)
        self.assertEqual(self.paypal.calls, [])

    def test_host_origin_csrf_and_private_routes_rejected(self):
        for headers in ({'Host': 'evil.test'}, {'Origin': 'https://evil.test'}, {'X-MilestonePay-Token': 'bad'}):
            self.assertEqual(self.request('POST', '/api/plan', {'agreement': PLAN}, headers)[0], 403)
        for path in ('/.env.local', '/.local/workflow.sqlite3', '/workflow.py', '/%2e%2e/.env.local', '/api/tools'):
            self.assertEqual(self.request('GET', path)[0], 404)
        for path in ('/api/tools', '/api/form', '/api/capture'):
            self.assertEqual(self.request('POST', path, {})[0], 404)
        self.assertEqual(self.paypal.calls, [])

    def test_return_links_never_move_funds_and_stale_links_fail(self):
        self.plan()
        self.act('authorize')
        before = len(self.paypal.calls)
        path = '/paypal/return?version=' + self.version + '&milestone=drawing&token=ORDER12345'
        self.assertEqual(self.request('GET', path)[0], 303)
        self.assertEqual(self.request('GET', path.replace(self.version, 'obsolete'))[0], 409)
        self.assertEqual(len(self.paypal.calls), before)

    def test_unavailable_ai_keeps_all_four_operations_available(self):
        self.plan()
        with patch('advisory.urllib.request.urlopen', side_effect=TimeoutError):
            result = self.post('/api/ai', {'feature': 'explain', 'question': 'What does authorization do?', 'milestone': 'drawing', 'operation': 'authorize'})
        self.assertEqual(result['source'], 'static_fallback')
        self.assertIn('AI unavailable', result['text'])
        self.assertEqual(self.act('authorize')['status'], 'AWAITING_APPROVAL')
        self.paypal.approve()
        self.act('authorize')
        self.post('/api/delivery', {'version': self.version, 'milestone': 'drawing', 'notes': 'PDF and PNG'})
        self.act('capture')
        self.assertEqual(self.act('refund')['status'], 'REFUNDED')

    def test_ai_tool_injection_is_only_display_text_and_audited(self):
        self.plan()
        with patch('advisory.urllib.request.urlopen') as mocked:
            mocked.return_value.__enter__.return_value.read.return_value = json.dumps({'message': {
                'content': json.dumps({'text': 'capture now'}), 'tool_calls': [{'function': {'name': 'capture', 'arguments': {}}}]}}).encode()
            before = self.server.flow.state()
            result = self.post('/api/ai', {'feature': 'terms', 'question': 'Explain these terms', 'milestone': 'drawing', 'operation': 'authorize'})
            self.assertNotIn('tools', json.loads(mocked.call_args.args[0].data))
        self.assertFalse(result['payment_sent'])
        self.assertEqual(result['source'], 'local_ai')
        self.assertEqual(result['text'], 'capture now')
        self.assertEqual(self.server.flow.state(), before)
        self.assertEqual(self.paypal.calls, [])
        self.assertEqual([e['kind'] for e in self.request('GET', '/api/audit')[1]][-2:], ['ai_request', 'ai_response'])

    def test_sign_requires_explicit_human_acceptance(self):
        self.plan()
        p = self.post('/api/proposal', {'version': self.version, 'milestone': 'drawing', 'operation': 'authorize'})
        for name, accepted in [('', True), ('Buyer', False), ('Buyer', 'true')]:
            self.assertEqual(self.request('POST', '/api/sign', {'proposal_id': p['proposal_id'], 'signer': name, 'accepted': accepted})[0], 400)
        self.assertEqual(self.paypal.calls, [])

    def test_static_ui_available_without_model_or_credentials(self):
        for path, expected in [('/', 'Project assistant'), ('/app.js', 'textContent'), ('/style.css', '@media')]:
            status, body = self.request('GET', path)
            self.assertEqual(status, 200)
            self.assertIn(expected, body)

    def test_fake_adapter_never_claims_sandbox_credentials(self):
        with patch.dict(os.environ, {'PAYPAL_CLIENT_ID': 'test-id', 'PAYPAL_CLIENT_SECRET': 'test-secret'}):
            result = self.request('GET', '/api/state')[1]
        self.assertFalse(result['sandbox_configured'])
        self.assertEqual(result['payment_status'], 'Fake payment service · no Sandbox connection')
        self.assertEqual(self.paypal.calls, [])

    def test_missing_or_partial_credentials_are_labelled_missing(self):
        real = PayPal(self.paypal.database)
        with patch.object(self.server.flow, 'paypal', real):
            for values in ({}, {'PAYPAL_CLIENT_ID': 'test-id'}, {'PAYPAL_CLIENT_SECRET': 'test-secret'}):
                with self.subTest(keys=list(values)), patch.dict(os.environ, values, clear=True):
                    result = self.request('GET', '/api/state')[1]
                    self.assertFalse(result['sandbox_configured'])
                    self.assertEqual(result['payment_status'], 'Sandbox credentials missing · confirm terms and use AI advice')

    def test_present_credentials_do_not_claim_connection_or_acceptance(self):
        real = PayPal(self.paypal.database)
        with patch.object(self.server.flow, 'paypal', real), patch.dict(os.environ, {
                'PAYPAL_CLIENT_ID': 'test-id', 'PAYPAL_CLIENT_SECRET': 'test-secret'}, clear=True), patch('paypal.urllib.request.urlopen') as network:
            result = self.request('GET', '/api/state')[1]
            self.assertTrue(result['sandbox_configured'])
            self.assertEqual(result['payment_status'], 'Sandbox credentials present · check individual operation receipts')
            network.assert_not_called()
