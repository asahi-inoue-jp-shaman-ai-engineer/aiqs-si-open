"""Explicit first-party provider boundary; not mounted by default or exposed to MCP."""
import hashlib
from urllib.parse import urlsplit

import httpx
from standardwebhooks.webhooks import Webhook


class SupabaseOnboardingProvider:
    def __init__(self, coordinator, *, issuer, publishable_key, hook_secret, client=None, transport=None):
        if client is not None and transport is not None:
            raise ValueError('Choose one upstream transport boundary')
        parsed = urlsplit(issuer)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment
                or parsed.path != '/auth/v1'):
            raise ValueError('Fixed upstream auth issuer required')
        if not publishable_key or not hook_secret:
            raise ValueError('Explicit provider configuration required')
        if coordinator.issuer != issuer:
            raise ValueError('Coordinator and provider issuer must match')
        self.coordinator = coordinator
        self.issuer = issuer
        self.publishable_key = publishable_key
        # Supabase displays versioned secrets; Standard Webhooks takes whsec_.
        secret = hook_secret.removeprefix('v1,')
        self.webhook = Webhook(secret)
        self.client = client
        self.transport = transport

    def capture(self, body, headers):
        if not isinstance(body, bytes) or len(body) > 65536:
            raise ValueError('Invalid hook body')
        event = self.webhook.verify(body, dict(headers))
        if not isinstance(event, dict):
            raise ValueError('Invalid hook event')
        # Digest only, never persist the payload (which can contain private data).
        return self.coordinator.capture_verified_creation(event, evidence_digest=hashlib.sha256(body).hexdigest())

    def finalize(self, bearer, expected_subject):
        if (not isinstance(bearer, str) or not 1 <= len(bearer) <= 16384
                or any(c.isspace() for c in bearer)):
            raise ValueError('Upstream session required')
        headers = {'Authorization': 'Bearer ' + bearer, 'apikey': self.publishable_key}
        def request(client):
            response = client.get(self.issuer + '/user', headers=headers,
                                  timeout=10, follow_redirects=False)
            if response.status_code != 200:
                raise ValueError('Upstream identity verification failed')
            user = response.json()
            if not isinstance(user, dict):
                raise ValueError('Invalid upstream user')
            return self.coordinator.finalize_verified_user(expected_subject, user)
        if self.client is not None:
            return request(self.client)
        with httpx.Client(trust_env=False, transport=self.transport) as client:
            return request(client)

    def routes(self):
        """Explicit opt-in server integration; no cookies/CORS/general DB access."""
        from starlette.concurrency import run_in_threadpool
        from starlette.responses import JSONResponse
        from starlette.routing import Route
        import json

        async def body_under_limit(request, maximum):
            result = bytearray()
            async for chunk in request.stream():
                result.extend(chunk)
                if len(result) > maximum:
                    raise ValueError('Request too large')
            return bytes(result)

        async def creation(request):
            try:
                for name in ('webhook-id', 'webhook-timestamp', 'webhook-signature'):
                    if len(request.headers.getlist(name)) != 1:
                        raise ValueError('Invalid signature headers')
                body = await body_under_limit(request, 65536)
                await run_in_threadpool(self.capture, body, request.headers)
                return JSONResponse({})
            except Exception:
                # Provider hook contract: explicit error prevents SI-marked creation.
                # Never expose provider body, credentials or DB exception text.
                return JSONResponse({'error': {'http_code': 400, 'message': 'Registration could not be verified'}}, status_code=400)

        async def complete(request):
            try:
                authorization = request.headers.getlist('authorization')
                if len(authorization) != 1 or not authorization[0].startswith('Bearer '):
                    return JSONResponse({'error': 'upstream_session_required'}, status_code=401)
                data = json.loads(await body_under_limit(request, 1024))
                if not isinstance(data, dict) or set(data) != {'subject'}:
                    raise ValueError('Expected registration subject')
                await run_in_threadpool(self.finalize, authorization[0][7:], data['subject'])
                return JSONResponse({'registration_complete': True})
            except Exception:
                return JSONResponse({'error': 'registration_not_completed'}, status_code=400)

        return [Route('/onboarding/creation', creation, methods=['POST']),
                Route('/onboarding/complete', complete, methods=['POST'])]
