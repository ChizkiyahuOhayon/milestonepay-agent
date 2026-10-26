# Local tests

Run `python3 -m unittest discover -v`. Tests use Python's standard library,
synthetic fixtures and temporary SQLite databases. They need no credentials,
model server or third-party packages. Tests that exercise HTTP use loopback.

Payment-path assertions use deterministic fake adapters. They test request
binding, one-use permissions, stale plans, interrupted responses, refund bounds
and audit filtering. They do not establish real PayPal Sandbox acceptance.
Advisory tests use fake model responses and check structural validation and
fallback behavior, not semantic accuracy.

`test_server.py` also checks that fake adapters never report Sandbox credentials,
missing or partial credentials are labelled missing, and present credentials
are explicitly unverified without making a network request.

Optional developer check: `node --check app.js`. Node is not needed to run the app.
