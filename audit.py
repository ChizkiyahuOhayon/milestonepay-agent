"""Local ordered audit. Whitelist receipts; never store raw PayPal responses."""
from datetime import datetime, timezone
import json

FIELDS = {'version', 'source', 'milestone_id', 'amount', 'name', 'stage', 'revision',
    'request_id', 'arguments', 'plan_version', 'operation', 'currency',
    'proposal_id', 'terms', 'signer', 'error_type', 'status', 'authorization_id',
    'capture_id', 'order_id', 'refund_id', 'refunded', 'refund_amount',
    'remote_status', 'remote_authorization_status', 'remote_capture_status',
    'remote_payment_count', 'feature', 'action', 'value', 'currency_code', 'code'}
RESOURCE_IDS = {'order_id', 'authorization_id', 'capture_id', 'refund_id'}


def sanitize(value):
    if isinstance(value, dict):
        return {key: ('…' + str(item)[-4:] if key in RESOURCE_IDS else sanitize(item))
                for key, item in value.items() if key in FIELDS}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value


class Audit:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS audit (seq INTEGER PRIMARY KEY AUTOINCREMENT, utc TEXT, kind TEXT, data TEXT)')

    def append(self, kind, details):
        with self.store.connect() as db:
            db.execute('INSERT INTO audit(utc,kind,data) VALUES (?,?,?)',
                (datetime.now(timezone.utc).isoformat(), kind, json.dumps(sanitize(details))))

    def entries(self):
        with self.store.connect() as db:
            rows = db.execute('SELECT seq,utc,kind,data FROM audit ORDER BY seq').fetchall()
        return [{'seq': seq, 'utc': utc, 'kind': kind, 'details': json.loads(data)}
                for seq, utc, kind, data in rows]
