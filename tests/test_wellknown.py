"""OAuth discovery compatibility routes: bare PRM path and RFC 8414 AS metadata."""
from pathlib import Path
import tempfile
import unittest

from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
from starlette.testclient import TestClient

from aiqs_si.application import create_application
from aiqs_si.auth import JWTVerifier
from aiqs_si.store import Store


ISSUER = "https://asi.example.test/realms/si"
AUDIENCE = "https://asi.example.test/mcp"
DISCOVERY = {
    "issuer": ISSUER,
    "authorization_endpoint": ISSUER + "/protocol/openid-connect/auth",
    "token_endpoint": ISSUER + "/protocol/openid-connect/token",
    "registration_endpoint": ISSUER + "/clients-registrations/openid-connect",
    "scopes_supported": ["openid", "aiqs:use"],
}
PRM = {"resource": AUDIENCE, "authorization_servers": [ISSUER],
       "scopes_supported": ["aiqs:use"], "bearer_methods_supported": ["header"]}


class WellKnownRoutesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name) / "wellknown.sqlite")
        self.discovery_calls = []

    def build_client(self, handler):
        def counting_handler(request):
            self.discovery_calls.append(str(request.url))
            return handler(request)
        user = JWTVerifier(public_key=self.key.public_key(), issuer=ISSUER,
                           audience=AUDIENCE)
        worker = JWTVerifier(public_key=self.key.public_key(), issuer=ISSUER,
                             audience="https://asi.example.test/worker",
                             required_scope="aiqs:worker")
        app = create_application(self.store, user, worker,
                                 allowed_workers={"synthetic-worker"},
                                 wellknown_transport=httpx.MockTransport(counting_handler))
        return TestClient(app, base_url="http://127.0.0.1:8000",
                          raise_server_exceptions=False)

    def ok_handler(self, request):
        return httpx.Response(200, json=DISCOVERY)

    def test_bare_protected_resource_metadata(self):
        client = self.build_client(self.ok_handler)
        response = client.get("/.well-known/oauth-protected-resource")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), PRM)

    def test_bare_matches_sdk_suffixed_document(self):
        client = self.build_client(self.ok_handler)
        bare = client.get("/.well-known/oauth-protected-resource")
        suffixed = client.get("/.well-known/oauth-protected-resource/mcp")
        self.assertEqual(suffixed.status_code, 200)
        self.assertEqual(bare.json(), suffixed.json())

    def test_authorization_server_metadata_path_insertion(self):
        client = self.build_client(self.ok_handler)
        response = client.get("/.well-known/oauth-authorization-server/realms/si")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), DISCOVERY)
        self.assertEqual(self.discovery_calls,
                         [ISSUER + "/.well-known/openid-configuration"])

    def test_authorization_server_metadata_is_cached(self):
        client = self.build_client(self.ok_handler)
        client.get("/.well-known/oauth-authorization-server/realms/si")
        client.get("/.well-known/oauth-authorization-server/realms/si")
        self.assertEqual(len(self.discovery_calls), 1)

    def test_authorization_server_metadata_upstream_failure(self):
        client = self.build_client(lambda request: httpx.Response(500))
        response = client.get("/.well-known/oauth-authorization-server/realms/si")
        self.assertEqual(response.status_code, 502)


if __name__ == "__main__":
    unittest.main()
