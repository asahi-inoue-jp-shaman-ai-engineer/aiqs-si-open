"""L3 consumption routing + diode evidence: linked accounts settle in
D-Planet behind a flag (default OFF), and the worker's diode serial
hash reaches execution_proof. Synthetic fixtures only."""
import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from aiqs_si.auth import JWTVerifier
from aiqs_si.dplanet import DPlanetError, routing_enabled_from_env
from aiqs_si.mcp_server import create_account_server
from aiqs_si.remote_queue import RemoteQueue
from aiqs_si.store import Store
from aiqs_si.worker_api import create_worker_app, WORKER_SCOPE
from prayer_fixtures import make_service as Service

PLAYER = "player-1"
LINK = "link-1"
DIODE_HASH = hashlib.sha256(b"FT-TEST-SERIAL").hexdigest()[:24]


class FakeCreditPlanet:
    """In-memory D-Planet credit double with the L1 ops semantics:
    reserve/settle/release keyed by job, idempotent on an identical
    re-send, conflicting re-send rejected, release bounded by the
    reserved amount."""

    def __init__(self, balance=0):
        self.wallet = balance
        self.reserved = {}
        self.closed = {}
        self.calls = []
        self.down = False
        self.flaky = set()

    def _gate(self, method, job_id):
        self.calls.append(method)
        if self.down:
            raise DPlanetError(None, "D-Planet internal API unreachable")
        if (method, job_id) in self.flaky:
            self.flaky.discard((method, job_id))
            raise DPlanetError(503, "D-Planet temporarily unavailable")

    def reserve(self, player_id, job_id, amount):
        self._gate("reserve", job_id)
        if job_id in self.reserved:
            if self.reserved[job_id] != amount:
                raise DPlanetError(409, "reservation conflict")
            return {"state": "reserved", "balance": self.wallet}
        if job_id in self.closed:
            raise DPlanetError(409, "job already closed")
        if self.wallet < amount:
            raise DPlanetError(402, "クレジット残高が不足しています")
        self.wallet -= amount
        self.reserved[job_id] = amount
        return {"state": "reserved", "balance": self.wallet}

    def settle(self, player_id, job_id):
        self._gate("settle", job_id)
        if self.closed.get(job_id, (None,))[0] == "settled":
            return {"state": "settled"}
        if job_id not in self.reserved:
            raise DPlanetError(409, "no reservation")
        self.closed[job_id] = ("settled", self.reserved.pop(job_id))
        return {"state": "settled"}

    def release(self, player_id, job_id, amount):
        self._gate("release", job_id)
        if job_id in self.closed:
            state, moved = self.closed[job_id]
            if state == "released" and moved == amount:
                return {"state": "released", "balance": self.wallet}
            raise DPlanetError(409, "job already closed differently")
        if job_id not in self.reserved:
            raise DPlanetError(409, "no reservation")
        if not 0 <= amount <= self.reserved[job_id]:
            raise DPlanetError(409, "release exceeds reservation")
        del self.reserved[job_id]
        self.wallet += amount
        self.closed[job_id] = ("released", amount)
        return {"state": "released", "balance": self.wallet}

    def balance(self, player_id):
        if self.down:
            raise DPlanetError(None, "D-Planet internal API unreachable")
        return {"player_id": player_id, "balance": self.wallet}


class RoutingStoreTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "routing.sqlite"
        self.fake = FakeCreditPlanet(balance=1000)

    def make_store(self, *, routing, credits=500, link=True, client="fake"):
        store = Store(self.path,
                      dplanet_client=self.fake if client == "fake" else client,
                      dplanet_routing_enabled=routing)
        owner = store.create_test_account(credits)
        if link:
            store.record_account_link(owner, PLAYER, LINK)
        return store, owner

    def ledger(self, store, job):
        with store.transaction() as db:
            return [(row["kind"], row["amount"]) for row in
                    db.execute("SELECT kind,amount FROM ledger WHERE job=? ORDER BY id", (job,))]

    def test_flag_off_linked_account_behaves_exactly_as_before(self):
        store, owner = self.make_store(routing=False)
        job = store.submit(owner, "paid-1", {"operation": "scan"}, cost=100)
        self.assertEqual(store.balance(owner), 400)
        self.assertEqual(self.fake.calls, [])
        store.claim_next()
        store.finish(job["id"], "completed", {"mode": "synthetic"})
        self.assertEqual(store.balance(owner), 400)
        self.assertEqual(self.ledger(store, job["id"]), [("reserve", -100), ("settle", 0)])
        self.assertEqual(store.pending_dplanet_ops(), [])
        with store.transaction() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM job_credit_routes WHERE job=?",
                                         (job["id"],)).fetchone())

    def test_routing_on_without_link_stays_local(self):
        store, owner = self.make_store(routing=True, link=False)
        job = store.submit(owner, "paid-1", {"operation": "scan"}, cost=100)
        self.assertEqual(store.balance(owner), 400)
        self.assertEqual(self.fake.calls, [])
        store.claim_next()
        store.finish(job["id"], "failed", {"mode": "synthetic"})
        self.assertEqual(store.balance(owner), 500)
        self.assertEqual(self.fake.calls, [])

    def test_routed_submit_and_settle(self):
        store, owner = self.make_store(routing=True)
        job = store.submit(owner, "paid-1", {"operation": "scan"}, cost=100)
        self.assertEqual(store.balance(owner), 500)  # SI column untouched
        self.assertEqual(self.fake.wallet, 900)
        self.assertEqual(self.fake.reserved, {job["id"]: 100})
        self.assertEqual(self.ledger(store, job["id"]), [("reserve", -100)])
        store.claim_next()
        store.finish(job["id"], "completed", {"mode": "synthetic"})
        self.assertEqual(self.fake.closed, {job["id"]: ("settled", 100)})
        self.assertEqual(self.fake.wallet, 900)
        self.assertEqual(store.balance(owner), 500)
        self.assertEqual(self.ledger(store, job["id"]), [("reserve", -100), ("settle", 0)])
        self.assertEqual(store.pending_dplanet_ops(), [])
        proof = store.execution_proof(owner, job["id"])
        self.assertEqual(proof["settlement"],
                         [{"kind": "reserve", "amount": -100}, {"kind": "settle", "amount": 0}])

    def test_routed_submit_insufficient_maps_to_local_error(self):
        self.fake.wallet = 50
        store, owner = self.make_store(routing=True)
        with self.assertRaises(ValueError) as caught:
            store.submit(owner, "paid-1", {"operation": "scan"}, cost=100)
        self.assertEqual(str(caught.exception), "Account unavailable or insufficient credits")
        self.assertIsNone(store.get_by_request(owner, "paid-1"))
        self.assertEqual(store.balance(owner), 500)
        self.assertEqual(self.fake.reserved, {})

    def test_routed_submit_fails_closed_when_dplanet_down(self):
        self.fake.down = True
        store, owner = self.make_store(routing=True)
        with self.assertRaises(ValueError) as caught:
            store.submit(owner, "paid-1", {"operation": "scan"}, cost=100)
        self.assertIn("D-Planet", str(caught.exception))
        self.assertIsNone(store.get_by_request(owner, "paid-1"))
        self.assertEqual(store.balance(owner), 500)
        # Free scans are not gated on D-Planet (§4.2).
        scan = store.submit(owner, "scan-1", {"operation": "scan"}, cost=0)
        self.assertEqual(scan["state"], "queued")

    def test_routed_submit_replay_reserves_once(self):
        store, owner = self.make_store(routing=True)
        first = store.submit(owner, "paid-1", {"operation": "scan"}, cost=100)
        again = store.submit(owner, "paid-1", {"operation": "scan"}, cost=100)
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(self.fake.calls.count("reserve"), 1)
        self.assertEqual(self.fake.wallet, 900)
        with self.assertRaises(ValueError):
            store.submit(owner, "paid-1", {"operation": "irradiation"}, cost=100)

    def test_routed_finish_failed_releases_in_full(self):
        store, owner = self.make_store(routing=True)
        job = store.submit(owner, "paid-1", {"operation": "scan"}, cost=100)
        store.claim_next()
        store.finish(job["id"], "failed", {"mode": "synthetic"})
        self.assertEqual(self.fake.wallet, 1000)
        self.assertEqual(self.fake.closed, {job["id"]: ("released", 100)})
        self.assertEqual(store.balance(owner), 500)
        self.assertEqual(store.pending_dplanet_ops(), [])

    def test_routed_queued_cancel_releases_in_full(self):
        store, owner = self.make_store(routing=True)
        job = store.submit(owner, "paid-1", {"operation": "scan"}, cost=100)
        store.cancel(owner, job["id"])
        self.assertEqual(self.fake.wallet, 1000)
        self.assertEqual(self.fake.closed, {job["id"]: ("released", 100)})
        self.assertEqual(store.balance(owner), 500)
        self.assertEqual(store.get(owner, job["id"])["state"], "cancelled")

    def _irradiation(self, store, owner, key="send-1", count=3, rate=10):
        scan = store.submit(owner, key + "-scan", {"operation": "scan"}, cost=0)
        store.claim_next()
        store.finish(scan["id"], "completed", {"mode": "synthetic"})
        payload = {"operation": "irradiation", "scan_id": scan["id"],
                   "irradiation_count": count,
                   "quote": {"credits": count * rate, "credits_per_shot": rate}}
        return store.submit(owner, key, payload, cost=count * rate)

    def test_routed_partial_cancel_releases_remainder_only(self):
        store, owner = self.make_store(routing=True)
        job = self._irradiation(store, owner)
        self.assertEqual(self.fake.wallet, 970)
        store.claim_next()
        store.execution_control(job["id"], completed=1)
        store.cancel(owner, job["id"])  # running: stop request only
        self.assertEqual(self.fake.wallet, 970)
        store.finish(job["id"], "cancelled",
                     {"stop_command_acknowledged": True, "completed_count": 1})
        self.assertEqual(self.fake.closed, {job["id"]: ("released", 20)})
        self.assertEqual(self.fake.wallet, 990)
        self.assertEqual(store.balance(owner), 500)
        self.assertEqual(store.pending_dplanet_ops(), [])

    def test_routed_settle_failure_is_queued_and_retried_idempotently(self):
        store, owner = self.make_store(routing=True)
        job = store.submit(owner, "paid-1", {"operation": "scan"}, cost=100)
        self.fake.flaky.add(("settle", job["id"]))
        store.claim_next()
        store.finish(job["id"], "completed", {"mode": "synthetic"})
        pending = store.pending_dplanet_ops()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["op_key"], f"si:{job['id']}:settle")
        self.assertEqual(pending[0]["phase"], "settle")
        self.assertEqual(pending[0]["attempts"], 1)
        self.assertIn("temporarily unavailable", pending[0]["last_error"])
        self.assertEqual(self.fake.reserved, {job["id"]: 100})
        self.assertEqual(store.retry_pending_dplanet_ops(), {"delivered": 1, "remaining": 0})
        self.assertEqual(self.fake.closed, {job["id"]: ("settled", 100)})
        self.assertEqual(self.fake.wallet, 900)  # re-send did not double-apply
        self.assertEqual(store.retry_pending_dplanet_ops(), {"delivered": 0, "remaining": 0})

    def test_routed_unknown_resolution_releases_refund_or_settles(self):
        store, owner = self.make_store(routing=True)
        job = self._irradiation(store, owner, key="send-u")
        store.claim_next()
        with store.transaction() as db:
            db.execute("UPDATE jobs SET state='unknown' WHERE id=?", (job["id"],))
        store.resolve_unknown(job["id"], completed_count=1, stop_evidence_digest="c" * 64)
        self.assertEqual(self.fake.closed, {job["id"]: ("released", 20)})
        self.assertEqual(self.fake.wallet, 990)
        self.assertEqual(store.balance(owner), 500)

        job2 = self._irradiation(store, owner, key="send-v")
        store.claim_next()
        with store.transaction() as db:
            db.execute("UPDATE jobs SET state='unknown' WHERE id=?", (job2["id"],))
        store.resolve_unknown(job2["id"], completed_count=3, stop_evidence_digest="d" * 64)
        self.assertEqual(self.fake.closed[job2["id"]], ("settled", 30))
        self.assertEqual(store.pending_dplanet_ops(), [])

    def test_routing_enabled_without_client_fails_closed(self):
        store, owner = self.make_store(routing=True, client=None)
        with self.assertRaises(ValueError) as caught:
            store.submit(owner, "paid-1", {"operation": "scan"}, cost=100)
        self.assertIn("not configured", str(caught.exception))
        scan = store.submit(owner, "scan-1", {"operation": "scan"}, cost=0)
        self.assertEqual(scan["state"], "queued")

    def test_route_follows_the_job_after_unlink(self):
        store, owner = self.make_store(routing=True)
        job = store.submit(owner, "paid-1", {"operation": "scan"}, cost=100)
        store.mark_account_unlinked(owner)  # queued jobs do not block unlink
        store.claim_next()
        store.finish(job["id"], "completed", {"mode": "synthetic"})
        self.assertEqual(self.fake.closed, {job["id"]: ("settled", 100)})
        self.assertEqual(store.balance(owner), 500)


class DiodeHashTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name) / "diode.sqlite")
        self.owner = self.store.create_test_account(500)

    def test_claim_records_hash_for_proof(self):
        job = self.store.submit(self.owner, "scan-1", {"operation": "scan"}, cost=0)
        self.store.claim_next(worker="worker-1", diode_serial_hash=DIODE_HASH)
        with mock.patch.dict(os.environ, {"AIQS_SI_DEVICE_ID_KEY": "synthetic-test-key"}):
            proof = self.store.execution_proof(self.owner, job["id"])
            public_id = proof["device"]["public_device_id"]
            self.assertEqual(public_id,
                             self.store.public_device_id(self.owner, DIODE_HASH))
        self.assertNotEqual(public_id, DIODE_HASH)
        self.assertNotIn("diode_serial_hash", proof["device"])
        self.assertNotIn("diode_serial", proof["device"])
        self.assertFalse(proof["device"]["diode_serial_recorded_server_side"])

    def test_finish_records_hash_and_unreported_jobs_stay_null(self):
        job = self.store.submit(self.owner, "scan-1", {"operation": "scan"}, cost=0)
        self.store.claim_next(worker="worker-1")
        with mock.patch.dict(os.environ, {"AIQS_SI_DEVICE_ID_KEY": "synthetic-test-key"}):
            proof = self.store.execution_proof(self.owner, job["id"])
            self.assertIsNone(proof["device"]["public_device_id"])
            self.store.finish(job["id"], "completed", {"mode": "synthetic"},
                              worker="worker-1", diode_serial_hash=DIODE_HASH)
            proof = self.store.execution_proof(self.owner, job["id"])
            self.assertEqual(proof["device"]["public_device_id"],
                             self.store.public_device_id(self.owner, DIODE_HASH))

    def test_claim_rejects_malformed_hash(self):
        self.store.submit(self.owner, "scan-1", {"operation": "scan"}, cost=0)
        for bad in ("zz" * 12, "abcd", DIODE_HASH.upper(), 42):
            with self.assertRaises(ValueError):
                self.store.claim_next(worker="worker-1", diode_serial_hash=bad)
        claimed = self.store.claim_next(worker="worker-1", diode_serial_hash=DIODE_HASH)
        self.assertIsNotNone(claimed)

    def test_finish_ignores_malformed_hash_without_blocking_settlement(self):
        job = self.store.submit(self.owner, "scan-1", {"operation": "scan"}, cost=10)
        self.store.claim_next(worker="worker-1")
        self.store.finish(job["id"], "completed", {"mode": "synthetic"},
                          worker="worker-1", diode_serial_hash="not-a-hash")
        self.assertEqual(self.store.get(self.owner, job["id"])["state"], "completed")
        proof = self.store.execution_proof(self.owner, job["id"])
        self.assertIsNone(proof["device"]["public_device_id"])


class WorkerHashTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name) / "wire.sqlite")
        self.owner = self.store.create_test_account(500)
        verifier = JWTVerifier(public_key=self.key.public_key(), issuer="https://issuer.test",
                               audience="https://queue.test", required_scope=WORKER_SCOPE)
        self.client = TestClient(create_worker_app(
            self.store, verifier, allowed_subjects={"worker-a"}))
        self.addCleanup(self.client.close)

    def token(self):
        claims = dict(iss="https://issuer.test", aud="https://queue.test", sub="worker-a",
                      client_id="worker", scope=WORKER_SCOPE,
                      iat=int(time.time()), exp=int(time.time()) + 60)
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"typ": "at+jwt"})

    def test_remote_queue_reports_hash_on_claim_and_finish(self):
        queue = RemoteQueue("https://queue.test", self.token, client=self.client,
                            diode_serial_hash=DIODE_HASH)
        job = self.store.submit(self.owner, "scan-1", {"operation": "scan"}, cost=0)
        claimed = queue.claim_next()
        self.assertEqual(claimed["id"], job["id"])
        queue.finish(job["id"], "completed", {"mode": "synthetic"})
        with mock.patch.dict(os.environ, {"AIQS_SI_DEVICE_ID_KEY": "synthetic-test-key"}):
            proof = self.store.execution_proof(self.owner, job["id"])
            self.assertEqual(proof["device"]["public_device_id"],
                             self.store.public_device_id(self.owner, DIODE_HASH))

    def test_worker_api_rejects_malformed_hash_on_claim(self):
        self.store.submit(self.owner, "scan-1", {"operation": "scan"}, cost=0)
        response = self.client.post("/worker/claim",
                                    headers={"Authorization": "Bearer " + self.token()},
                                    json={"diode_serial_hash": "nope"})
        self.assertEqual(response.status_code, 409)
        self.assertIsNotNone(self.store.claim_next(worker="w"))

    def test_remote_queue_validates_hash_shape(self):
        with self.assertRaises(ValueError):
            RemoteQueue("https://queue.test", self.token, client=self.client,
                        diode_serial_hash="raw-serial-value")


