"""Batch 2d regressions: the eight findings of the OSS re-export
adversarial round (2026-11-23, reports/astra-diff-round-output.md).

HIGH 2 — unauthenticated uploads must not occupy reading slots:
the reading pool is reserved for EXECUTION, acquired only after
authentication and complete body reception; unverified traffic
consumes public capacity exclusively and under the public body
deadline.

HIGH 3 — a rotating worker must not starve behind a forged-token
flood: worker traffic carrying the operator-configured boundary
credential verifies through a reserved budget/pool that unknown
traffic cannot draw from (the application-layer form of an
authenticated private ingress). Without the credential, the
historical shared-budget behavior is unchanged.
"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aiqs_si.request_limits import RequestBodyLimit
from aiqs_si.serializers import public_job_view
from aiqs_si.store import Store
from aiqs_si.worker import Worker
from aiqs_si_device.synthetic import SyntheticDevice
from prayer_fixtures import JOY, PHOTO, make_service


class _AccountVerifier:
    async def verify_token(self, token):
        if token.startswith("good"):
            return SimpleNamespace(expires_at=None)
        return None


class _WorkerVerifier:
    def __init__(self):
        self.calls = 0

    async def verify_token(self, token):
        self.calls += 1
        if token in ("worker-token-v1", "worker-token-v2"):
            return SimpleNamespace(subject="worker-1")
        return None


def _send_recorder(statuses):
    async def send(message):
        if message["type"] == "http.response.start":
            statuses.append(message["status"])
    return send


class ReadingSlotAdmissionTests(unittest.IsolatedAsyncioTestCase):
    """HIGH 2: the reading slot belongs to execution, not to the
    upload. Four unauthenticated reading POSTs with withheld
    bodies must consume NO reading slots, must die on the PUBLIC
    body deadline (not the 60-second authenticated one), and must
    leave an authenticated reading fully servable."""

    async def test_unauthenticated_uploads_never_hold_reading_slots(self):
        calls = []

        async def app(scope, receive, send):
            calls.append(scope["path"])
            await receive()
            await send({"type": "http.response.start", "status": 200,
                        "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        guard = RequestBodyLimit(
            app, account_verifier=_AccountVerifier(),
            body_timeout=30, public_body_timeout=0.3,
            rate=1000, burst=1000)
        release = asyncio.Event()

        async def withheld_receive():
            await release.wait()
            return {"type": "http.request", "body": b"{}"}

        statuses = []
        send = _send_recorder(statuses)
        public_reading = {"type": "http", "method": "POST",
                          "path": "/api/app/readings", "headers": []}
        tasks = [asyncio.create_task(guard(public_reading, withheld_receive, send))
                 for _ in range(4)]
        await asyncio.sleep(0.05)
        # While the bodies are withheld, no reading slot may be
        # occupied: the slot is for generation, not for upload.
        self.assertEqual(guard.reading_active, 0)
        # The public deadline (0.3s), not the authenticated one
        # (30s), ends these requests.
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        self.assertEqual(statuses, [408, 408, 408, 408])
        self.assertEqual(guard.reading_active, 0)
        self.assertEqual(calls, [])
        release.set()

    async def test_authenticated_reading_served_while_uploads_withheld(self):
        calls = []

        async def app(scope, receive, send):
            calls.append(scope["path"])
            await receive()
            await send({"type": "http.response.start", "status": 200,
                        "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        guard = RequestBodyLimit(
            app, account_verifier=_AccountVerifier(),
            body_timeout=30, public_body_timeout=30,
            rate=1000, burst=1000)
        release = asyncio.Event()

        async def withheld_receive():
            await release.wait()
            return {"type": "http.request", "body": b"{}"}

        async def quick_receive():
            return {"type": "http.request", "body": b"{}"}

        statuses = []
        send = _send_recorder(statuses)
        public_reading = {"type": "http", "method": "POST",
                          "path": "/api/app/readings", "headers": []}
        tasks = [asyncio.create_task(guard(public_reading, withheld_receive, send))
                 for _ in range(4)]
        await asyncio.sleep(0.05)
        authed_reading = {"type": "http", "method": "POST",
                          "path": "/api/app/readings",
                          "headers": [(b"authorization", b"Bearer good-1")]}
        await guard(authed_reading, quick_receive, send)
        # The authenticated reading reached the application even
        # though four unauthenticated uploads are still open.
        self.assertEqual(calls, ["/api/app/readings"])
        self.assertEqual(statuses, [200])
        release.set()
        await asyncio.gather(*tasks)

    async def test_held_uploads_occupy_public_lane_slots(self):
        # Round 2 (Astra): the swap must not swing to the opposite
        # bypass — while buffering, a reading upload holds its
        # LANE slot, so lane concurrency still bounds total
        # buffered bodies. Two held uploads fill a 2-slot public
        # lane; a third public request is refused; no reading
        # slot is touched meanwhile.
        calls = []

        async def app(scope, receive, send):
            calls.append(scope["path"])
            await receive()
            await send({"type": "http.response.start", "status": 200,
                        "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        guard = RequestBodyLimit(
            app, account_verifier=_AccountVerifier(),
            body_timeout=30, public_body_timeout=30,
            rate=1000, burst=1000, max_concurrent=2)
        release = asyncio.Event()

        async def withheld_receive():
            await release.wait()
            return {"type": "http.request", "body": b"{}"}

        async def quick_receive():
            return {"type": "http.request", "body": b"{}"}

        statuses = []
        send = _send_recorder(statuses)
        public_reading = {"type": "http", "method": "POST",
                          "path": "/api/app/readings", "headers": []}
        tasks = [asyncio.create_task(guard(public_reading, withheld_receive, send))
                 for _ in range(2)]
        await asyncio.sleep(0.05)
        self.assertEqual(guard.active, 2)
        self.assertEqual(guard.reading_active, 0)
        ordinary = {"type": "http", "method": "POST",
                    "path": "/api/app/jobs/scan", "headers": []}
        await guard(ordinary, quick_receive, send)
        self.assertEqual(statuses, [503])
        release.set()
        await asyncio.gather(*tasks)
        self.assertEqual(sorted(calls), ["/api/app/readings"] * 2)
        self.assertEqual(guard.active, 0)
        self.assertEqual(guard.reading_active, 0)

    async def test_reading_pool_still_bounds_execution(self):
        # The execution bound itself is unchanged: four readings
        # executing concurrently still 503 the fifth. Since
        # batch 2e (R2) the APPLICATION acquires the pool through
        # the scope gate after its authentication, so this stub
        # does exactly what readings_create does: acquire, and
        # answer 503 itself when the pool is full.
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def app(scope, receive, send):
            calls.append(scope["path"])
            await receive()
            gate = scope.get("aiqs_si.reading_gate")
            lease = await gate.acquire() if gate is not None else None
            if gate is not None and lease is None:
                await send({"type": "http.response.start", "status": 503,
                            "headers": []})
                await send({"type": "http.response.body", "body": b"busy"})
                return
            entered.set()
            await release.wait()
            if lease is not None:
                lease.release()
            await send({"type": "http.response.start", "status": 200,
                        "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        guard = RequestBodyLimit(
            app, account_verifier=_AccountVerifier(),
            body_timeout=30, public_body_timeout=30,
            rate=1000, burst=1000)

        async def quick_receive():
            return {"type": "http.request", "body": b"{}"}

        statuses = []
        send = _send_recorder(statuses)
        authed_reading = {"type": "http", "method": "POST",
                          "path": "/api/app/readings",
                          "headers": [(b"authorization", b"Bearer good-1")]}
        tasks = [asyncio.create_task(guard(authed_reading, quick_receive, send))
                 for _ in range(4)]
        await asyncio.sleep(0.1)
        self.assertEqual(guard.reading_active, 4)
        await guard(authed_reading, quick_receive, send)
        self.assertEqual(statuses, [503])
        release.set()
        await asyncio.gather(*tasks)
        # The fifth request reached the application and was
        # refused by the gate (503 above) without executing.
        self.assertEqual(len(calls), 5)
        self.assertEqual(guard.reading_active, 0)


class WorkerBoundaryVerificationTests(unittest.IsolatedAsyncioTestCase):
    """HIGH 3: with a boundary credential configured, a worker
    presenting it verifies on reserved capacity even while forged
    tokens saturate the shared verification budget. A wrong or
    absent credential gets exactly the historical behavior."""

    async def _run(self, guard, scope):
        statuses = []

        async def send(message):
            if message["type"] == "http.response.start":
                statuses.append(message["status"])

        async def receive():
            return {"type": "http.request", "body": b""}

        await guard(scope, receive, send)
        return statuses

    def _guard(self, verifier, calls, **kwargs):
        async def app(scope, receive, send):
            calls.append(scope["path"])
            await receive()
        return RequestBodyLimit(
            app, worker_verifier=verifier, allowed_workers=("worker-1",),
            worker_verify_rate=0, worker_verify_burst=3, **kwargs)

    async def test_rotating_worker_starves_without_boundary(self):
        # Documents the pre-fix exposure and pins that the fix
        # does not silently change keyless behavior: with the
        # shared budget exhausted by forged tokens, a brand-new
        # (rotated) legitimate token is refused 429.
        verifier = _WorkerVerifier()
        calls = []
        guard = self._guard(verifier, calls,
                            worker_boundary_key="boundary-secret")
        forged = {"type": "http", "path": "/worker/heartbeat",
                  "headers": [(b"authorization", b"Bearer junk")]}
        for _ in range(3):
            await self._run(guard, forged)
        rotated = {"type": "http", "path": "/worker/heartbeat",
                   "headers": [(b"authorization", b"Bearer worker-token-v2")]}
        self.assertEqual(await self._run(guard, rotated), [429])
        self.assertEqual(calls, [])

    async def test_boundary_credential_reserves_verification(self):
        verifier = _WorkerVerifier()
        calls = []
        guard = self._guard(verifier, calls,
                            worker_boundary_key="boundary-secret")
        forged = {"type": "http", "path": "/worker/heartbeat",
                  "headers": [(b"authorization", b"Bearer junk")]}
        for _ in range(3):
            await self._run(guard, forged)
        # The shared budget is now empty (rate=0 in this fixture).
        rotated = {"type": "http", "path": "/worker/heartbeat",
                   "headers": [(b"authorization", b"Bearer worker-token-v2"),
                               (b"x-si-worker-boundary", b"boundary-secret")]}
        statuses = await self._run(guard, rotated)
        self.assertEqual(calls, ["/worker/heartbeat"])
        self.assertNotEqual(statuses, [429])
        # And the rotated token is cached afterwards, like any
        # verified worker token.
        calls_before = verifier.calls
        await self._run(guard, rotated)
        self.assertEqual(verifier.calls, calls_before)

    async def test_wrong_boundary_credential_gets_no_reserve(self):
        verifier = _WorkerVerifier()
        calls = []
        guard = self._guard(verifier, calls,
                            worker_boundary_key="boundary-secret")
        forged = {"type": "http", "path": "/worker/heartbeat",
                  "headers": [(b"authorization", b"Bearer junk")]}
        for _ in range(3):
            await self._run(guard, forged)
        wrong = {"type": "http", "path": "/worker/heartbeat",
                 "headers": [(b"authorization", b"Bearer worker-token-v2"),
                             (b"x-si-worker-boundary", b"not-the-secret")]}
        self.assertEqual(await self._run(guard, wrong), [429])
        self.assertEqual(calls, [])

    async def test_boundary_reserve_is_itself_bounded(self):
        # The reserved channel is not an unbounded verification
        # oracle for whoever holds the key: its own budget caps
        # forged-token checks presented WITH the credential.
        verifier = _WorkerVerifier()
        calls = []
        guard = self._guard(verifier, calls,
                            worker_boundary_key="boundary-secret",
                            worker_boundary_verify_rate=0,
                            worker_boundary_verify_burst=2)
        forged_with_key = {
            "type": "http", "path": "/worker/heartbeat",
            "headers": [(b"authorization", b"Bearer junk"),
                        (b"x-si-worker-boundary", b"boundary-secret")]}
        seen = []
        for _ in range(4):
            seen += await self._run(guard, forged_with_key)
        self.assertEqual(verifier.calls, 2)
        self.assertEqual(seen, [401, 401, 429, 429])
        self.assertEqual(calls, [])


class ResultSemanticBindingTests(unittest.TestCase):
    """Batch 2d, MEDIUM 5: a well-typed result must also be TRUE.
    On the pre-fix tree every forged finish below was accepted and
    then published, with the public result and the execution proof
    free to disagree."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = Store(Path(self.folder.name) / "queue.sqlite")
        self.owner = self.store.create_test_account(5000)
        self.service = make_service(self.store,
                                    {"field": [{"id": "1", "text": "test"}]})
        self.scan = self.service.submit_image_scan(
            self.owner, "scan", "test", [PHOTO], self_love_joy=JOY)

    def _irradiation(self, count=1):
        Worker(self.store, SyntheticDevice()).run_one()
        return self.service.submit_irradiation(
            self.owner, "send", self.scan["id"], count=count)

    def test_astra_counterexample_is_rejected(self):
        job = self._irradiation(count=1)
        self.store.claim_next(worker="a")
        self.store.execution_control(job["id"], 1, worker="a")
        forged = {"mode": "hardware", "requested_count": 999999,
                  "completed_count": 999999,
                  "physical_output_verified": True,
                  "executed_physically": True}
        with self.assertRaises(ValueError):
            self.store.finish(job["id"], "completed", forged, worker="a")
        self.assertEqual(self.store.get(self.owner, job["id"])["state"],
                         "running")

    def test_each_false_claim_is_rejected_individually(self):
        job = self._irradiation(count=1)
        self.store.claim_next(worker="a")
        base = {"mode": "synthetic", "requested_count": 1,
                "completed_count": 1, "simulated_count": 1,
                "executed_physically": False,
                "physical_output_verified": False}
        for patch in ({"requested_count": 2},
                      {"completed_count": 2},
                      {"simulated_count": 2},
                      {"physical_output_verified": True},
                      {"executed_physically": True},
                      {"simulated_count": 1, "mode": "hardware"}):
            with self.subTest(patch=patch):
                with self.assertRaises(ValueError):
                    self.store.finish(job["id"], "completed",
                                      {**base, **patch}, worker="a")
        # The genuine shape still finishes.
        self.store.finish(job["id"], "completed", base, worker="a")
        self.assertEqual(self.store.get(self.owner, job["id"])["state"],
                         "completed")

    def test_final_state_coherence(self):
        # Round 2 (Astra): counts must agree with the FINAL STATE,
        # not only with the reservation. A failed irradiation
        # claiming a completed physical execution was accepted
        # while its reservation was released in full; a completed
        # one could report zero shots with no progress record.
        job = self._irradiation(count=1)
        self.store.claim_next(worker="a")
        forged_failed = {"mode": "hardware", "requested_count": 1,
                         "completed_count": 1,
                         "executed_physically": True,
                         "physical_output_verified": False}
        with self.assertRaises(ValueError):
            self.store.finish(job["id"], "failed", forged_failed,
                              worker="a")
        with self.assertRaises(ValueError):
            self.store.finish(job["id"], "completed",
                              {"mode": "synthetic", "requested_count": 1,
                               "completed_count": 0, "simulated_count": 0,
                               "executed_physically": False,
                               "physical_output_verified": False},
                              worker="a")
        with self.assertRaises(ValueError):
            self.store.finish(job["id"], "completed",
                              {"mode": "synthetic",
                               "physical_output_verified": False},
                              worker="a")
        # The truthful shapes still land: a reason-only failure,
        # and (on a fresh job) a full completion.
        self.store.finish(job["id"], "failed",
                          {"reason": "device_mode_mismatch"}, worker="a")
        self.assertEqual(self.store.get(self.owner, job["id"])["state"],
                         "failed")

    def test_cancelled_simulated_count_is_bound_to_progress(self):
        # Round 3 (Astra): cancellation checked completed_count
        # against progress but left simulated_count bounded only
        # by the reservation — a zero-progress cancellation could
        # claim a simulated execution while the settlement
        # refunded everything and the public view published the
        # phantom count.
        job = self._irradiation(count=1)
        self.store.claim_next(worker="a")
        self.store.execution_control(job["id"], 0, worker="a")
        forged = {"mode": "synthetic", "requested_count": 1,
                  "completed_count": 0, "simulated_count": 1,
                  "stop_command_acknowledged": True,
                  "executed_physically": False,
                  "physical_output_verified": False}
        with self.assertRaises(ValueError):
            self.store.finish(job["id"], "cancelled", forged, worker="a")
        # The truthful zero-progress cancellation lands.
        truthful = dict(forged, simulated_count=0)
        self.store.finish(job["id"], "cancelled", truthful, worker="a")
        self.assertEqual(self.store.get(self.owner, job["id"])["state"],
                         "cancelled")

    def test_cancelled_without_progress_cannot_claim_execution(self):
        job = self._irradiation(count=2)
        self.store.claim_next(worker="a")
        forged = {"mode": "synthetic", "requested_count": 2,
                  "completed_count": 1, "simulated_count": 1,
                  "executed_physically": False,
                  "physical_output_verified": False}
        with self.assertRaises(ValueError):
            self.store.finish(job["id"], "cancelled", forged, worker="a")

    def test_partial_synthetic_cancellation_matches_progress(self):
        # The genuine stop shape: progress, completed and
        # simulated all agree on the shots that actually ran.
        job = self._irradiation(count=3)
        self.store.claim_next(worker="a")
        self.store.execution_control(job["id"], 1, worker="a")
        result = {"mode": "synthetic", "requested_count": 3,
                  "completed_count": 1, "simulated_count": 1,
                  "stop_command_acknowledged": True,
                  "executed_physically": False,
                  "physical_output_verified": False}
        self.store.finish(job["id"], "cancelled", result, worker="a")
        self.assertEqual(self.store.get(self.owner, job["id"])["state"],
                         "cancelled")

    def test_semantic_validator_branches_directly(self):
        # Round 4 (Astra): the store-level no-progress
        # cancellation above is rejected by the store's own
        # progress check before the schema branch runs, so pin
        # the schema branches directly — including synthetic
        # UNDERREPORTING against positive progress (equality,
        # not just an upper bound).
        from aiqs_si.result_schema import validate_result_semantics
        base = {"mode": "synthetic", "requested_count": 3,
                "executed_physically": False,
                "physical_output_verified": False}
        with self.assertRaises(ValueError):
            validate_result_semantics(
                dict(base, completed_count=1, simulated_count=1),
                operation="irradiation", state="cancelled",
                reserved_count=3, progress_completed=None)
        with self.assertRaises(ValueError):
            validate_result_semantics(
                dict(base, completed_count=2, simulated_count=1),
                operation="irradiation", state="cancelled",
                reserved_count=3, progress_completed=2)
        # An omitted simulated count is zero, and zero
        # underreports positive synthetic progress.
        with self.assertRaises(ValueError):
            validate_result_semantics(
                dict(base, completed_count=2),
                operation="irradiation", state="cancelled",
                reserved_count=3, progress_completed=2)
        # Exact agreement passes.
        validate_result_semantics(
            dict(base, completed_count=2, simulated_count=2),
            operation="irradiation", state="cancelled",
            reserved_count=3, progress_completed=2)

    def test_scan_finish_accepts_the_real_hits_distribution(self):
        # Regression: an earlier semantic rule bounded an entry's
        # hits by the configured cycle count, on the premise that
        # hits are "resonated cycles". The engine's hits are draw
        # counts (scan_pro makes N*cycles draws over N entries; an
        # entry's expected hits EQUAL the cycle count and published
        # top entries sit above it), so such a bound rejects
        # genuine device scans as a matter of course. This fixture
        # models a distribution of that shape - top entries above
        # the cycle count - with synthetic entries; the finish
        # must land and the hits must be stored unaltered.
        import json
        import sqlite3
        real_shape_hits = [129, 118, 116, 111, 110, 109, 107, 107, 105, 101]
        # Pin a catalog the fixture entries resolve against, so
        # the public serializer path below can publish them.
        catalog_digest = self.store.save_catalog({
            "field": [{"id": f"e{n}", "text": f"Entry {n}"}
                      for n in range(1, len(real_shape_hits) + 1)]})
        db = sqlite3.connect(self.store.path)
        payload = json.loads(db.execute(
            "SELECT payload FROM jobs WHERE id=?",
            (self.scan["id"],)).fetchone()[0])
        payload["catalog_digest"] = catalog_digest
        payload["execution_settings"] = {
            "cycles": 88, "top_mfs": 10, "top_entries": 10,
            "qrs_threshold": 150, "duration_sec": 9.0, "count": 33}
        db.execute("UPDATE jobs SET payload=? WHERE id=?",
                   (json.dumps(payload), self.scan["id"]))
        db.commit()
        db.close()
        self.store.claim_next(worker="a")
        result = {"mode": "hardware", "physical_output_verified": False,
                  "intention_usage": "recorded_only",
                  "scan": {"cycles": 88, "total_mfs": 130,
                           "observer_coordinate_applied": False,
                           "final_entries": [
                               {"id": f"e{n}", "hits": hits, "rank": 1}
                               for n, hits in enumerate(real_shape_hits, 1)]}}
        self.store.finish(self.scan["id"], "completed", result, worker="a")
        stored = self.store.get(self.owner, self.scan["id"])
        self.assertEqual(stored["state"], "completed")
        self.assertEqual(
            [entry["hits"] for entry in stored["result"]["scan"]["final_entries"]],
            real_shape_hits)
        # The public view must publish the same hits unaltered:
        # the serializer resolves the entries against the pinned
        # catalog and finite-bounds them, nothing more.
        view = public_job_view(self.store, stored)
        self.assertEqual(
            [entry["hits"] for entry in view["result"]["scan"]["final_entries"]],
            real_shape_hits)

    def test_semantics_validator_directly(self):
        from aiqs_si.result_schema import (validate_result_semantics,
                                           validate_worker_result)
        # Hits carry no cycle-count bound at the semantic layer:
        # a distribution whose top entries exceed the cycle count
        # passes, and no bound is invented for larger values either.
        validate_result_semantics(
            {"scan": {"final_entries": [{"hits": 129}, {"hits": 10**6}]}},
            operation="scan")
        with self.assertRaises(ValueError):
            validate_result_semantics(
                {"physical_output_verified": True}, operation="scan")
        # The finite bound lives in the type schema instead:
        # a hits value beyond MAX_COUNT is rejected at finish.
        with self.assertRaises(ValueError):
            validate_worker_result(
                {"scan": {"final_entries": [{"id": "e1", "hits": 10**6}]}})

    def test_unknown_without_progress_cannot_claim_execution(self):
        # Antigravity glass audit, finding 8: the unknown state
        # had no no-progress rule at all, so a worker could
        # publish the whole reservation as executed shots for a
        # job whose outcome is by definition unconfirmed.
        from aiqs_si.result_schema import validate_result_semantics
        with self.assertRaises(ValueError):
            validate_result_semantics(
                {"mode": "hardware", "completed_count": 3,
                 "executed_physically": True},
                operation="irradiation", state="unknown",
                reserved_count=3, progress_completed=None)
        with self.assertRaises(ValueError):
            validate_result_semantics(
                {"mode": "hardware", "executed_physically": True},
                operation="irradiation", state="unknown",
                reserved_count=3, progress_completed=None)
        # The truthful unknown finish (no execution claim) lands.
        validate_result_semantics(
            {"mode": "hardware"},
            operation="irradiation", state="unknown",
            reserved_count=3, progress_completed=None)

    def test_zero_progress_cannot_claim_physical_execution(self):
        # Antigravity glass audit, finding 8: a cancelled finish
        # with zero recorded progress could still claim physical
        # execution, publishing an execution the settlement
        # treats as never having happened.
        from aiqs_si.result_schema import validate_result_semantics
        with self.assertRaises(ValueError):
            validate_result_semantics(
                {"mode": "hardware", "completed_count": 0,
                 "executed_physically": True},
                operation="irradiation", state="cancelled",
                reserved_count=3, progress_completed=0)

    def test_publication_prefers_authoritative_records(self):
        job = self._irradiation(count=33)
        Worker(self.store, SyntheticDevice()).run_one()
        stored = self.store.get(self.owner, job["id"])
        self.assertEqual(stored["state"], "completed")
        # Simulate a legacy/tampered stored row: the public views
        # must still certify the reservation and the progress, and
        # must never publish a physical-output verification claim.
        import json
        import sqlite3
        tampered = dict(stored["result"])
        tampered["requested_count"] = 999999
        tampered["physical_output_verified"] = True
        db = sqlite3.connect(self.store.path)
        db.execute("UPDATE jobs SET result=? WHERE id=?",
                   (json.dumps(tampered), job["id"]))
        db.commit()
        db.close()
        fresh = self.store.get(self.owner, job["id"])
        view = public_job_view(self.store, fresh)
        self.assertEqual(view["result"]["requested_count"], 33)
        self.assertEqual(view["result"]["completed_count"], 33)
        self.assertIs(view["result"]["physical_output_verified"], False)
        proof = self.store.execution_proof(self.owner, job["id"])
        self.assertEqual(proof["execution"]["requested_count"], 33)
        self.assertIs(proof["device"]["physical_output_verified"], False)


