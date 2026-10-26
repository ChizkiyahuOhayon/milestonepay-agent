"""Loopback-only single-user application. Run one process per local database."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import sys
import urllib.error
from urllib.parse import parse_qs, urlparse

from advisory import advise
from paypal import PayPal
from product_settings import RECOMMENDATIONS_ENABLED
from workflow import Workflow

ROOT = Path(__file__).resolve().parent


def application(port=8765, paypal=None):
    server = HTTPServer(('127.0.0.1', port), Handler)
    server.csrf = secrets.token_urlsafe(32)
    server.flow = Workflow(paypal or PayPal(ROOT / '.local/workflow.sqlite3'),
                           'http://127.0.0.1:' + str(server.server_port))
    return server


class Handler(BaseHTTPRequestHandler):
    def send(self, data, status=200, mime='application/json', location=None):
        body = json.dumps(data).encode() if mime == 'application/json' else data
        self.send_response(status)
        self.send_header('Content-Type', mime + '; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if location:
            self.send_header('Location', location)
        self.end_headers()
        self.wfile.write(body)

    def trusted(self):
        origin = 'http://127.0.0.1:' + str(self.server.server_port)
        return (self.headers.get('Host') == origin.removeprefix('http://')
                and self.headers.get('Origin') in (None, origin))

    def do_GET(self):
        if not self.trusted():
            return self.send({'error': 'Local host required'}, 403)
        flow = self.server.flow
        if self.path == '/api/state':
            real_adapter = type(flow.paypal) is PayPal
            configured = real_adapter and flow.paypal.configured
            payment_status = ('Fake payment service · no Sandbox connection' if not real_adapter else
                'Sandbox credentials present · connection unverified · lifecycle acceptance pending' if configured else
                'Sandbox credentials missing · confirm terms and use AI advice')
            return self.send({'csrf': self.server.csrf, 'state': flow.state(),
                'sandbox_configured': configured, 'payment_status': payment_status,
                'recommendations_enabled': RECOMMENDATIONS_ENABLED})
        if self.path == '/api/audit':
            return self.send(flow.audit.entries())
        url = urlparse(self.path)
        if url.path in ('/paypal/return', '/paypal/cancel'):
            try:
                query = parse_qs(url.query)
                state = flow.current(query['version'][0])
                record = state['milestones'][query['milestone'][0]]
                token = query.get('token', [''])[0]
                if not record.get('order_id') or (token != record['order_id'] and (token or url.path.endswith('return'))):
                    raise ValueError('Unknown checkout link')
                # Redirects never perform a money operation; a fresh signature is required.
                return self.send({}, 303, location='/?checkout=' + ('returned' if url.path.endswith('return') else 'cancelled'))
            except (ValueError, PermissionError, KeyError):
                return self.send({'error': 'Stale or unknown checkout link. Open the current plan.'}, 409)
        files = {'/': ('app.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'), '/style.css': ('style.css', 'text/css')}
        if (url.path in files and not url.query) or (url.path == '/' and url.query.startswith('checkout=')):
            name, mime = files[url.path]
            return self.send((ROOT / name).read_bytes(), mime=mime)
        self.send({'error': 'Not found'}, 404)

    def do_POST(self):
        if not self.trusted() or self.headers.get('X-MilestonePay-Token') != self.server.csrf:
            return self.send({'error': 'Local session check failed'}, 403)
        shapes = {'/api/plan': {'agreement'}, '/api/delivery': {'version', 'milestone', 'notes'},
            '/api/refund-amount': {'version', 'milestone', 'amount'}, '/api/proposal': {'version', 'milestone', 'operation'},
            '/api/sign': {'proposal_id', 'signer', 'accepted'}, '/api/execute': {'proposal_id', 'signature'},
            '/api/recheck': {'version', 'milestone'}, '/api/ai': {'feature', 'question', 'milestone', 'operation'}}
        if self.path not in shapes:
            return self.send({'error': 'Not found'}, 404)
        flow = self.server.flow
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 64000:
                raise ValueError('Invalid request size')
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict) or set(data) != shapes[self.path]:
                raise ValueError('Request fields do not match this human action')
            for key, value in data.items():
                if key not in ('agreement', 'accepted') and not isinstance(value, str):
                    if not (self.path == '/api/ai' and key == 'milestone' and value is None):
                        raise ValueError('Expected a text field: ' + key)
            if self.path == '/api/plan':
                result = flow.save_plan(data['agreement'])
            elif self.path == '/api/delivery':
                result = flow.delivery(data['version'], data['milestone'], data['notes'])
            elif self.path == '/api/refund-amount':
                result = flow.refund_amount(data['version'], data['milestone'], data['amount'])
            elif self.path == '/api/proposal':
                result = flow.propose(data['version'], data['milestone'], data['operation'])
            elif self.path == '/api/sign':
                result = flow.sign(data['proposal_id'], data['signer'], data['accepted'])
            elif self.path == '/api/execute':
                result = flow.execute(data['proposal_id'], data['signature'])
            elif self.path == '/api/recheck':
                result = flow.recheck(data['version'], data['milestone'])
            else:
                flow.event('ai_request', {'feature': data['feature'], 'milestone_id': data['milestone']})
                result = advise(data['feature'], data['question'], flow.state(), data['milestone'], data['operation'])
                flow.event('ai_response', {'feature': data['feature'], 'source': result['source'],
                    'name': 'recommend_action' if data['feature'] == 'suggest' else 'advisory_text',
                    'action': result.get('action')})
            self.send(result)
        except PermissionError as error:
            flow.event('request_rejected', {'code': 'SIGNATURE_REQUIRED'})
            self.send({'error': str(error), 'code': 'SIGNATURE_REQUIRED'}, 403)
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            self.send({'error': str(error)}, 400)
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            self.send({'error': 'Sandbox response uncertain. Recheck before signing a retry; the saved request ID will be reused.'}, 502)

    def log_message(self, *args):
        pass


def load_credentials():
    path = ROOT / '.env.local'
    if path.exists():
        for line in path.read_text().splitlines():
            key, separator, value = line.partition('=')
            if separator and key.strip() in ('PAYPAL_CLIENT_ID', 'PAYPAL_CLIENT_SECRET'):
                os.environ[key.strip()] = value.strip().strip('"\'')


if __name__ == '__main__':
    os.umask(0o077)
    load_credentials()
    with application(int(sys.argv[1]) if len(sys.argv) == 2 else 8765) as server:
        print('MilestonePay local Sandbox workspace: http://127.0.0.1:' + str(server.server_port), flush=True)
        server.serve_forever()
