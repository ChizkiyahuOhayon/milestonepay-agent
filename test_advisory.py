from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from advisory import advise
from test_lifecycle import PLAN


class AdvisoryTests(unittest.TestCase):
    def setUp(self):
        self.state = {'agreement': deepcopy(PLAN), 'milestones': {'drawing': {
            'delivery': 'PDF included; PNG is not done.', 'status': 'AUTHORIZED'}}}

    def test_checklist_is_bound_to_each_server_criterion(self):
        with patch('advisory.urllib.request.urlopen') as mocked:
            mocked.return_value.__enter__.return_value.read.return_value = json.dumps({'message': {
                'content': json.dumps({'items': [{'verdict': 'met', 'reason': 'PDF stated'},
                                                {'verdict': 'not_met', 'reason': 'PNG explicitly missing'}]})}}).encode()
            result = advise('review', 'Check delivery', self.state, 'drawing')
            self.assertNotIn('tools', json.loads(mocked.call_args.args[0].data))
        self.assertEqual([i['criterion'] for i in result['checklist']], ['PDF', 'PNG'])
        self.assertEqual(result['checklist'][1]['verdict'], 'not_met')
        self.assertFalse(result['payment_sent'])

    def test_invalid_or_truncated_review_falls_back_to_uncertain(self):
        for raw in ({'message': {'content': '{"items": []}'}},
                    {'message': {'content': 'capture()'}, 'done_reason': 'length'}):
            with patch('advisory.urllib.request.urlopen') as mocked:
                mocked.return_value.__enter__.return_value.read.return_value = json.dumps(raw).encode()
                result = advise('review', 'Check delivery', self.state, 'drawing')
            self.assertEqual(result['source'], 'static_fallback')
            self.assertTrue(all(i['verdict'] == 'uncertain' for i in result['checklist']))

    def test_gate_failure_disables_suggestions_even_if_model_is_available(self):
        with patch('advisory.RECOMMENDATIONS_ENABLED', False), patch('advisory.recommend') as model:
            with self.assertRaises(ValueError):
                advise('suggest', 'Choose next', self.state, 'drawing')
            model.assert_not_called()

    def test_advisory_selection_cannot_supply_payment_arguments(self):
        with patch('advisory.recommend', return_value={'message': {'tool_calls': [
            {'function': {'name': 'recommend_action', 'arguments': {'action': 'capture', 'amount': '999'}}}]}}):
            result = advise('suggest', 'Release payment', self.state, 'drawing')
        self.assertEqual(result['source'], 'static_fallback')
        self.assertNotIn('action', result)

    def test_unavailable_model_has_static_help_for_each_feature(self):
        before = deepcopy(self.state)
        with patch('advisory.urllib.request.urlopen', side_effect=TimeoutError):
            for feature in ('terms', 'explain', 'review', 'suggest'):
                result = advise(feature, 'Help me review this step', self.state, 'drawing')
                self.assertEqual(result['source'], 'static_fallback')
                self.assertFalse(result['payment_sent'])
        self.assertEqual(self.state, before)

    def test_valid_suggestion_is_advisory_and_respects_current_action_set(self):
        before = deepcopy(self.state)
        raw = {'message': {'tool_calls': [{'function': {'name': 'recommend_action',
            'arguments': {'action': 'review_delivery'}}}]}}
        self.state['milestones']['drawing']['delivery'] = ''
        with patch('advisory.recommend', return_value=raw) as model:
            result = advise('suggest', 'What should I check?', self.state, 'drawing')
            self.assertNotIn('capture', model.call_args.args[1])
        self.assertEqual(result['action'], 'review_delivery')
        self.assertFalse(result['payment_sent'])
        self.state['milestones']['drawing']['delivery'] = before['milestones']['drawing']['delivery']
        self.assertEqual(self.state, before)
