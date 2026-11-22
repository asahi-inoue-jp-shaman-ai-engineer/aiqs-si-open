"""Source-derived negative compatibility fixtures, NOT live provider tokens."""
from aiqs_si.auth import create_mcp_http_app
import asyncio
import time
import unittest
import json
import httpx
import jwt
from starlette.testclient import TestClient
from aiqs_si.auth import create_http_server
from aiqs_si.jwks import JWKSVerifier
from aiqs_si.store import Store
from pathlib import Path
import tempfile
from cryptography.hazmat.primitives.asymmetric import ec
from aiqs_si.auth import JWTVerifier


class SupabaseContractTests(unittest.TestCase):
    def setUp(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.issuer = 'https://synthetic.supabase.co/auth/v1'
        self.audience = 'https://si.example.test/mcp'
        self.verifier = JWTVerifier(public_key=self.key.public_key(), issuer=self.issuer,
                                    audience=self.audience, signing_algorithm='ES256')

    def accepted(self, *, typ='JWT', **changes):
        claims = {'iss': self.issuer, 'aud': 'authenticated', 'sub': 'synthetic-user',
                  'client_id': 'synthetic-client', 'scope': 'openid', 'role': 'authenticated',
                  'session_id': 'synthetic-session', 'iat': int(time.time()),
                  'exp': int(time.time()) + 300}
        claims.update(changes)
        token = jwt.encode(claims, self.key, algorithm='ES256', headers={'typ': typ})
        return asyncio.run(self.verifier.verify_token(token)) is not None

    def test_standard_provider_shape_is_not_compatible(self):
        self.assertFalse(self.accepted())

    def test_audience_and_scope_claim_changes_do_not_fix_header_contract(self):
        self.assertFalse(self.accepted(aud=self.audience, scope='openid aiqs:use'))

    def test_fixing_type_alone_does_not_allow_legacy_audience_or_oidc_only_scope(self):
        self.assertFalse(self.accepted(typ='at+jwt', scope='aiqs:use'))
        self.assertFalse(self.accepted(typ='at+jwt', aud=self.audience))

    def test_all_current_contract_requirements_are_needed(self):
        self.assertTrue(self.accepted(typ='at+jwt', aud=self.audience, scope='openid aiqs:use'))
        self.assertFalse(self.accepted(typ='at+jwt', aud='synthetic-client', scope='openid aiqs:use'))


class SupabaseProfileTests(unittest.TestCase):
    client_id = '11111111-1111-4111-8111-111111111111'
    subject = '22222222-2222-4222-8222-222222222222'
    session_id = '33333333-3333-4333-8333-333333333333'

    def setUp(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.options = dict(issuer='https://synthetic.supabase.co/auth/v1',
                            audience='https://si.example.test/mcp', signing_algorithm='ES256',
                            supabase_clients=[self.client_id])
        self.verifier = JWTVerifier(public_key=self.key.public_key(), **self.options)

    def token(self, *, typ='JWT', **changes):
        claims = dict(iss=self.options['issuer'], aud=self.options['audience'],
                      sub=self.subject, client_id=self.client_id, session_id=self.session_id,
                      role='authenticated', is_anonymous=False, scope='openid aiqs:use',
                      iat=int(time.time()), exp=int(time.time()) + 300)
        claims.update(changes)
        return jwt.encode(claims, self.key, algorithm='ES256', headers={'typ': typ, 'kid': 'test'})

    def test_explicit_profile_checks_access_token_and_si_permission(self):
        self.assertIsNotNone(asyncio.run(self.verifier.verify_token(self.token())))
        for change in [dict(aud='authenticated'), dict(aud=self.client_id),
                       dict(aud=[self.options['audience'], 'authenticated']),
                       dict(client_id='44444444-4444-4444-8444-444444444444'),
                       dict(client_id=None), dict(scope='openid'), dict(scope='aiqs:use'),
                       dict(session_id=None), dict(sub=''), dict(role='service_role'),
                       dict(is_anonymous=True), dict(is_anonymous=None), dict(typ='at+jwt'),
                       dict(exp=0), dict(iss='https://other.test')]:
            with self.subTest(change=change):
                self.assertIsNone(asyncio.run(self.verifier.verify_token(self.token(**change))))

    def test_invalid_profile_configuration_rejected(self):
        for change in [dict(signing_algorithm='RS256'), dict(required_scope='aiqs:worker'),
                       dict(supabase_clients=[]), dict(supabase_clients=['arbitrary'])]:
            with self.assertRaises(ValueError):
                JWTVerifier(public_key=self.key.public_key(), **{**self.options, **change})

    def test_jwks_profile_is_explicit_and_generic_default_stays_strict(self):
        jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(self.key.public_key()))
        jwk.update(kid='test', alg='ES256', use='sig')
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={'keys': [jwk]}))
        options = dict(jwks_url='https://synthetic.supabase.co/auth/v1/.well-known/jwks.json', transport=transport)
        verifier = JWKSVerifier(**options, **self.options)
        self.assertIsNotNone(asyncio.run(verifier.verify_token(self.token())))
        self.assertIsNone(asyncio.run(verifier.verify_token(self.token(scope='openid'))))
        generic = JWKSVerifier(**options, **{k: v for k, v in self.options.items() if k != 'supabase_clients'})
        self.assertIsNone(asyncio.run(generic.verify_token(self.token())))

    def test_metadata_requests_supported_oidc_scope_but_mcp_requires_si_claim(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'test.sqlite')
            app = create_mcp_http_app(create_http_server(store, self.verifier, embedded_test_worker=False))
            with TestClient(app, base_url='https://si.example.test') as client:
                metadata = client.get('/.well-known/oauth-protected-resource/mcp').json()
                self.assertEqual(metadata['scopes_supported'], ['openid'])
                request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                           'params': {'name': 'credit_balance', 'arguments': {}}}
                headers = {'Accept': 'application/json, text/event-stream'}
                denied = client.post('/mcp', json=request, headers={**headers, 'Authorization': 'Bearer ' + self.token(scope='openid')})
                self.assertEqual(denied.status_code, 401)
                ok = client.post('/mcp', json=request, headers={**headers, 'Authorization': 'Bearer ' + self.token()})
                self.assertEqual(ok.status_code, 200)
                self.assertEqual(ok.json()['result']['structuredContent']['available'], 0)
