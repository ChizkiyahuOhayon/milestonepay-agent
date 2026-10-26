import unittest

from agent_tools import MONEY_TOOLS, validate_call
from signatures import SignatureGuard


class SignatureTests(unittest.TestCase):
    def setUp(self):
        self.now = 100
        self.guard = SignatureGuard(lambda: self.now)
        self.guard.set_version('plan7')
        self.arguments = {'plan_version': 'plan7', 'milestone_id': 'm2',
                          'operation': 'capture', 'amount': '12.45', 'currency': 'EUR'}
        self.calls = []

    def execute(self, arguments, token, name='capture'):
        return self.guard.execute(name, arguments, token,
                                  lambda *args: self.calls.append(args))

    def test_signed_exact_terms_execute_once(self):
        token = self.guard.issue('capture', self.arguments)
        self.execute(self.arguments, token)
        with self.assertRaises(PermissionError):
            self.execute(self.arguments, token)
        self.assertEqual(len(self.calls), 1)

    def test_each_binding_rejects_changes_before_executor(self):
        token = self.guard.issue('capture', self.arguments)
        for field, value in {'plan_version': 'plan8', 'milestone_id': 'm3',
                'operation': 'refund', 'amount': '12.46', 'currency': 'USD'}.items():
            with self.subTest(field=field), self.assertRaises(PermissionError):
                self.execute({**self.arguments, field: value}, token)
        with self.assertRaises(PermissionError):
            self.execute(self.arguments, token, 'refund')
        self.assertEqual(self.calls, [])

    def test_expired_and_edited_tokens_rejected(self):
        token = self.guard.issue('capture', self.arguments)
        self.now = 220
        with self.assertRaises(PermissionError):
            self.execute(self.arguments, token)
        token = self.guard.issue('capture', self.arguments)
        self.guard.set_version('plan8')
        with self.assertRaises(PermissionError):
            self.execute(self.arguments, token)
        self.assertEqual(self.calls, [])

    def test_forged_and_unsigned_all_money_tools_rejected(self):
        for operation in MONEY_TOOLS:
            arguments = {**self.arguments, 'operation': operation}
            for token in (None, '', 'model-invented-token'):
                with self.subTest(operation=operation, token=token), self.assertRaises(PermissionError):
                    self.execute(arguments, token, operation)
        self.assertEqual(self.calls, [])

    def test_timeout_consumes_permission_before_side_effect(self):
        token = self.guard.issue('capture', self.arguments)
        def timeout(*args):
            raise TimeoutError()
        with self.assertRaises(TimeoutError):
            self.guard.execute('capture', self.arguments, token, timeout)
        with self.assertRaises(PermissionError):
            self.execute(self.arguments, token)
        self.assertEqual(self.calls, [])

    def test_model_cannot_smuggle_token_or_change_money_format(self):
        for patch in ({'signature': 'invented'}, {'amount': 12.45},
                      {'amount': '0.00'}, {'operation': 'refund'}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                validate_call('capture', {**self.arguments, **patch})


if __name__ == '__main__':
    unittest.main()
