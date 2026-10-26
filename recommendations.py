"""Advisory action selection only. Never connected to payment execution."""
import json
from pathlib import Path
import urllib.request

CONFIG = json.loads(Path(__file__).with_name('recommendation-config.json').read_text())


def request_body(context, allowed):
    if not 2 <= len(allowed) <= 4 or len(set(allowed)) != len(allowed):
        raise ValueError('Expected two to four distinct allowed actions')
    schema = {'type': 'object', 'properties': {'action': {
        'type': 'string', 'enum': allowed}}, 'required': ['action'],
        'additionalProperties': False}
    return {'model': CONFIG['model'], 'stream': False, 'think': False,
        'keep_alive': '5m', 'options': CONFIG['options'], 'messages': [
            {'role': 'system', 'content': CONFIG['system']},
            {'role': 'user', 'content': json.dumps({'context': context,
                                                  'allowed_actions': allowed})}],
        'tools': [{'type': 'function', 'function': {'name': 'recommend_action',
            'description': 'Suggest one allowed next action for human review only.',
            'parameters': schema}}]}


def score(raw, allowed):
    calls = raw.get('message', {}).get('tool_calls', [])
    if len(calls) != 1 or raw.get('done_reason') == 'length':
        raise ValueError('Expected exactly one complete tool call')
    call = calls[0]['function']
    args = call['arguments']
    if (call['name'] != 'recommend_action' or not isinstance(args, dict)
            or set(args) != {'action'} or args['action'] not in allowed):
        raise ValueError('Invalid recommendation')
    return args['action']


def recommend(context, allowed):
    request = urllib.request.Request(CONFIG['endpoint'],
        json.dumps(request_body(context, allowed)).encode(),
        {'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=CONFIG['timeout_seconds']) as response:
        return json.load(response)
