"""Single-process Phase 1 signature guard. No model-facing issuance endpoint."""
import secrets
import time
import hashlib
import json
import uuid

from agent_tools import MONEY_TOOLS, validate_call


class SignatureGuard:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.active_version = None
        self.tokens = {}

    def set_version(self, version):
        self.active_version = version
        self.tokens.clear()

    def issue(self, name, arguments):
        """Internal only; future human review UI must authorize issuance."""
        validate_call(name, arguments)
        if name not in MONEY_TOOLS or arguments['plan_version'] != self.active_version:
            raise ValueError('Only a current payment proposal can be signed')
        token = secrets.token_urlsafe(32)
        self.tokens[token] = (name, dict(arguments), self.clock() + 120)
        return token

    def execute(self, name, arguments, token, executor):
        # A missing, stale, mismatched, expired, or reused token cannot reach executor.
        if not isinstance(token, str) or token not in self.tokens:
            raise PermissionError('Human signature required')
        signed_name, signed_arguments, expires = self.tokens[token]
        if (self.clock() >= expires or name != signed_name
                or arguments != signed_arguments
                or arguments.get('plan_version') != self.active_version):
            raise PermissionError('Signature does not authorize these current terms')
        validate_call(name, arguments)
        del self.tokens[token]
        return executor(name, arguments)


class PersistentSignatures:
    """Single local human; hashes of permissions persist across process restarts."""
    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS proposals (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS permissions (hash TEXT PRIMARY KEY, proposal TEXT, payload TEXT, expires REAL, consumed INTEGER NOT NULL DEFAULT 0)')

    @staticmethod
    def encode(payload):
        return json.dumps(payload, sort_keys=True, separators=(',', ':'))

    def prepare(self, payload):
        ident = str(uuid.uuid4())
        with self.store.connect() as db:
            db.execute('INSERT INTO proposals VALUES (?,?)', (ident, self.encode(payload)))
        return {'proposal_id': ident, **payload}

    def load(self, ident):
        with self.store.connect() as db:
            row = db.execute('SELECT payload FROM proposals WHERE id=?', (ident,)).fetchone()
        if not row:
            raise ValueError('Unknown proposal')
        return json.loads(row[0])

    def issue(self, ident, current, signer, accepted):
        if accepted is not True or not isinstance(signer, str) or not 1 <= len(signer.strip()) <= 80:
            raise ValueError('Type your name and explicitly confirm the displayed terms')
        if self.load(ident) != current:
            raise PermissionError('Proposal changed; review current terms again')
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.store.connect() as db:
            db.execute('INSERT INTO permissions(hash,proposal,payload,expires) VALUES (?,?,?,?)',
                       (digest, ident, self.encode(current), self.clock() + 120))
        return token

    def consume(self, ident, current, token):
        if not isinstance(token, str) or not token:
            raise PermissionError('Human signature required')
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.store.connect() as db:
            # Atomic even if two requests try the same permission concurrently.
            changed = db.execute('UPDATE permissions SET consumed=1 WHERE hash=? AND proposal=? AND payload=? AND expires>? AND consumed=0',
                (digest, ident, self.encode(current), self.clock())).rowcount
            if changed != 1:
                raise PermissionError('Signature is missing, stale, expired, mismatched or already used')