class WorkerStaleRevalidationChannelTests(unittest.IsolatedAsyncioTestCase):
    """Batch 2d, HIGH 3 round 2 (Astra): a boundary-authorized
    worker's STALE cache entry revalidated through the SHARED
    revalidation reserve — the same reserve account-token
    revalidation traffic can inflate. The boundary channel now
    pays for stale revalidation too."""

    async def _run(self, guard, scope):
        statuses = []

        async def send(message):
            if message["type"] == "http.response.start":
                statuses.append(message["status"])

        async def receive():
            return {"type": "http.request", "body": b""}

        await guard(scope, receive, send)
        return statuses

    async def test_stale_worker_revalidates_via_boundary_channel(self):
        verifier = _WorkerVerifier()
        calls = []

        async def app(scope, receive, send):
            calls.append(scope["path"])
            await receive()

        guard = RequestBodyLimit(
            app, worker_verifier=verifier, allowed_workers=("worker-1",),
            worker_boundary_key="boundary-secret")
        plain = {"type": "http", "path": "/worker/heartbeat",
                 "headers": [(b"authorization", b"Bearer worker-token-v1")]}
        await self._run(guard, plain)
        self.assertEqual(calls, ["/worker/heartbeat"])
        self.assertEqual(verifier.calls, 1)
        # Age the cached entry into staleness.
        key = guard._token_key("worker-token-v1")
        entry = guard._verify_cache["worker"][key]
        entry[0] = 0.0
        entry[1] = 0.0
        entry[2] = 0.0
        # Drain the shared revalidation reserve (no refill: the
        # timestamp is now, the rate is finite).
        import aiqs_si.request_limits as rl
        guard.reverify_tokens = 0
        guard.reverify_updated = rl.monotonic()
        # Without the credential, revalidation cannot be paid for.
        self.assertEqual(await self._run(guard, plain), [503])
        self.assertEqual(verifier.calls, 1)
        # With the credential, the boundary channel pays and the
        # established worker is revalidated.
        authorized = {"type": "http", "path": "/worker/heartbeat",
                      "headers": [(b"authorization", b"Bearer worker-token-v1"),
                                  (b"x-si-worker-boundary", b"boundary-secret")]}
        await self._run(guard, authorized)
        self.assertEqual(calls, ["/worker/heartbeat", "/worker/heartbeat"])
        self.assertEqual(verifier.calls, 2)
