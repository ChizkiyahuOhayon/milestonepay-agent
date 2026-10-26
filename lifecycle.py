"""Sandbox lifecycle adapter. The human workflow supplies all terms and keys."""
from decimal import Decimal
import re

from checkout import approval_url
from order_draft import build_order, money


def resource_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9]{5,64}', value):
        raise ValueError('Invalid PayPal resource ID')
    return value


def check_money(resource, expected):
    amount = resource.get('amount', {})
    if (amount.get('currency_code') != expected['currency_code']
            or Decimal(amount.get('value', '-1')) != Decimal(expected['value'])):
        raise ValueError('PayPal amount or currency requires reconciliation')


class Lifecycle:
    def __init__(self, paypal, origin):
        self.paypal, self.origin = paypal, origin

    def order(self, record):
        order = self.paypal.call('GET', '/v2/checkout/orders/' + resource_id(record['order_id']))
        units = order.get('purchase_units', [])
        if order.get('id') != record['order_id'] or order.get('intent') != 'AUTHORIZE' or len(units) != 1:
            raise ValueError('Unexpected PayPal order')
        if units[0].get('reference_id') != record['milestone_id']:
            raise ValueError('PayPal milestone differs from current terms')
        check_money(units[0], record['amount'])
        return order

    def create(self, plan, milestone, version, key):
        body = build_order(plan, milestone, confirmed=True)
        body['intent'] = 'AUTHORIZE'
        query = '?version=' + version + '&milestone=' + milestone
        body['payment_source'] = {'paypal': {'experience_context': {
            'return_url': self.origin + '/paypal/return' + query,
            'cancel_url': self.origin + '/paypal/cancel' + query,
            'shipping_preference': 'NO_SHIPPING', 'user_action': 'PAY_NOW'}}}
        result = self.paypal.create(key, body)
        record = {'order_id': result['id'], 'milestone_id': milestone,
                  'amount': body['purchase_units'][0]['amount']}
        remote = self.order(record)
        return {'status': 'AWAITING_APPROVAL', 'order_id': result['id'],
                'approval_url': approval_url(remote)}

    def authorize(self, record, key):
        order = self.order(record)
        authorizations = order['purchase_units'][0].get('payments', {}).get('authorizations', [])
        if not authorizations:
            if order.get('status') != 'APPROVED':
                raise ValueError('Approve the order with the Sandbox buyer first')
            self.paypal.call('POST', '/v2/checkout/orders/' + record['order_id'] + '/authorize', {}, key)
            order = self.order(record)
            authorizations = order['purchase_units'][0].get('payments', {}).get('authorizations', [])
        if len(authorizations) != 1 or authorizations[0].get('status') != 'CREATED':
            raise ValueError('Authorization is not active; reconciliation required')
        authorization = authorizations[0]
        check_money(authorization, record['amount'])
        return {'status': 'AUTHORIZED', 'authorization_id': resource_id(authorization['id'])}

    def capture(self, record, key):
        order = self.order(record)
        captures = order['purchase_units'][0].get('payments', {}).get('captures', [])
        if not captures:
            path = '/v2/payments/authorizations/' + resource_id(record['authorization_id'])
            authorization = self.paypal.call('GET', path)
            check_money(authorization, record['amount'])
            if authorization.get('status') != 'CREATED':
                raise ValueError('Authorization is no longer capturable; reconcile first')
            self.paypal.call('POST', path + '/capture',
                             {'amount': record['amount'], 'final_capture': True}, key)
            captures = self.order(record)['purchase_units'][0].get('payments', {}).get('captures', [])
        if len(captures) != 1 or captures[0].get('status') != 'COMPLETED':
            raise ValueError('Capture is not completed; reconcile before retrying')
        check_money(captures[0], record['amount'])
        return {'status': 'CAPTURED', 'capture_id': resource_id(captures[0]['id'])}

    def void(self, record, key):
        path = '/v2/payments/authorizations/' + resource_id(record['authorization_id'])
        authorization = self.paypal.call('GET', path)
        check_money(authorization, record['amount'])
        if authorization.get('status') == 'CREATED':
            self.paypal.call('POST', path + '/void', {}, key)
            authorization = self.paypal.call('GET', path)
            check_money(authorization, record['amount'])
        if authorization.get('status') != 'VOIDED':
            raise ValueError('Void is not confirmed; reconcile before retrying')
        return {'status': 'VOIDED', 'authorization_id': record['authorization_id']}

    def refund(self, record, amount, key):
        if money(amount) > Decimal(record['amount']['value']) - Decimal(record.get('refunded', '0.00')):
            raise ValueError('Refund exceeds captured balance')
        path = '/v2/payments/captures/' + resource_id(record['capture_id'])
        capture = self.paypal.call('GET', path)
        check_money(capture, record['amount'])
        if capture.get('status') not in ('COMPLETED', 'PARTIALLY_REFUNDED', 'REFUNDED'):
            raise ValueError('Capture is not refundable')
        requested = {'currency_code': record['amount']['currency_code'], 'value': amount}
        # Retry the identical persisted key/body if the prior response was lost.
        result = self.paypal.call('POST', path + '/refund', {'amount': requested}, key)
        check_money(result, requested)
        refund_id = resource_id(result['id'])
        if result.get('status') == 'PENDING':
            return {'status': 'REFUND_PENDING', 'refund_id': refund_id, 'refund_amount': amount}
        if result.get('status') != 'COMPLETED':
            raise ValueError('Refund is not completed; reconciliation required')
        return self.refund_receipt(record, amount, refund_id)

    @staticmethod
    def refund_receipt(record, amount, refund_id):
        refunded = Decimal(record.get('refunded', '0.00')) + Decimal(amount)
        if refunded > Decimal(record['amount']['value']):
            raise ValueError('Refund exceeds captured balance')
        return {'status': 'REFUNDED' if refunded == Decimal(record['amount']['value']) else 'PARTIALLY_REFUNDED',
                'refunded': str(refunded), 'refund_id': refund_id}

    def recheck(self, record):
        """Read-only remote queries. Never authorize/capture/void/refund here."""
        if record['status'] == 'REFUND_PENDING':
            result = self.paypal.call('GET', '/v2/payments/refunds/' + resource_id(record['refund_id']))
            check_money(result, {'currency_code': record['amount']['currency_code'],
                                 'value': record['refund_amount']})
            if result.get('status') == 'COMPLETED':
                return self.refund_receipt(record, record['refund_amount'], record['refund_id'])
            return {'status': 'REFUND_PENDING', 'remote_status': result.get('status')}
        order = self.order(record)
        result = {'remote_status': order.get('status')}
        payments = order['purchase_units'][0].get('payments', {})
        if record.get('authorization_id'):
            authorization = self.paypal.call('GET', '/v2/payments/authorizations/' + resource_id(record['authorization_id']))
            check_money(authorization, record['amount'])
            result['remote_authorization_status'] = authorization.get('status')
        if record.get('capture_id'):
            capture = self.paypal.call('GET', '/v2/payments/captures/' + resource_id(record['capture_id']))
            check_money(capture, record['amount'])
            result['remote_capture_status'] = capture.get('status')
        result['remote_payment_count'] = sum(len(payments.get(k, [])) for k in ('authorizations', 'captures'))
        return result
