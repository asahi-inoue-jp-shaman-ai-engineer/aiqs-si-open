"""Batch 2g regressions (Astra batch2f diff review, R4 residual).

Batch 2e's policy-coherence comparison — the policies in effect
must equal a fresh load of the deployment environment — ran only
in create_application. The direct constructors (auth's
create_http_server and mcp_server's create_account_server) ran
the posture gate alone, so Astra's counterexample stood: import
pricing under one environment (its policy freezes at import),
then supply complete, valid, DIFFERENT policies and construct
the production MCP server directly with an injected service —
posture validates the new env, nothing compares it with the
cached policy, and pricing keeps using its import-time values.

These tests pin the counterexample against both direct
constructors, plus the coherent control that must still start.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _designated_store(case):
    from aiqs_si.deployment import designate_production
    from aiqs_si.store import Store
    folder = tempfile.TemporaryDirectory()
    case.addCleanup(folder.cleanup)
    store = Store(Path(folder.name) / "batch2g.sqlite")
    designate_production(store.path)
    return store


def _divergent_pricing_env():
    """An env whose pricing policy is complete and valid but
    differs from the import-time pricing snapshot — the stale
    cache Astra's counterexample relies on."""
    from aiqs_si import pricing
    held = pricing._POLICY.credits_per_shot_tenths
    other = held + 10 if held <= 90 else held - 10
    return {
        "AIQS_SI_ALLOW_DEFAULT_POLICIES": "1",
        "AIQS_SI_PRICING_POLICY_JSON":
            json.dumps({"credits_per_shot_tenths": other}),
    }


class DirectConstructorCoherenceTests(unittest.TestCase):
    def test_direct_mcp_constructor_rejects_stale_pricing(self):
        from aiqs_si.mcp_server import create_account_server
        store = _designated_store(self)
        with mock.patch.dict(os.environ, _divergent_pricing_env()):
            with self.assertRaises(ValueError) as caught:
                create_account_server(store, lambda: "owner")
        self.assertIn("diverges", str(caught.exception))
        self.assertIn("pricing", str(caught.exception))

    def test_direct_http_constructor_rejects_stale_pricing(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        from aiqs_si.auth import JWTVerifier, create_http_server
        key = rsa.generate_private_key(public_exponent=65537,
                                       key_size=2048)
        # A non-production issuer, so the issuer/synthetic
        # interlock does not fire first: the store's own durable
        # designation is the production signal under test.
        verifier = JWTVerifier(
            public_key=key.public_key(),
            issuer="https://issuer.example.test/auth/v1",
            audience="https://si.example.test/mcp")
        store = _designated_store(self)
        with mock.patch.dict(os.environ, _divergent_pricing_env()):
            with self.assertRaises(ValueError) as caught:
                create_http_server(store, verifier)
        self.assertIn("diverges", str(caught.exception))

    def test_direct_constructors_accept_coherent_policies(self):
        from types import SimpleNamespace
        from aiqs_si.mcp_server import create_account_server
        store = _designated_store(self)
        env = {"AIQS_SI_ALLOW_DEFAULT_POLICIES": "1"}
        # A hardware-mode stub service: the default synthetic
        # Service is itself forbidden behind a designated store
        # (the deployment guard), which is not the gate under
        # test here.
        service = SimpleNamespace(mode="hardware", store=store)
        with mock.patch.dict(os.environ, env):
            server = create_account_server(
                store, lambda: "owner", service=service,
                embedded_test_worker=False)
        self.assertIsNotNone(server)

    def test_direct_mcp_constructor_rejects_stale_pricing_with_injected_service(self):
        # Round 1 coverage note: Astra's original counterexample
        # INJECTED a service. Injecting one must not bypass the
        # coherence gate — it runs before the service is used.
        from types import SimpleNamespace
        from aiqs_si.mcp_server import create_account_server
        store = _designated_store(self)
        service = SimpleNamespace(mode="hardware", store=store)
        with mock.patch.dict(os.environ, _divergent_pricing_env()):
            with self.assertRaises(ValueError) as caught:
                create_account_server(
                    store, lambda: "owner", service=service,
                    embedded_test_worker=False)
        self.assertIn("diverges", str(caught.exception))
        self.assertIn("pricing", str(caught.exception))

    def test_direct_http_constructor_accepts_coherent_policies(self):
        # Round 1 coverage note: the coherent control covered
        # only the MCP constructor. The HTTP constructor must
        # also compose normally under coherent policies.
        from types import SimpleNamespace
        from cryptography.hazmat.primitives.asymmetric import rsa
        from aiqs_si.auth import JWTVerifier, create_http_server
        key = rsa.generate_private_key(public_exponent=65537,
                                       key_size=2048)
        verifier = JWTVerifier(
            public_key=key.public_key(),
            issuer="https://issuer.example.test/auth/v1",
            audience="https://si.example.test/mcp")
        store = _designated_store(self)
        service = SimpleNamespace(mode="hardware", store=store)
        env = {"AIQS_SI_ALLOW_DEFAULT_POLICIES": "1"}
        with mock.patch.dict(os.environ, env):
            server = create_http_server(
                store, verifier, service=service,
                embedded_test_worker=False)
        self.assertIsNotNone(server)


if __name__ == "__main__":
    unittest.main()
