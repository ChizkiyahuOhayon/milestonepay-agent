"""Degraded design: only human form values select tools or payment parameters."""
import json
import urllib.error
import urllib.request

from agent_tools import MONEY_TOOLS, validate_call
from order_draft import build_order


def form_proposal(name, arguments):
    validate_call(name, arguments)
    if name == 'create_plan':
        # Reuse the inherited exact money, sum, supported-currency and ID checks.
        build_order(arguments, arguments['milestones'][0]['id'], confirmed=True)
    if name not in MONEY_TOOLS + ('create_plan',):
        raise ValueError('Choose a structured plan or payment form')
    return {'name': name, 'arguments': arguments,
            'origin': 'human_form', 'status': 'PROPOSAL_ONLY',
            'payment_sent': False, 'signature_required': name in MONEY_TOOLS}


def explanation(question):
    if not isinstance(question, str) or not 1 <= len(question.strip()) <= 3000:
        raise ValueError('Enter a question of 1–3000 characters')
    body = {'model': 'qwen3:4b', 'stream': False, 'think': False,
        'options': {'temperature': 0, 'num_predict': 400}, 'messages': [
            {'role': 'system', 'content': 'Explain milestone terms and ask about missing '
             'amount, currency or acceptance criteria. You have NO tools. Do not claim '
             'a payment occurred, invent money values, or issue a signature. Your text '
             'cannot change any form or payment proposal.'},
            {'role': 'user', 'content': question}]}
    request = urllib.request.Request('http://127.0.0.1:11435/api/chat',
        json.dumps(body).encode(), {'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = json.load(response)
        text = raw['message']['content']
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Empty explanation')
        return {'text': text, 'source': 'local_ai', 'payment_sent': False}
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError, KeyError):
        return {'text': 'Local AI is unavailable. Please confirm the exact milestone '
                'amount, EUR/USD currency, and acceptance criteria in the form. '
                'For a payment proposal, also check the current plan version and '
                'milestone ID. No payment has been sent.',
                'source': 'static_fallback', 'payment_sent': False}