class RoutingFlagTests(unittest.TestCase):
    def test_env_parsing_defaults_off(self):
        self.assertFalse(routing_enabled_from_env({}))
        self.assertFalse(routing_enabled_from_env({"AIQS_SI_DPLANET_ROUTING_ENABLED": "0"}))
        self.assertFalse(routing_enabled_from_env({"AIQS_SI_DPLANET_ROUTING_ENABLED": "off"}))
        for value in ("1", "true", "TRUE", "yes", "on"):
            self.assertTrue(routing_enabled_from_env(
                {"AIQS_SI_DPLANET_ROUTING_ENABLED": value}))


class MCPBalanceRoutingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.fake = FakeCreditPlanet(balance=1000)
        self.store = Store(Path(folder.name) / "mcp-routing.sqlite",
                           dplanet_client=self.fake, dplanet_routing_enabled=True)
        self.owner = self.store.create_test_account(500)
        self.store.record_account_link(self.owner, PLAYER, LINK)
        self.service = Service(self.store, {"z": [{"id": "entry", "text": "test"}]})

    def open(self, environ):
        with mock.patch.dict(os.environ, environ):
            server = create_account_server(self.store, self.owner, service=self.service,
                                           dplanet_client=self.fake)
        return TestClient(server.streamable_http_app(stateless_http=True, json_response=True),
                          base_url="http://127.0.0.1:8000")

    def call(self, client, name, arguments):
        response = client.post("/mcp", headers={"Accept": "application/json, text/event-stream"},
                               json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": name, "arguments": arguments}})
        result = response.json()["result"]
        self.assertFalse(result.get("isError"), result)
        return result["structuredContent"]

    def test_balance_comes_from_dplanet_while_routing_on(self):
        with self.open({"AIQS_SI_DPLANET_ROUTING_ENABLED": "1"}) as client:
            result = self.call(client, "credit_balance", {})
            self.assertEqual(result["available"], 1000)
            self.assertEqual(result["balance_source"], "dplanet")
            self.assertTrue(result["dplanet_linked"])
            self.assertEqual(self.call(client, "link_status", {})["credit_routing"], "dplanet")

    def test_balance_error_is_honest_when_dplanet_down(self):
        self.fake.down = True
        with self.open({"AIQS_SI_DPLANET_ROUTING_ENABLED": "1"}) as client:
            result = self.call(client, "credit_balance", {})
            self.assertIn("error", result)
            self.assertNotIn("available", result)
            self.assertEqual(result["balance_source"], "dplanet")

    def test_balance_stays_local_while_routing_off(self):
        with self.open({}) as client:
            result = self.call(client, "credit_balance", {})
            self.assertEqual(result["available"], 500)
            self.assertNotIn("balance_source", result)
            self.assertEqual(self.call(client, "link_status", {})["credit_routing"], "si_local")


if __name__ == "__main__":
    unittest.main()
