"""Persistent human-driven state machine. No model output enters this module."""
from copy import deepcopy
from decimal import Decimal
import json
import time
import uuid

from fallback import form_proposal
from lifecycle import Lifecycle
from order_draft import money
from signatures import PersistentSignatures
from audit import Audit


class Workflow:
    def __init__(self, paypal, origin, clock=time.time):
        self.paypal, self.clock = paypal, clock
        self.lifecycle = Lifecycle(paypal, origin)
        self.signatures = PersistentSignatures(paypal, clock)
        self.audit = Audit(paypal)
        with paypal.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS workflow_plans (version TEXT PRIMARY KEY, data TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS workflow_current (id INTEGER PRIMARY KEY CHECK(id=1), version TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, payload TEXT, started REAL, result TEXT)')

    def event(self, kind, details):
        self.audit.append(kind, details)

    def state(self):
        with self.paypal.connect() as db:
            row = db.execute('SELECT data FROM workflow_plans JOIN workflow_current USING(version) WHERE id=1').fetchone()
        return json.loads(row[0]) if row else None

    def persist(self, state):
        with self.paypal.connect() as db:
            db.execute('INSERT OR REPLACE INTO workflow_plans VALUES (?,?)',
                       (state['version'], json.dumps(state)))
            db.execute('INSERT OR REPLACE INTO workflow_current VALUES (1,?)', (state['version'],))

    def current(self, version):
        state = self.state()
        if not state or version != state['version']:
            raise PermissionError('Plan version changed; review current terms')
        return state

    def save_plan(self, agreement):
        form_proposal('create_plan', agreement)
        for milestone in agreement['milestones']:
            if not 1 <= len(milestone['title'].strip()) <= 127 or not milestone['acceptance_criteria'].strip():
                raise ValueError('Every milestone needs a title and explicit acceptance criteria')
        old = self.state()
        if old and any(m['status'] != 'PLANNED' or m.get('pending_operation') for m in old['milestones'].values()):
            raise ValueError('Terms are locked after checkout begins; manage existing funds first')
        state = {'version': str(uuid.uuid4()), 'revision': 0, 'agreement': deepcopy(agreement),
                 'milestones': {m['id']: {'milestone_id': m['id'], 'status': 'PLANNED',
                    'amount': {'value': m['amount'], 'currency_code': agreement['currency']},
                    'refunded': '0.00', 'delivery': '', 'field_source': 'human_confirmed'}
                    for m in agreement['milestones']}}
        self.persist(state)
        self.event('plan_confirmed', {'version': state['version'], 'source': 'human_form'})
        return state

    @staticmethod
    def allowed(record):
        return {'PLANNED': ['authorize'], 'AWAITING_APPROVAL': ['authorize'],
                'AUTHORIZED': ['capture', 'void'], 'CAPTURED': ['refund'],
                'PARTIALLY_REFUNDED': ['refund']}.get(record['status'], [])

    def delivery(self, version, milestone, notes):
        state = self.current(version)
        record = state['milestones'][milestone]
        if record.get('pending_operation'):
            raise ValueError('Reconcile the pending operation first')
        if not isinstance(notes, str) or not 1 <= len(notes.strip()) <= 8000:
            raise ValueError('Enter a delivery note of 1–8000 characters')
        record['delivery'] = notes
        state['revision'] += 1
        self.persist(state)
        self.event('delivery_recorded', {'version': version, 'milestone_id': milestone})
        return state

    def refund_amount(self, version, milestone, amount):
        state = self.current(version)
        record = state['milestones'][milestone]
        if 'refund' not in self.allowed(record) or record.get('pending_operation'):
            raise ValueError('Refund amount cannot change in this state')
        if money(amount) > Decimal(record['amount']['value']) - Decimal(record['refunded']):
            raise ValueError('Refund exceeds the remaining captured balance')
        record['requested_refund'] = amount
        state['revision'] += 1
        self.persist(state)
        self.event('refund_amount_confirmed', {'version': version, 'milestone_id': milestone, 'amount': amount})
        return state

    def payload(self, version, milestone, operation):
        state = self.current(version)
        record = state['milestones'][milestone]
        if operation not in self.allowed(record):
            raise ValueError('Operation is not allowed in the current state')
        if operation == 'capture' and not record['delivery'].strip():
            raise ValueError('Record the delivery before accepting and releasing payment')
        amount = record['amount']['value']
        stage = 'create' if operation == 'authorize' and record['status'] == 'PLANNED' else operation
        if operation == 'refund':
            amount = record.get('requested_refund') or str(Decimal(amount) - Decimal(record['refunded']))
            if money(amount) > Decimal(record['amount']['value']) - Decimal(record['refunded']):
                raise ValueError('Refund exceeds remaining balance')
            stage += ':' + record['refunded'] + ':' + amount
        key = str(uuid.uuid5(uuid.UUID(version), milestone + ':' + stage))
        if record.get('pending_operation') not in (None, key):
            raise ValueError('Another operation needs reconciliation first')
        return {'name': operation, 'stage': stage, 'revision': state['revision'],
            'request_id': key, 'arguments': {'plan_version': version,
                'milestone_id': milestone, 'operation': operation, 'amount': amount,
                'currency': record['amount']['currency_code']}}

    def propose(self, version, milestone, operation):
        proposal = self.signatures.prepare(self.payload(version, milestone, operation))
        self.event('human_proposal', {k: v for k, v in proposal.items() if k != 'proposal_id'})
        return proposal

    def current_proposal(self, ident):
        saved = self.signatures.load(ident)
        args = saved['arguments']
        current = self.payload(args['plan_version'], args['milestone_id'], saved['name'])
        if current != saved:
            raise PermissionError('Proposal is stale; review and sign current terms')
        return current

    def sign(self, ident, signer, accepted):
        current = self.current_proposal(ident)
        token = self.signatures.issue(ident, current, signer, accepted)
        self.event('human_signed', {'proposal_id': ident, 'terms': current, 'signer': signer.strip()})
        return {'signature': token, 'expires_in_seconds': 120}

    def execute(self, ident, token):
        # Reject unsigned requests before looking up even a proposal.
        if not isinstance(token, str) or not token:
            raise PermissionError('Human signature required')
        current = self.current_proposal(ident)
        try:
            self.signatures.consume(ident, current, token)
        except PermissionError:
            self.event('signature_rejected', {'proposal_id': ident})
            raise
        if not self.paypal.configured:
            raise ValueError('Configure Sandbox credentials before sending a request')
        args = current['arguments']
        state = self.current(args['plan_version'])
        record = state['milestones'][args['milestone_id']]
        key = current['request_id']
        with self.paypal.connect() as db:
            db.execute('INSERT OR IGNORE INTO operations(id,payload,started) VALUES (?,?,?)',
                       (key, json.dumps(current, sort_keys=True), self.clock()))
            operation = db.execute('SELECT started,result FROM operations WHERE id=?', (key,)).fetchone()
        if operation[1]:
            patch = json.loads(operation[1])
        else:
            # Bound retries to below PayPal's shortest documented 6-hour key retention.
            if self.clock() - operation[0] >= 5 * 3600:
                raise ValueError('Retry window expired; reconcile with PayPal before further action')
            record['pending_operation'] = key
            self.persist(state)
            self.event('payment_attempt', {'terms': current})
            try:
                if current['stage'] == 'create':
                    patch = self.lifecycle.create(state['agreement'], args['milestone_id'], state['version'], key)
                elif current['name'] == 'refund':
                    patch = self.lifecycle.refund(record, args['amount'], key)
                else:
                    patch = getattr(self.lifecycle, current['name'])(record, key)
            except Exception as error:
                self.event('payment_uncertain', {'request_id': key, 'error_type': type(error).__name__})
                raise
            with self.paypal.connect() as db:
                db.execute('UPDATE operations SET result=? WHERE id=?', (json.dumps(patch), key))
        record.update(patch)
        record.pop('pending_operation', None)
        record.pop('requested_refund', None)
        state['revision'] += 1
        self.persist(state)
        self.event('paypal_response', {'request_id': key, 'milestone_id': args['milestone_id'], **patch})
        return state

    def recheck(self, version, milestone):
        state = self.current(version)
        record = state['milestones'][milestone]
        # Complete a local receipt saved just before an interrupted state update.
        if record.get('pending_operation'):
            with self.paypal.connect() as db:
                row = db.execute('SELECT result FROM operations WHERE id=?', (record['pending_operation'],)).fetchone()
            if row and row[0]:
                record.update(json.loads(row[0]))
                record.pop('pending_operation')
                state['revision'] += 1
        if not record.get('order_id'):
            raise ValueError('No known order yet. A newly signed retry reuses the saved request ID')
        patch = self.lifecycle.recheck(record)
        if patch.get('status') and patch['status'] != record['status']:
            state['revision'] += 1
        record.update(patch)
        self.persist(state)
        self.event('paypal_recheck', {'milestone_id': milestone, **patch})
        return state
