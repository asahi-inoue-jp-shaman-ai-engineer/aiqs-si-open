"""Public signing-key retrieval from an operator-configured HTTPS endpoint."""
import asyncio
import json
import time
from urllib.parse import urlsplit
import httpx
import jwt
from .auth import JWTVerifier


class JWKSVerifier:
    def __init__(self, *, jwks_url, issuer, audience, required_scope="aiqs:use", transport=None, clock=time.monotonic, signing_algorithm="RS256", supabase_clients=None, identity_issuer=None, broker_identity_issuer=None):
        url = urlsplit(jwks_url)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("Use an explicit public HTTPS JWKS URL")
        policy = JWTVerifier(public_key="", issuer=issuer, audience=audience, required_scope=required_scope, signing_algorithm=signing_algorithm, supabase_clients=supabase_clients, identity_issuer=identity_issuer, broker_identity_issuer=broker_identity_issuer)
        self.broker_identity_issuer = broker_identity_issuer
        self.identity_issuer = identity_issuer
        self.supabase_clients = policy.supabase_clients
        self.token_type = policy.token_type
        self.authorization_scopes = policy.authorization_scopes
        self.signing_algorithm = signing_algorithm
        self.url, self.issuer, self.audience = jwks_url, issuer, audience
        self.required_scope, self.transport, self.clock = required_scope, transport, clock
        self.keys, self.expires = {}, 0
        self.lock = asyncio.Lock()

    async def verify_token(self, token):
        refreshing = False
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if header.get("alg") != self.signing_algorithm or not isinstance(kid, str) or not 1 <= len(kid) <= 256:
                return None
            typ = header.get("typ")
            if not isinstance(typ, str) or typ.lower() != self.token_type:
                return None
            async with self.lock:
                if self.clock() >= self.expires:
                    refreshing = True
                    self.keys = {}  # Never retain expired keys after a refresh failure.
                    self.expires = self.clock() + 5  # Bound outage/invalid-key fetch retries.
                    async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False,
                                                 transport=self.transport) as client:
                        async with client.stream("GET", self.url) as response:
                            response.raise_for_status()
                            data = bytearray()
                            async for chunk in response.aiter_bytes():
                                data.extend(chunk)
                                if len(data) > 262144:
                                    raise ValueError("JWKS too large")
                    document = json.loads(data)
                    keys = document.get("keys")
                    if not isinstance(keys, list) or len(keys) > 64:
                        raise ValueError("Invalid JWKS")
                    selected = {}
                    for key in keys:
                        if not isinstance(key, dict):
                            raise ValueError("Invalid key")
                        if key.get("kty") != ("RSA" if self.signing_algorithm == "RS256" else "EC") or key.get("use", "sig") != "sig" or key.get("alg", self.signing_algorithm) != self.signing_algorithm:
                            continue
                        if self.signing_algorithm == "ES256" and key.get("crv") != "P-256":
                            continue
                        identifier = key.get("kid")
                        if not isinstance(identifier, str) or not identifier or identifier in selected or "d" in key:
                            raise ValueError("Invalid or duplicate signing key")
                        if "key_ops" in key:
                            if not isinstance(key["key_ops"], list):
                                raise ValueError("Invalid signing-key operations")
                            if "verify" not in key["key_ops"]:
                                continue
                        selected[identifier] = jwt.PyJWK(key, algorithm=self.signing_algorithm).key
                    self.keys, self.expires = selected, self.clock() + 60
                    refreshing = False
                key = self.keys.get(kid)
            if key is None:
                return None
            verifier = JWTVerifier(public_key=key, issuer=self.issuer, audience=self.audience,
                                   required_scope=self.required_scope, signing_algorithm=self.signing_algorithm,
                                   supabase_clients=self.supabase_clients, identity_issuer=self.identity_issuer,
                                   broker_identity_issuer=self.broker_identity_issuer)
            return await verifier.verify_token(token)
        except (httpx.HTTPError, jwt.PyJWTError, ValueError, TypeError, AttributeError):
            if refreshing:
                self.expires = self.clock() + 5
            return None
