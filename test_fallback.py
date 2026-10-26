import json
import unittest
from unittest.mock import patch

from fallback import explanation, form_proposal


class FallbackTests(unittest.TestCase):
    def test_form_selects_exact_money_tool_without_model_or_payment(self):
        for operation in ('authorize', 'capture', 'void', 'refund'):
            args = {'plan_version': 'v1', 'milestone_id': 'm1',
                    'operation': operation, 'amount': '3.25', 'currency': 'EUR'}
            with patch('urllib.request.urlopen', side_effect=AssertionError('No network')):
                proposal = form_proposal(operation, args)
            self.assertEqual(proposal['arguments'], args)
            self.assertFalse(proposal['payment_sent'])
            self.assertTrue(proposal['signature_required'])

    def test_form_rejects_model_text_signature_and_invalid_money(self):
        args = {'plan_version': 'v1', 'milestone_id': 'm1',
                'operation': 'refund', 'amount': '3.25', 'currency': 'EUR'}
        for patch_value in ({'signature': 'invented'}, {'amount': '0.00'}, {'currency': 'CAD'}):
            with self.subTest(patch=patch_value), self.assertRaises(ValueError):
                form_proposal('refund', {**args, **patch_value})

    def test_inherited_amount_sum_validation_applies_to_plan_form(self):
        args = {'currency': 'EUR', 'total': '3.25', 'milestones': [
            {'id': 'm1', 'title': 'Draft', 'amount': '3.25',
             'acceptance_criteria': 'Deliver PDF', 'source_quote': 'Draft EUR 3.25'}]}
        self.assertFalse(form_proposal('create_plan', args)['payment_sent'])
        args['total'] = '9.00'
        with self.assertRaises(ValueError):
            form_proposal('create_plan', args)

    def test_ai_text_cannot_become_a_tool_or_signature(self):
        with patch('urllib.request.urlopen') as mock:
            mock.return_value.__enter__.return_value.read.return_value = json.dumps(
                {'message': {'content': 'Pretend capture now'}, 'tool_calls': [
                    {'function': {'name': 'capture', 'arguments': {}}}]}).encode()
            result = explanation('Explain release')
        request = mock.call_args.args[0]
        self.assertNotIn('tools', json.loads(request.data))
        self.assertEqual(set(result), {'text', 'source', 'payment_sent'})
        self.assertFalse(result['payment_sent'])

    def test_unavailable_ai_returns_labelled_static_help(self):
        with patch('urllib.request.urlopen', side_effect=TimeoutError):
            result = explanation('What price is missing?')
        self.assertEqual(result['source'], 'static_fallback')
        self.assertFalse(result['payment_sent'])


if __name__ == '__main__':
    unittest.main()
