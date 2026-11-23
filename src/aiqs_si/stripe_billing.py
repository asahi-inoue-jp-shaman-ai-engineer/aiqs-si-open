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
            # Accepted-offer terms (batch 2b item 4): the credit
            # amount quoted at checkout is persisted with the order,
            # so settlement grants what the buyer accepted even if
            # the pack policy changes before the webhook lands.
            columns = [row[1] for row in db.execute('PRAGMA table_info(stripe_orders)')]
            if 'credits' not in columns:
                db.execute('ALTER TABLE stripe_orders ADD COLUMN credits INTEGER')
            # Settlement holds (Astra round-1 F5): orders persisted
            # before accepted terms were recorded carry NULL credits
            # and must NOT be settled at whatever the current quote
            # happens to be — the accepted price is unrecoverable
            # from the order alone, so the webhook parks the order
            # here for operator reconciliation instead of guessing.
            db.execute('CREATE TABLE IF NOT EXISTS stripe_settlement_holds (order_id TEXT PRIMARY KEY, owner TEXT NOT NULL, yen INTEGER NOT NULL, session_id TEXT, created_at REAL NOT NULL)')

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
                if row['credits'] is not None:
                    # The accepted terms are the order's, not today's.
                    quote = {'yen': yen, 'credits': row['credits'], 'currency': 'JPY'}
            else:
                order_id = str(uuid.uuid4())
                db.execute('INSERT INTO stripe_orders VALUES (?,?,?,?,?,?,?)', (order_id, owner, request_key, yen, int(self.live), time.time(), quote['credits']))
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
        # Settle against the order's accepted terms (batch 2b item
        # 4). Orders persisted before terms were recorded carry
        # NULL: their accepted price cannot be reconstructed from
        # the order, so they are HELD for reconciliation (Astra
        # round-1 F5) — settling them at the current quote would
        # silently re-price a completed purchase.
        accepted = row['credits'] if 'credits' in row.keys() else None
        if accepted is None:
            # Hold — unless the order is ALREADY settled (Astra
            # round-2 N2): a duplicate webhook after operator
            # reconciliation must not resurrect a fulfilled order
            # as unresolved. The receipt check and the hold insert
            # are one transaction, so the two cannot race apart.
            with self.store.transaction() as db:
                receipt = db.execute(
                    'SELECT 1 FROM purchase_receipts WHERE payment_id=?',
                    ('stripe-order:' + row['id'],)).fetchone()
                if receipt is None:
                    db.execute('INSERT OR IGNORE INTO stripe_settlement_holds VALUES (?,?,?,?,?)',
                               (row['id'], row['owner'], row['yen'], session.get('id'), time.time()))
            if receipt is not None:
                return {'received': True, 'processed': True}
            return {'received': True, 'processed': False, 'held': 'terms_unrecorded'}
        self.store.record_verified_purchase(row['owner'], 'stripe-order:' + row['id'],
                                            row['yen'], accepted_credits=accepted)
        return {'received': True, 'processed': True}

    def settlement_holds(self):
        """Orders whose accepted terms were never recorded (Astra
        round-1 F5): paid, verified, and deliberately unsettled
        until an operator supplies the accepted terms."""
        with self.store.transaction() as db:
            rows = db.execute('SELECT * FROM stripe_settlement_holds ORDER BY created_at').fetchall()
            return [{'order_id': row['order_id'], 'owner': row['owner'],
                     'yen': row['yen'], 'session_id': row['session_id'],
                     'created_at': row['created_at']} for row in rows]

    def settle_held_order(self, order_id, accepted_credits):
        """Operator reconciliation for a held order: settle it with
        the accepted terms recovered from the original checkout
        records, exactly once (the stored receipt governs replays).

        The terms are required at this boundary (Astra round-2
        N5): a missing or malformed amount must never fall through
        to any current-price behavior — the hold stays untouched.
        The reconciled terms are persisted onto the order itself,
        so later webhook replays take the ordinary recorded-terms
        path instead of re-holding a fulfilled order (N2)."""
        if type(accepted_credits) is not int or accepted_credits < 1:
            raise ValueError('Accepted purchase terms required')
        with self.store.transaction() as db:
            hold = db.execute('SELECT * FROM stripe_settlement_holds WHERE order_id=?',
                              (order_id,)).fetchone()
            if hold is None:
                raise ValueError('No settlement hold for this order')
        result = self.store.record_verified_purchase(
            hold['owner'], 'stripe-order:' + order_id, hold['yen'],
            accepted_credits=accepted_credits)
        with self.store.transaction() as db:
            db.execute('UPDATE stripe_orders SET credits=? WHERE id=?',
                       (accepted_credits, order_id))
            db.execute('DELETE FROM stripe_settlement_holds WHERE order_id=?', (order_id,))
        return result

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
