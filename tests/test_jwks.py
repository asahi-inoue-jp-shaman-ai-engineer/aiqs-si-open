import time
import unittest
import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa, ec
from aiqs_si.jwks import JWKSVerifier


class JWKSTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_es256_preserves_access_token_contract(self):
        key = ec.generate_private_key(ec.SECP256R1())
        public = jwt.algorithms.ECAlgorithm.to_jwk(key.public_key(), as_dict=True)
        document = {"keys": [{**public, "kid": "ec", "alg": "ES256"}]}
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json=document))
        options = dict(jwks_url="https://issuer.test/keys", issuer="https://issuer.test",
                       audience="https://service.test/mcp", transport=transport)
        verifier = JWKSVerifier(**options, signing_algorithm="ES256")
        def token(typ="at+jwt", **overrides):
            claims = dict(iss="https://issuer.test", aud="https://service.test/mcp", sub="user",
                client_id="client", scope="aiqs:use", iat=int(time.time()), exp=int(time.time())+600)
            claims.update(overrides)
            return jwt.encode(claims, key, algorithm="ES256", headers={"kid": "ec", "typ": typ})
        self.assertIsNotNone(await verifier.verify_token(token()))
        self.assertIsNone(await JWKSVerifier(**options).verify_token(token()))
        for invalid in (token(typ="JWT"), token(aud="authenticated"), token(scope="openid"),
                        token(client_id=""), token(exp=1)):
            self.assertIsNone(await verifier.verify_token(invalid))
        wrong = ec.generate_private_key(ec.SECP256R1())
        claims = jwt.decode(token(), options={"verify_signature": False})
        self.assertIsNone(await verifier.verify_token(jwt.encode(claims, wrong, algorithm="ES256",
            headers={"kid": "ec", "typ": "at+jwt"})))
        with self.assertRaises(ValueError):
            JWKSVerifier(**options, signing_algorithm="HS256")

    async def test_rotation_expiry_and_untrusted_key_url(self):
        keys = [rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2)]
        now = [0]
        active = [0]
        requests = []
        def respond(request):
            requests.append(str(request.url))
            if active[0] == -1:
                return httpx.Response(503)
            key = jwt.algorithms.RSAAlgorithm.to_jwk(keys[active[0]].public_key(), as_dict=True)
            return httpx.Response(200, json={"keys": [{**key, "kid": str(active[0]), "alg": "RS256"}]})
        def token(index):
            return jwt.encode(dict(iss="https://issuer.test", aud="https://service.test/mcp",
                sub="test", client_id="test", scope="aiqs:use", iat=int(time.time()), exp=int(time.time())+600),
                keys[index], algorithm="RS256", headers={"kid": str(index), "typ": "at+jwt",
                                                         "jku": "https://untrusted.test/keys"})
        verifier = JWKSVerifier(jwks_url="https://issuer.test/keys", issuer="https://issuer.test",
            audience="https://service.test/mcp", transport=httpx.MockTransport(respond), clock=lambda: now[0])
        self.assertIsNotNone(await verifier.verify_token(token(0)))
        self.assertIsNone(await verifier.verify_token(token(1)))
        self.assertEqual(len(requests), 1)
        active[0], now[0] = 1, 61
        self.assertIsNotNone(await verifier.verify_token(token(1)))
        self.assertIsNone(await verifier.verify_token(token(0)))
        active[0], now[0] = -1, 122
        self.assertIsNone(await verifier.verify_token(token(1)))
        self.assertIsNone(await verifier.verify_token(token(1)))
        self.assertEqual(requests, ["https://issuer.test/keys"] * 3)

    def test_endpoint_must_be_configured_https_without_credentials(self):
        for url in ("http://issuer.test/keys", "https://user:password@issuer.test/keys", "https://issuer.test/keys?token=test"):
            with self.assertRaises(ValueError):
                JWKSVerifier(jwks_url=url, issuer="https://issuer.test", audience="https://service.test/mcp")

    async def test_slow_failure_backoff_starts_at_failure(self):
        now, calls = [100], []
        def fail(request):
            calls.append(now[0])
            now[0] += 6
            return httpx.Response(503)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = jwt.encode({}, key, algorithm="RS256", headers={"typ": "at+jwt", "kid": "test"})
        verifier = JWKSVerifier(jwks_url="https://issuer.test/keys", issuer="https://issuer.test",
            audience="https://service.test/mcp", transport=httpx.MockTransport(fail), clock=lambda: now[0])
        self.assertIsNone(await verifier.verify_token(token))
        self.assertIsNone(await verifier.verify_token(token))
        self.assertEqual(calls, [100])
        now[0] = 111
        self.assertIsNone(await verifier.verify_token(token))
        self.assertEqual(calls, [100, 111])
