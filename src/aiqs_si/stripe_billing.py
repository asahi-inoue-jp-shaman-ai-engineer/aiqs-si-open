"""SI Checkout adapter using the existing Stripe account, enabled only explicitly.

No secrets are loaded from legacy files; credentials are supplied by the host.
Legacy Checkout and signed-webhook flow is reused, not its account pricing rules.
"""
import hashlib
import hmac
import json
import time
import uuid
from urllib.parse import urlsplit
import httpx
from starlette.responses import JSONResponse
from starlette.routing import Route
from .pricing import purchase_quote
from .auth import resolve_account


class StripeBilling:
    def __init__(self, store, *, secret_key, webhook_secret, return_origin, live=False, client=None):
        origin = urlsplit(return_origin)
        if origin.scheme != 'https' or not origin.hostname or origin.username or origin.password or origin.query or origin.fragment or origin.path not in ('', '/'):
            raise ValueError('A fixed HTTPS return origin is required')
        if type(live) is not bool or not secret_key.startswith('sk_live_' if live else 'sk_test_') or not webhook_secret:
            raise ValueError('Explicit matching Stripe mode and credentials required')
        self.store, self.key, self.webhook_secret = store, secret_key, webhook_secret
        self.origin, self.live = return_origin.rstrip('/'), live
        self.client = client or httpx.Client(timeout=20, follow_redirects=False, trust_env=False)
        with store.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS stripe_orders (id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES accounts(id), request_key TEXT NOT NULL, yen INTEGER NOT NULL, live INTEGER NOT NULL, created_at REAL NOT NULL, UNIQUE(owner,request_key))')

    def checkout(self, owner, request_key, yen):
        quote = purchase_quote(yen)
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 128:
            raise ValueError('Purchase request key required')
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM stripe_orders WHERE owner=? AND request_key=?', (owner, request_key)).fetchone()
            if row:
                if row['yen'] != yen or bool(row['live']) != self.live:
                    raise ValueError('Purchase request key conflict')
                if time.time() - row['created_at'] > 23 * 3600:
                    raise ValueError('Purchase request expired; use a new request key')
                order_id = row['id']
            else:
                order_id = str(uuid.uuid4())
                db.execute('INSERT INTO stripe_orders VALUES (?,?,?,?,?,?)', (order_id, owner, request_key, yen, int(self.live), time.time()))
        form = {'mode': 'payment', 'success_url': self.origin + '/?billing=success',
                'cancel_url': self.origin + '/?billing=cancel', 'client_reference_id': order_id,
                'metadata[product]': 'aiqs-si', 'metadata[order_id]': order_id,
                'line_items[0][quantity]': '1', 'line_items[0][price_data][currency]': 'jpy',
                'line_items[0][price_data][unit_amount]': str(yen),
                'line_items[0][price_data][product_data][name]': 'Aiqs SI credits ' + str(quote['credits'])}
        try:
            response = self.client.post('https://api.stripe.com/v1/checkout/sessions', data=form,
                headers={'Authorization': 'Bearer ' + self.key, 'Idempotency-Key': 'si-' + order_id}, follow_redirects=False)
            if response.status_code != 200:
                raise ValueError('Stripe request rejected')
            session = response.json()
            parsed = urlsplit(session['url'])
            if parsed.scheme != 'https' or parsed.hostname != 'checkout.stripe.com' or parsed.username or parsed.password:
                raise ValueError('Invalid Checkout destination')
            if session.get('livemode') is not self.live:
                raise ValueError('Stripe mode mismatch')
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise ValueError('Checkout unavailable') from None
        return {**quote, 'order_id': order_id, 'url': session['url']}

    def purchase_status(self, owner, order_id):
        """Only a verified webhook receipt establishes credit delivery, never a redirect."""
        with self.store.transaction() as db:
            row = db.execute('SELECT yen,live FROM stripe_orders WHERE id=? AND owner=?', (order_id, owner)).fetchone()
            if row is None or bool(row['live']) != self.live:
                raise ValueError('Purchase unavailable')
            receipt = db.execute('SELECT credits FROM purchase_receipts WHERE payment_id=? AND owner=?',
                                 ('stripe-order:' + order_id, owner)).fetchone()
            balance = db.execute('SELECT balance FROM accounts WHERE id=?', (owner,)).fetchone()[0]
        return {'order_id': order_id, 'state': 'credited' if receipt else 'awaiting_payment_confirmation',
                'yen': row['yen'], 'granted_credits': receipt['credits'] if receipt else 0,
                'balance': balance, 'credit_kind': 'live' if self.live else 'test'}

    def webhook(self, raw, signature, *, now=None):
        if len(raw) > 1024 * 1024:
            raise ValueError('Webhook too large')
        parts = [part.split('=', 1) for part in signature.split(',') if '=' in part]
        timestamps = [value for key, value in parts if key == 't']
        if len(timestamps) != 1:
            raise ValueError('Invalid signature')
        timestamp = int(timestamps[0])
        if abs((time.time() if now is None else now) - timestamp) > 300:
            raise ValueError('Expired signature')
        expected = hmac.new(self.webhook_secret.encode(), timestamps[0].encode() + b'.' + raw, hashlib.sha256).hexdigest()
        if not any(hmac.compare_digest(expected, value) for key, value in parts if key == 'v1'):
            raise ValueError('Invalid signature')
        event = json.loads(raw)
        if event.get('livemode') is not self.live:
            raise ValueError('Stripe mode mismatch')
        if event.get('type') not in ('checkout.session.completed', 'checkout.session.async_payment_succeeded'):
            return {'received': True, 'processed': False}
        session = event['data']['object']
        metadata = session.get('metadata') or {}
        if metadata.get('product') != 'aiqs-si':
            return {'received': True, 'processed': False}
        if session.get('payment_status') != 'paid':
            return {'received': True, 'processed': False}
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM stripe_orders WHERE id=?', (metadata.get('order_id'),)).fetchone()
        if (row is None or session.get('mode') != 'payment' or session.get('livemode') is not self.live
                or bool(row['live']) != self.live or session.get('currency') != 'jpy'
                or type(session.get('amount_total')) is not int or session['amount_total'] != row['yen']
                or session.get('client_reference_id') != row['id']
                or not isinstance(session.get('id'), str) or not session['id'].startswith('cs_')):
            raise ValueError('Payment does not match SI order')
        self.store.record_verified_purchase(row['owner'], 'stripe-order:' + row['id'], row['yen'])
        return {'received': True, 'processed': True}

    def routes(self, verifier):
        async def checkout(request):
            header = request.headers.get('authorization', '')
            token = await verifier.verify_token(header[7:]) if header.startswith('Bearer ') else None
            if token is None:
                return JSONResponse({'error': 'unauthorized'}, status_code=401)
            try:
                body = await request.json()
                owner = resolve_account(self.store, token)
                import asyncio
                result = await asyncio.to_thread(self.checkout, owner, body['request_key'], body['yen'])
                return JSONResponse(result)
            except (ValueError, KeyError, TypeError):
                return JSONResponse({'error': 'checkout_unavailable'}, status_code=409)

        async def webhook(request):
            try:
                raw = bytearray()
                async for chunk in request.stream():
                    raw.extend(chunk)
                    if len(raw) > 1024 * 1024:
                        raise ValueError('Webhook too large')
                return JSONResponse(self.webhook(bytes(raw), request.headers.get('stripe-signature', '')))
            except (ValueError, KeyError, TypeError, AttributeError):
                return JSONResponse({'error': 'invalid_webhook'}, status_code=400)
        return [Route('/billing/checkout', checkout, methods=['POST']), Route('/billing/stripe/webhook', webhook, methods=['POST'])]
