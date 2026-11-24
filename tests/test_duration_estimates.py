"""Duration estimates (foundation item 3, 2026-11-23): the server
computes a RANGE in seconds for a scan or an irradiation — the job's
own effective duration plus the wait behind the jobs occupying the
single device lane — from operator-measured effective durations
(the estimate policy), the durable lane state, and fresh worker
presence. When any basis is missing the answer is null, never an
invented figure.

The device setting stamped into payloads (9 s/shot) is never the
basis: the measured end-to-end cost of one shot is about 11 s, held
in the policy. Synthetic fixtures only; the policy is injected.
"""
import json
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from aiqs_si.application import create_application
from aiqs_si.auth import JWTVerifier
from aiqs_si.estimates import duration_estimate, estimate_from_lane
from aiqs_si.policies import (EstimatePolicy, PolicyError,
                              load_estimate_policy)
from aiqs_si.serializers import public_job_view
from aiqs_si.store import Store
from aiqs_si.worker import Worker
from aiqs_si.worker_api import WORKER_SCOPE, create_worker_app
from aiqs_si_device.synthetic import SyntheticDevice
from prayer_fixtures import PHOTO, JOY, make_service as Service

CATALOG = {"field": [{"id": "one", "text": "test"}]}

POLICY = EstimatePolicy(scan_seconds=50, irradiation_seconds_per_shot=11,
                        range_low_factor=0.8, range_high_factor=1.6)
NO_POLICY = EstimatePolicy(scan_seconds=None,
                           irradiation_seconds_per_shot=None,
                           range_low_factor=0.8, range_high_factor=1.6)
SCAN_ONLY = EstimatePolicy(scan_seconds=50,
                           irradiation_seconds_per_shot=None,
                           range_low_factor=0.8, range_high_factor=1.6)
IRRADIATION_ONLY = EstimatePolicy(scan_seconds=None,
                                  irradiation_seconds_per_shot=11,
                                  range_low_factor=0.8, range_high_factor=1.6)


class EstimatePolicyLoaderTests(unittest.TestCase):
    def test_unset_env_means_no_measured_values(self):
        policy = load_estimate_policy({})
        self.assertIsNone(policy.scan_seconds)
        self.assertIsNone(policy.irradiation_seconds_per_shot)
        self.assertEqual(policy.range_low_factor, 0.8)
        self.assertEqual(policy.range_high_factor, 1.6)

    def test_full_and_partial_values(self):
        env = {"AIQS_SI_ESTIMATE_POLICY_JSON":
               '{"scan_seconds": 50, "irradiation_seconds_per_shot": 11.5,'
               ' "range_low_factor": 0.9, "range_high_factor": 2}'}
        policy = load_estimate_policy(env)
        self.assertEqual(policy.scan_seconds, 50)
        self.assertEqual(policy.irradiation_seconds_per_shot, 11.5)
        self.assertEqual(policy.range_low_factor, 0.9)
        self.assertEqual(policy.range_high_factor, 2)
        partial = load_estimate_policy(
            {"AIQS_SI_ESTIMATE_POLICY_JSON": '{"scan_seconds": 45}'})
        self.assertEqual(partial.scan_seconds, 45)
        self.assertIsNone(partial.irradiation_seconds_per_shot)

    def test_explicit_null_seconds_means_unmeasured(self):
        policy = load_estimate_policy(
            {"AIQS_SI_ESTIMATE_POLICY_JSON":
             '{"scan_seconds": null, "irradiation_seconds_per_shot": 11}'})
        self.assertIsNone(policy.scan_seconds)
        self.assertEqual(policy.irradiation_seconds_per_shot, 11)

    def test_rejects_bad_values(self):
        bad = ['{"scan_seconds": "50"}',
               '{"scan_seconds": true}',
               '{"scan_seconds": 0}',
               '{"scan_seconds": -5}',
               '{"scan_seconds": NaN}',
               '{"scan_seconds": Infinity}',
               '{"irradiation_seconds_per_shot": 0}',
               '{"range_low_factor": 0}',
               '{"range_low_factor": 1.5}',
               '{"range_high_factor": 0.5}',
               '{"range_high_factor": 6}',
               '{"unknown_key": 1}',
               'not json',
               '[1, 2]']
        for raw in bad:
            with self.assertRaises(PolicyError, msg=raw):
                load_estimate_policy({"AIQS_SI_ESTIMATE_POLICY_JSON": raw})

    def test_rejects_equal_range_factors(self):
        # Equal factors would collapse the range to a point promise.
        with self.assertRaises(PolicyError):
            load_estimate_policy(
                {"AIQS_SI_ESTIMATE_POLICY_JSON":
                 '{"range_low_factor": 1, "range_high_factor": 1}'})

    def test_rejects_huge_integer_with_policy_error(self):
        # A 400-digit integer is valid JSON and below Python's int
        # digit limit; the loader must reject it with its controlled
        # error, not an OverflowError from a float conversion.
        raw = '{"scan_seconds": ' + "9" * 400 + '}'
        with self.assertRaises(PolicyError):
            load_estimate_policy({"AIQS_SI_ESTIMATE_POLICY_JSON": raw})


class StoreFixture(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name) / "estimates.sqlite")
        self.owner = self.store.register_identity(
            "https://issuer.example.test/auth/v1", "user-a")
        self.other = self.store.register_identity(
            "https://issuer.example.test/auth/v1", "user-b")
        # A worker is polling: the presence basis every estimate
        # needs. Presence-specific tests rewrite the contact row.
        self.store.note_worker_contact()

    def submit(self, key, payload, owner=None, cost=0):
        """Insert a queued job row directly: these fixtures exercise
        the lane and the estimator, not submission validation (the
        store's submit path rightly demands a real completed scan
        behind every irradiation payload, which is not what is
        being tested here)."""
        job_id = str(uuid.uuid4())
        encoded = json.dumps(payload, sort_keys=True)
        with self.store.transaction() as db:
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,NULL)",
                       (job_id, owner or self.owner, key, encoded,
                        "fixture-digest", cost, "queued"))
        return {"id": job_id}

    def set_state(self, job_id, state):
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET state=? WHERE id=?", (state, job_id))

    def set_payload(self, job_id, payload_text):
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET payload=? WHERE id=?",
                       (payload_text, job_id))


class LaneBasisTests(StoreFixture):
    def test_basis_reads_operation_and_count_in_order(self):
        self.submit("k-scan", {"operation": "scan"})
        self.submit("k-irr", {"operation": "irradiation",
                              "irradiation_count": 33})
        basis = self.store.lane_estimate_basis()
        self.assertFalse(basis["blocked"])
        self.assertIsNone(basis["target_state"])
        lane = basis["entries"]
        self.assertEqual([entry["operation"] for entry in lane],
                         ["scan", "irradiation"])
        self.assertIsNone(lane[0]["irradiation_count"])
        self.assertEqual(lane[1]["irradiation_count"], 33)
        self.assertTrue(all(entry["state"] == "queued" for entry in lane))

    def test_basis_keeps_running_and_unknown_occupants(self):
        first = self.submit("k-1", {"operation": "scan"})
        second = self.submit("k-2", {"operation": "scan"})
        claimed = self.store.claim_next()
        self.assertEqual(claimed["id"], first["id"])
        basis = self.store.lane_estimate_basis()
        self.assertEqual([entry["state"] for entry in basis["entries"]],
                         ["running", "queued"])
        # An unresolved job blocks the lane globally, and the
        # basis then skips the lane read entirely (the estimator
        # will answer None regardless of the entries).
        self.set_state(second["id"], "unknown")
        basis = self.store.lane_estimate_basis()
        self.assertTrue(basis["blocked"])
        self.assertEqual(basis["entries"], [])
        # Terminal jobs leave the lane.
        self.store.finish(first["id"], "completed", {"mode": "synthetic"})
        self.set_state(second["id"], "failed")
        basis = self.store.lane_estimate_basis()
        self.assertFalse(basis["blocked"])
        self.assertEqual(basis["entries"], [])

    def test_basis_marks_undecodable_payload_as_unknown_operation(self):
        job = self.submit("k-x", {"operation": "scan"})
        self.set_payload(job["id"], "not json")
        basis = self.store.lane_estimate_basis()
        self.assertIsNone(basis["entries"][0]["operation"])

    def test_basis_survives_deeply_nested_payload(self):
        # json.loads raises RecursionError past the interpreter's
        # nesting limit; the lane decoder must treat that as an
        # unreadable payload, not propagate a 500.
        job = self.submit("k-x", {"operation": "scan"})
        self.set_payload(job["id"], "[" * 5000 + "]" * 5000)
        basis = self.store.lane_estimate_basis()
        self.assertIsNone(basis["entries"][0]["operation"])
        self.assertIsNone(duration_estimate(self.store, "scan",
                                            policy=POLICY))

    def test_basis_for_job_is_owner_scoped_and_reports_state(self):
        first = self.submit("k-1", {"operation": "scan"})
        second = self.submit("k-2", {"operation": "scan"},
                             owner=self.other)
        basis = self.store.lane_estimate_basis(owner=self.other,
                                               job_id=second["id"])
        self.assertEqual(basis["target_state"], "queued")
        self.assertEqual(len(basis["entries"]), 1)
        self.assertEqual(basis["entries"][0]["operation"], "scan")
        basis = self.store.lane_estimate_basis(owner=self.owner,
                                               job_id=first["id"])
        self.assertEqual(basis["entries"], [])
        with self.assertRaises(KeyError):
            self.store.lane_estimate_basis(owner=self.owner,
                                           job_id=second["id"])

    def test_worker_contact_throttle_and_freshness(self):
        store = Store(Path(self.store.path).parent / "contact.sqlite")
        self.assertIsNone(store.worker_last_seen())
        store.note_worker_contact(now=1000.0)
        self.assertEqual(store.worker_last_seen(), 1000.0)
        store.note_worker_contact(now=1010.0)  # inside the 30 s throttle
        self.assertEqual(store.worker_last_seen(), 1000.0)
        store.note_worker_contact(now=1031.0)
        self.assertEqual(store.worker_last_seen(), 1031.0)

    def test_worker_contact_replaces_a_future_timestamp(self):
        # A wall-clock rollback must not let a future timestamp
        # engage the throttle or pose as fresh presence.
        store = Store(Path(self.store.path).parent / "future.sqlite")
        store.note_worker_contact(now=5000.0)
        store.note_worker_contact(now=1000.0)
        self.assertEqual(store.worker_last_seen(), 1000.0)

    def test_state_index_supports_the_lane_queries(self):
        # With a realistic terminal history, the blocked check must
        # use the state index rather than scanning every old job.
        import sqlite3
        store = Store(Path(self.store.path).parent / "history.sqlite")
        owner = store.register_identity(
            "https://issuer.example.test/auth/v1", "history-user")
        rows = [(str(uuid.uuid4()), owner, f"h-{i}",
                 '{"operation": "scan"}', "fixture-digest", 0,
                 "completed", None) for i in range(3000)]
        with store.transaction() as db:
            db.executemany("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)",
                           rows)
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)",
                       (str(uuid.uuid4()), owner, "h-queued",
                        '{"operation": "scan"}', "fixture-digest", 0,
                        "queued", None))
        db = sqlite3.connect(store.path)
        try:
            db.execute("ANALYZE")
            plan = db.execute(
                "EXPLAIN QUERY PLAN SELECT 1 FROM jobs "
                "WHERE state='unknown' LIMIT 1").fetchall()
        finally:
            db.close()
        self.assertTrue(
            any("idx_jobs_state" in str(step) for step in plan), plan)
        basis = store.lane_estimate_basis()
        self.assertEqual(len(basis["entries"]), 1)

    def test_candidate_queries_use_the_state_index(self):
        # Both phase-1 candidate queries (whole lane and boundary)
        # must run on the state index: with the boundary predicate
        # the planner otherwise prefers a rowid search and scans
        # the terminal history below the boundary (Astra round 4).
        import sqlite3
        store = Store(Path(self.store.path).parent / "plans.sqlite")
        owner = store.register_identity(
            "https://issuer.example.test/auth/v1", "plan-user")
        rows = [(str(uuid.uuid4()), owner, f"p-{i}",
                 '{"operation": "scan"}', "fixture-digest", 0,
                 "completed", None) for i in range(3000)]
        with store.transaction() as db:
            db.executemany("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)",
                           rows)
        db = sqlite3.connect(store.path)
        try:
            db.execute("ANALYZE")
            for sql in (
                    "SELECT rowid FROM jobs INDEXED BY idx_jobs_state "
                    "WHERE state IN ('queued','running','unknown') "
                    "LIMIT 101",
                    "SELECT rowid FROM jobs INDEXED BY idx_jobs_state "
                    "WHERE state IN ('queued','running','unknown') "
                    "AND rowid < 5 LIMIT 101"):
                plan = db.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
                text = " ".join(str(step) for step in plan)
                self.assertIn("idx_jobs_state", text, plan)
                self.assertNotIn("INTEGER PRIMARY KEY", text, plan)
        finally:
            db.close()

    def _history_store(self, name, history_rows):
        import sqlite3
        store = Store(Path(self.store.path).parent / name)
        owner = store.register_identity(
            "https://issuer.example.test/auth/v1", f"{name}-user")
        rows = [(str(uuid.uuid4()), owner, f"{name}-{i}",
                 '{"operation": "scan"}', "fixture-digest", 0,
                 "completed", None) for i in range(history_rows)]
        target_id = str(uuid.uuid4())
        with store.transaction() as db:
            db.executemany("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)",
                           rows)
            # Three queued jobs ahead of the target, so the
            # boundary basis exercises the sizing and payload
            # phases too, not just the candidate scan.
            for i in range(3):
                db.execute(
                    "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), owner, f"{name}-ahead-{i}",
                     '{"operation": "scan"}', "fixture-digest", 0,
                     "queued", None))
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)",
                       (target_id, owner, f"{name}-target",
                        '{"operation": "scan"}', "fixture-digest", 0,
                        "queued", None))
        raw = sqlite3.connect(store.path)
        try:
            raw.execute("ANALYZE")
        finally:
            raw.close()
        return store, owner, target_id

    def _counted_basis(self, store, **kwargs):
        """Run lane_estimate_basis with a progress handler on every
        connection; returns (basis, progress callbacks)."""
        import sqlite3
        steps = []
        real_connect = sqlite3.connect

        def counting_connect(*args, **kw):
            connection = real_connect(*args, **kw)
            connection.set_progress_handler(
                lambda: steps.append(1) or 0, 100)
            return connection

        with mock.patch.object(sqlite3, "connect", counting_connect):
            basis = store.lane_estimate_basis(**kwargs)
        return basis, len(steps)

    def test_basis_work_does_not_scale_with_history(self):
        # Both branches (whole lane and boundary) over two history
        # sizes an order of magnitude apart. Measured cost of the
        # indexed implementation is ~7 callbacks at both sizes; the
        # ceiling leaves headroom for plan jitter, while a
        # history-proportional scan (~3 VM steps per history row,
        # i.e. ~90 callbacks at 3,000 rows and ~900 at 30,000) fails
        # both the ceiling and the growth bound.
        counts = {}
        for label, history in (("small", 3000), ("large", 30000)):
            store, owner, target_id = self._history_store(
                f"work-{label}.sqlite", history)
            basis, whole = self._counted_basis(store)
            self.assertEqual(len(basis["entries"]), 4)
            basis, boundary = self._counted_basis(
                store, owner=owner, job_id=target_id)
            self.assertEqual(basis["target_state"], "queued")
            self.assertEqual(len(basis["entries"]), 3)
            counts[label] = (whole, boundary)
            self.assertLessEqual(whole, 30, counts)
            self.assertLessEqual(boundary, 30, counts)
        for branch in (0, 1):
            growth = counts["large"][branch] - counts["small"][branch]
            self.assertLessEqual(growth, 5, counts)

    def test_queue_status_still_reports_no_wait_estimate(self):
        # The shared queue_status surface (MCP included) is
        # deliberately unchanged: its estimated_wait_seconds stays
        # None; estimates live on the app API only.
        job = self.submit("k-q", {"operation": "scan"})
        status = self.store.queue_status(self.owner, job["id"])
        self.assertIsNone(status["estimated_wait_seconds"])
        self.assertEqual(status["jobs_ahead"], 0)

    def test_public_job_view_has_no_estimate(self):
        # The MCP serializer is an allowlist projection and must not
        # gain the estimate (or any lane aggregate).
        job = self.submit("k-q", {"operation": "scan"})
        view = public_job_view(self.store,
                               self.store.get(self.owner, job["id"]))
        self.assertNotIn("estimate", view)


class EstimatorTests(StoreFixture):
    def test_empty_lane_scan_and_irradiation_ranges(self):
        scan = duration_estimate(self.store, "scan", policy=POLICY)
        self.assertEqual(scan, {"min_seconds": 40, "max_seconds": 80,
                                "includes_queue_wait": True, "jobs_ahead": 0})
        irr = duration_estimate(self.store, "irradiation", 9, policy=POLICY)
        self.assertEqual(irr["min_seconds"], 79)   # floor(99 * 0.8)
        self.assertEqual(irr["max_seconds"], 159)  # ceil(99 * 1.6)
        self.assertEqual(irr["jobs_ahead"], 0)

    def test_queue_wait_sums_the_lane_ahead(self):
        self.submit("k-1", {"operation": "scan"})
        self.submit("k-2", {"operation": "irradiation",
                            "irradiation_count": 33})
        estimate = duration_estimate(self.store, "scan", policy=POLICY)
        # nominal = own 50 + scan 50 + irradiation 33*11=363 -> 463
        self.assertEqual(estimate["jobs_ahead"], 2)
        self.assertEqual(estimate["min_seconds"], 370)
        self.assertEqual(estimate["max_seconds"], 741)

    def test_running_job_ahead_counts_only_toward_the_upper_bound(self):
        # A running job's remaining time is anywhere in [0, full]:
        # its start is not durably known, so it must not raise the
        # lower bound (Astra round 1, finding 2).
        self.submit("k-1", {"operation": "irradiation",
                            "irradiation_count": 9})
        self.store.claim_next()
        estimate = duration_estimate(self.store, "scan", policy=POLICY)
        self.assertEqual(estimate["min_seconds"], 40)   # own 50 * 0.8 only
        self.assertEqual(estimate["max_seconds"], 239)  # (50+99) * 1.6 ceil

    def test_unknown_job_in_lane_removes_the_basis(self):
        blocked = self.submit("k-1", {"operation": "scan"})
        self.set_state(blocked["id"], "unknown")
        self.assertIsNone(duration_estimate(self.store, "scan", policy=POLICY))
        self.assertIsNone(duration_estimate(
            self.store, "irradiation", 9, policy=POLICY))

    def test_unknown_job_behind_also_blocks(self):
        # claim_next's unresolved-execution block is global: an
        # unknown job BEHIND an existing job still stops every
        # claim, so the ahead-only snapshot must not produce a
        # number for the job in front of it.
        mine = self.submit("k-1", {"operation": "scan"})
        behind = self.submit("k-2", {"operation": "scan"})
        self.set_state(behind["id"], "unknown")
        self.assertIsNone(duration_estimate(
            self.store, "scan", policy=POLICY,
            before_job_id=mine["id"], owner=self.owner))

    def test_unreadable_job_ahead_removes_the_basis(self):
        job = self.submit("k-1", {"operation": "scan"})
        self.set_payload(job["id"], "not json")
        self.assertIsNone(duration_estimate(self.store, "scan", policy=POLICY))

    def test_no_worker_contact_no_estimate(self):
        with self.store.transaction() as db:
            db.execute("DELETE FROM worker_contact")
        self.assertIsNone(duration_estimate(self.store, "scan",
                                            policy=POLICY))

    def test_stale_worker_contact_no_estimate(self):
        with self.store.transaction() as db:
            db.execute("UPDATE worker_contact SET seen_at=?",
                       (time.time() - 3600,))
        self.assertIsNone(duration_estimate(self.store, "scan",
                                            policy=POLICY))

    def test_future_worker_contact_is_no_basis(self):
        with self.store.transaction() as db:
            db.execute("UPDATE worker_contact SET seen_at=?",
                       (time.time() + 3600,))
        self.assertIsNone(duration_estimate(self.store, "scan",
                                            policy=POLICY))

    def test_dark_policy_reads_nothing(self):
        # An unmeasured operation must answer before any store
        # read: the unconfigured feature costs nothing per poll.
        class ExplodingStore:
            def worker_last_seen(self):
                raise AssertionError("store read for an unmeasured op")

            def lane_estimate_basis(self, **kwargs):
                raise AssertionError("store read for an unmeasured op")

        self.assertIsNone(duration_estimate(ExplodingStore(), "scan",
                                            policy=NO_POLICY))
        self.assertIsNone(duration_estimate(ExplodingStore(),
                                            "irradiation", 9,
                                            policy=NO_POLICY))

    def test_stored_invalid_counts_remove_the_basis(self):
        # The pure-function cases, this time through real payload
        # decoding: a stored count the submission path could never
        # have written is not a basis.
        for count in (True, 9.5, "9", 0, -3, 500):
            job = self.submit(f"k-{count!r}", {"operation": "scan"})
            self.set_payload(job["id"], json.dumps(
                {"operation": "irradiation", "irradiation_count": count}))
            self.assertIsNone(
                duration_estimate(self.store, "scan", policy=POLICY),
                msg=repr(count))
            self.set_state(job["id"], "failed")

    def test_over_budget_lane_has_no_estimate(self):
        for i in range(101):
            self.submit(f"k-{i}", {"operation": "scan"})
        self.assertIsNone(duration_estimate(self.store, "scan",
                                            policy=POLICY))

    def test_oversized_payload_has_no_estimate(self):
        job = self.submit("k-big", {"operation": "scan"})
        self.set_payload(job["id"], json.dumps(
            {"operation": "scan", "pad": "x" * (70 * 1024)}))
        self.assertIsNone(duration_estimate(self.store, "scan",
                                            policy=POLICY))

    def _counting_loads(self):
        """A json.loads spy over the store module: the budget must
        reject BEFORE any payload is decoded, so a rejected lane
        shows zero decode calls."""
        import aiqs_si.store as store_module
        real_loads = store_module.json.loads
        calls = []

        def spy(text, *args, **kwargs):
            calls.append(text)
            return real_loads(text, *args, **kwargs)

        return mock.patch.object(store_module.json, "loads", spy), calls

    def test_budget_rejects_before_decoding(self):
        patcher, calls = self._counting_loads()
        with patcher:
            for i in range(101):
                self.submit(f"k-{i}", {"operation": "scan"})
            self.assertIsNone(duration_estimate(self.store, "scan",
                                                policy=POLICY))
            self.assertEqual(calls, [])

    def test_oversized_payload_rejected_before_decoding(self):
        patcher, calls = self._counting_loads()
        job = self.submit("k-big", {"operation": "scan"})
        self.set_payload(job["id"], json.dumps(
            {"operation": "scan", "pad": "x" * (70 * 1024)}))
        with patcher:
            self.assertIsNone(duration_estimate(self.store, "scan",
                                                policy=POLICY))
            self.assertEqual(calls, [])

    def test_aggregate_budget_rejected_before_decoding(self):
        # Each payload is individually acceptable (~60 KiB); the
        # 1 MiB aggregate is not.
        patcher, calls = self._counting_loads()
        for i in range(20):
            job = self.submit(f"k-{i}", {"operation": "scan"})
            self.set_payload(job["id"], json.dumps(
                {"operation": "scan", "pad": "x" * (60 * 1024)}))
        with patcher:
            self.assertIsNone(duration_estimate(self.store, "scan",
                                                policy=POLICY))
            self.assertEqual(calls, [])

    def test_exactly_at_entry_limit_is_accepted(self):
        patcher, calls = self._counting_loads()
        for i in range(100):
            self.submit(f"k-{i}", {"operation": "scan"})
        with patcher:
            estimate = duration_estimate(self.store, "scan",
                                         policy=POLICY)
        self.assertIsNotNone(estimate)
        self.assertEqual(estimate["jobs_ahead"], 100)
        self.assertEqual(len(calls), 100)

    def test_payload_budget_counts_bytes_not_characters(self):
        # 40,000 CJK characters are ~120,000 UTF-8 bytes: over the
        # per-payload budget in bytes, under it in characters.
        patcher, calls = self._counting_loads()
        job = self.submit("k-wide", {"operation": "scan"})
        self.set_payload(job["id"], json.dumps(
            {"operation": "scan", "pad": "あ" * 40000},
            ensure_ascii=False))
        with patcher:
            self.assertIsNone(duration_estimate(self.store, "scan",
                                                policy=POLICY))
            self.assertEqual(calls, [])
        # 20,000 characters (~60,000 bytes) stay within budget.
        self.set_payload(job["id"], json.dumps(
            {"operation": "scan", "pad": "あ" * 20000},
            ensure_ascii=False))
        self.assertIsNotNone(duration_estimate(self.store, "scan",
                                               policy=POLICY))

    def _traced_estimate(self, operation="scan"):
        """Run duration_estimate with a SQL trace on every store
        connection; returns (estimate, [sql, ...])."""
        import sqlite3
        statements = []
        real_connect = sqlite3.connect

        def tracing_connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        with mock.patch.object(sqlite3, "connect", tracing_connect):
            estimate = duration_estimate(self.store, operation,
                                         policy=POLICY)
        return estimate, statements

    def test_entry_overflow_never_selects_payload_text(self):
        # The trace proves the transfer bound itself, not just the
        # null result: an over-limit lane is rejected from rowids
        # alone, so no statement may select the payload column.
        for i in range(101):
            self.submit(f"k-{i}", {"operation": "scan"})
        estimate, statements = self._traced_estimate()
        self.assertIsNone(estimate)
        self.assertFalse(
            any(", payload FROM" in sql for sql in statements),
            statements)

    def test_oversized_rejection_sizes_without_selecting_payload(self):
        job = self.submit("k-big", {"operation": "scan"})
        self.set_payload(job["id"], json.dumps(
            {"operation": "scan", "pad": "x" * (70 * 1024)}))
        estimate, statements = self._traced_estimate()
        self.assertIsNone(estimate)
        # The size phase ran (its result column is aliased nbytes)
        # and rejected the row before any payload text was selected.
        self.assertTrue(any("nbytes" in sql for sql in statements),
                        statements)
        self.assertFalse(
            any(", payload FROM" in sql for sql in statements),
            statements)

    def test_accepted_lane_sizes_before_selecting_payload(self):
        self.submit("k-1", {"operation": "scan"})
        estimate, statements = self._traced_estimate()
        self.assertIsNotNone(estimate)
        size_at = next(i for i, sql in enumerate(statements)
                       if "nbytes" in sql)
        select_at = next(i for i, sql in enumerate(statements)
                         if ", payload FROM" in sql)
        self.assertLess(size_at, select_at)

    def _make_recorder(self, stats):
        """Build a recording connection/cursor proxy family over
        `stats` (rows, per-statement rows, TEXT/BLOB bytes, largest
        single value). Values are counted POSITIONALLY — iterating
        a row yields values in column order — because reconstructing
        them through column names misses a payload hidden behind a
        duplicate column name (SELECT rowid, payload AS rowid: name
        lookup returns the first match). Cursors wrap execute /
        executemany / fetchmany / fetchone / iteration and
        connection.cursor(), and row counts accumulate per statement
        execution, so ordinary access patterns neither bypass nor
        fragment the accounting."""
        import sqlite3

        def record_value(value):
            if isinstance(value, str):
                size = len(value.encode("utf-8"))
            elif isinstance(value, (bytes, bytearray)):
                size = len(value)
            else:
                return
            stats["text_bytes"] += size
            stats["max_value_bytes"] = max(
                stats["max_value_bytes"], size)

        class RecordingCursor:
            def __init__(self, cursor):
                self._cursor = cursor
                self._statement_rows = 0

            def _take(self, rows):
                self._statement_rows += len(rows)
                stats["rows"] += len(rows)
                stats["max_statement_rows"] = max(
                    stats["max_statement_rows"], self._statement_rows)
                for row in rows:
                    for value in row:
                        record_value(value)
                return rows

            def execute(self, sql, parameters=()):
                return RecordingCursor(
                    self._cursor.execute(sql, parameters))

            def executemany(self, sql, sequence):
                return RecordingCursor(
                    self._cursor.executemany(sql, sequence))

            def fetchall(self):
                return self._take(self._cursor.fetchall())

            def fetchmany(self, size=None):
                rows = (self._cursor.fetchmany() if size is None
                        else self._cursor.fetchmany(size))
                return self._take(rows)

            def fetchone(self):
                row = self._cursor.fetchone()
                if row is not None:
                    self._take([row])
                return row

            def __iter__(self):
                for row in self._cursor:
                    self._take([row])
                    yield row

            def __getattr__(self, name):
                return getattr(self._cursor, name)

        class RecordingConnection:
            def __init__(self, connection):
                object.__setattr__(self, "_connection", connection)

            def execute(self, sql, parameters=()):
                return RecordingCursor(
                    self._connection.execute(sql, parameters))

            def executemany(self, sql, sequence):
                return RecordingCursor(
                    self._connection.executemany(sql, sequence))

            def executescript(self, sql):
                return RecordingCursor(
                    self._connection.executescript(sql))

            def cursor(self):
                return RecordingCursor(self._connection.cursor())

            def __getattr__(self, name):
                return getattr(self._connection, name)

            def __setattr__(self, name, value):
                setattr(self._connection, name, value)

        real_connect = sqlite3.connect

        def recording_connect(*args, **kw):
            return RecordingConnection(real_connect(*args, **kw))

        return RecordingConnection, recording_connect

    def _new_stats(self):
        return {"rows": 0, "max_statement_rows": 0,
                "text_bytes": 0, "max_value_bytes": 0}

    def _recorded_estimate(self, **kwargs):
        """Run duration_estimate behind the recording proxy and
        return (estimate, stats). Unlike SQL-text matching, this
        observes the transfer itself."""
        import sqlite3
        stats = self._new_stats()
        _, recording_connect = self._make_recorder(stats)
        with mock.patch.object(sqlite3, "connect", recording_connect):
            estimate = duration_estimate(self.store, "scan",
                                         policy=POLICY, **kwargs)
        return estimate, stats

    def _assert_bounded_rejection(self, estimate, stats):
        self.assertIsNone(estimate)
        # Legitimate rejected reads move only rowids, state strings
        # and integers. The bounds tie to the design constants: no
        # statement may return more than the candidate probe
        # (ESTIMATE_MAX_LANE_ENTRIES + 1), the whole read stays
        # within a few hundred rows and 1 KiB of text, and no single
        # value may be payload-sized.
        probe = Store.ESTIMATE_MAX_LANE_ENTRIES + 1
        self.assertLessEqual(stats["max_statement_rows"], probe, stats)
        self.assertLessEqual(stats["rows"], 250, stats)
        self.assertLessEqual(stats["text_bytes"], 1024, stats)
        self.assertLessEqual(stats["max_value_bytes"], 512, stats)

    def test_entry_overflow_transfers_no_payload(self):
        # Far more qualifying jobs than the probe: removing the
        # production LIMIT would show up in the row counts.
        for i in range(1000):
            self.submit(f"k-{i}", {"operation": "scan"})
        self._assert_bounded_rejection(*self._recorded_estimate())

    def test_oversized_rejection_transfers_no_payload(self):
        job = self.submit("k-big", {"operation": "scan"})
        self.set_payload(job["id"], json.dumps(
            {"operation": "scan", "pad": "x" * (70 * 1024)}))
        self._assert_bounded_rejection(*self._recorded_estimate())

    def test_aggregate_rejection_transfers_no_payload(self):
        for i in range(20):
            job = self.submit(f"k-{i}", {"operation": "scan"})
            self.set_payload(job["id"], json.dumps(
                {"operation": "scan", "pad": "x" * (60 * 1024)}))
        self._assert_bounded_rejection(*self._recorded_estimate())

    def test_boundary_rejection_transfers_no_payload(self):
        for i in range(1000):
            self.submit(f"k-ahead-{i}", {"operation": "scan"})
        target = self.submit("k-target", {"operation": "scan"})
        estimate, stats = self._recorded_estimate(
            before_job_id=target["id"], owner=self.owner)
        self._assert_bounded_rejection(estimate, stats)

    def test_accepted_lane_transfer_is_observed(self):
        # Vacuity guard: the recorder must SEE the accepted lane's
        # payload transfer — at least the payload's own serialized
        # length — or the rejection bounds would prove nothing.
        job = self.submit("k-1", {"operation": "scan"})
        payload_text = json.dumps({"operation": "scan",
                                   "pad": "x" * 500})
        self.set_payload(job["id"], payload_text)
        estimate, stats = self._recorded_estimate()
        self.assertIsNotNone(estimate)
        self.assertGreaterEqual(
            stats["text_bytes"], len(payload_text.encode("utf-8")))

    def test_recorder_covers_cursor_access_patterns(self):
        # The proxy itself: connection.cursor() + fetchmany, plain
        # iteration, and a payload hidden behind a duplicate column
        # name must all be counted (Astra round 6).
        import sqlite3
        job = self.submit("k-1", {"operation": "scan"})
        payload_text = json.dumps({"operation": "scan",
                                   "pad": "x" * 500})
        self.set_payload(job["id"], payload_text)
        stats = self._new_stats()
        RecordingConnection, _ = self._make_recorder(stats)
        connection = RecordingConnection(
            sqlite3.connect(self.store.path))
        connection.row_factory = sqlite3.Row
        cursor = connection.cursor()
        rows = cursor.execute(
            "SELECT rowid, payload AS rowid FROM jobs").fetchmany(10)
        self.assertEqual(len(rows), 1)
        self.assertGreaterEqual(
            stats["text_bytes"], len(payload_text.encode("utf-8")))
        before = stats["rows"]
        iterated = [row for row in connection.execute(
            "SELECT state FROM jobs")]
        self.assertEqual(len(iterated), 1)
        self.assertGreater(stats["rows"], before)
        connection.close()

    def test_rows_behind_the_target_do_not_affect_its_estimate(self):
        # The boundary basis covers only rows ahead of the target:
        # an oversized payload and an entry overflow BEHIND it are
        # not its problem and must not suppress its estimate.
        target = self.submit("k-target", {"operation": "scan"})
        big = self.submit("k-big", {"operation": "scan"})
        self.set_payload(big["id"], json.dumps(
            {"operation": "scan", "pad": "x" * (70 * 1024)}))
        for i in range(105):
            self.submit(f"k-late-{i}", {"operation": "scan"})
        estimate = duration_estimate(
            self.store, "scan", policy=POLICY,
            before_job_id=target["id"], owner=self.owner)
        self.assertIsNotNone(estimate)
        self.assertEqual(estimate["jobs_ahead"], 0)

    def test_unmeasured_operations_have_no_estimate(self):
        self.assertIsNone(duration_estimate(self.store, "scan",
                                            policy=NO_POLICY))
        self.assertIsNone(duration_estimate(self.store, "irradiation", 9,
                                            policy=NO_POLICY))
        self.assertIsNotNone(duration_estimate(self.store, "scan",
                                               policy=SCAN_ONLY))
        # An unmeasured operation ahead removes the basis behind it.
        self.submit("k-1", {"operation": "irradiation",
                            "irradiation_count": 9})
        self.assertIsNone(duration_estimate(self.store, "scan",
                                            policy=SCAN_ONLY))
        # And the mirror image: a scan ahead blocks an irradiation
        # estimate under an irradiation-only policy.
        self.submit("k-2", {"operation": "scan"})
        self.assertIsNone(duration_estimate(self.store, "irradiation", 9,
                                            policy=IRRADIATION_ONLY))

    def test_estimate_for_existing_job_requires_queued_state(self):
        mine = self.submit("k-1", {"operation": "scan"})
        self.assertIsNotNone(duration_estimate(
            self.store, "scan", policy=POLICY,
            before_job_id=mine["id"], owner=self.owner))
        claimed = self.store.claim_next()
        self.assertEqual(claimed["id"], mine["id"])
        self.assertIsNone(duration_estimate(
            self.store, "scan", policy=POLICY,
            before_job_id=mine["id"], owner=self.owner))
        self.store.finish(mine["id"], "completed", {"mode": "synthetic"})
        self.assertIsNone(duration_estimate(
            self.store, "scan", policy=POLICY,
            before_job_id=mine["id"], owner=self.owner))
        cancelled = self.submit("k-2", {"operation": "scan"})
        self.set_state(cancelled["id"], "cancelled")
        self.assertIsNone(duration_estimate(
            self.store, "scan", policy=POLICY,
            before_job_id=cancelled["id"], owner=self.owner))

    def test_before_job_id_limits_the_basis_to_jobs_ahead(self):
        self.submit("k-1", {"operation": "scan"})
        mine = self.submit("k-2", {"operation": "scan"}, owner=self.other)
        self.submit("k-3", {"operation": "scan"})
        estimate = duration_estimate(self.store, "scan", policy=POLICY,
                                     before_job_id=mine["id"],
                                     owner=self.other)
        self.assertEqual(estimate["jobs_ahead"], 1)
        self.assertEqual(estimate["min_seconds"], 80)   # (50+50)*0.8
        self.assertEqual(estimate["max_seconds"], 160)  # (50+50)*1.6

    def test_device_setting_is_never_the_basis(self):
        # Payloads carry the 9 s/shot device setting; varying it
        # must not move the estimate, which rests on the policy's
        # measured values only.
        job = self.submit("k-1", {"operation": "irradiation",
                                  "irradiation_count": 9,
                                  "execution_settings": {"duration_sec": 9}})
        first = duration_estimate(self.store, "scan", policy=POLICY)
        self.set_payload(job["id"], json.dumps(
            {"operation": "irradiation", "irradiation_count": 9,
             "execution_settings": {"duration_sec": 999}}))
        second = duration_estimate(self.store, "scan", policy=POLICY)
        self.assertEqual(first, second)

    def test_input_validation(self):
        with self.assertRaises(ValueError):
            duration_estimate(self.store, "reading", policy=POLICY)
        for bad in (None, 0, 100, "9", 9.5, True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                duration_estimate(self.store, "irradiation", bad,
                                  policy=POLICY)

    def test_pure_computation_rejects_unreadable_stored_counts(self):
        for count in (500, 0, None, True, 9.5, "9"):
            ahead = [{"state": "queued", "operation": "irradiation",
                      "irradiation_count": count}]
            self.assertIsNone(
                estimate_from_lane(POLICY, "scan", None, ahead),
                msg=repr(count))

    def test_pure_computation_collapsed_range_is_no_estimate(self):
        point = EstimatePolicy(scan_seconds=50,
                               irradiation_seconds_per_shot=11,
                               range_low_factor=1.0, range_high_factor=1.0)
        self.assertIsNone(estimate_from_lane(point, "scan", None, []))


class WorkerContactApiTests(unittest.TestCase):
    """The /worker/claim poll is the estimator's presence basis."""

    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537,
                                           key_size=2048)

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name) / "worker.sqlite")
        verifier = JWTVerifier(
            public_key=self.key.public_key(), issuer="https://issuer.test",
            audience="https://queue.test", required_scope=WORKER_SCOPE)
        self.client = TestClient(create_worker_app(
            self.store, verifier, allowed_subjects={"worker-a"}))

    def token(self, subject="worker-a"):
        claims = {"iss": "https://issuer.test", "aud": "https://queue.test",
                  "sub": subject, "client_id": "worker",
                  "scope": WORKER_SCOPE, "iat": int(time.time()),
                  "exp": int(time.time()) + 600}
        return jwt.encode(claims, self.key, algorithm="RS256",
                          headers={"typ": "at+jwt"})

    def test_authorized_claim_poll_records_contact(self):
        self.assertIsNone(self.store.worker_last_seen())
        response = self.client.post(
            "/worker/claim",
            headers={"Authorization": "Bearer " + self.token()})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNotNone(self.store.worker_last_seen())

    def test_unauthorized_poll_records_nothing(self):
        response = self.client.post("/worker/claim")
        self.assertEqual(response.status_code, 401)
        self.assertIsNone(self.store.worker_last_seen())

    def test_failed_claim_records_no_contact(self):
        # A misconfigured-but-authorized worker whose claims always
        # fail must not keep the presence basis alive (Astra round
        # 2): contact follows SUCCESSFUL operations only.
        response = self.client.post(
            "/worker/claim", json={"diode_serial_hash": "bad"},
            headers={"Authorization": "Bearer " + self.token()})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIsNone(self.store.worker_last_seen())

    def test_disallowed_subject_records_no_contact(self):
        response = self.client.post(
            "/worker/claim",
            headers={"Authorization": "Bearer " + self.token("worker-b")})
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIsNone(self.store.worker_last_seen())

    def test_execution_heartbeat_renews_contact(self):
        # While a job executes, claim polls pause — potentially far
        # beyond the presence window — so a successful heartbeat
        # must also record contact.
        owner = self.store.register_identity("https://issuer.test",
                                             "job-owner")
        job_id = str(uuid.uuid4())
        with self.store.transaction() as db:
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,NULL)",
                       (job_id, owner, "hb-1",
                        json.dumps({"operation": "scan"}),
                        "fixture-digest", 0, "queued"))
        headers = {"Authorization": "Bearer " + self.token()}
        claimed = self.client.post("/worker/claim", headers=headers)
        self.assertEqual(claimed.status_code, 200, claimed.text)
        job = claimed.json()["job"]
        self.assertEqual(job["id"], job_id)
        with self.store.transaction() as db:
            db.execute("DELETE FROM worker_contact")
        self.assertIsNone(self.store.worker_last_seen())
        response = self.client.post(
            "/worker/heartbeat", headers=headers,
            json={"job_id": job_id,
                  "claim_generation": job.get("claim_generation")})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNotNone(self.store.worker_last_seen())


class EstimateApiTests(unittest.TestCase):
    ISSUER = "https://issuer.example.test/auth/v1"
    AUDIENCE = "https://si.example.test/mcp"

    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537,
                                           key_size=2048)

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name) / "app.sqlite")
        self.service = Service(self.store, CATALOG)
        self.owner_a = self.store.register_identity(self.ISSUER, "user-a")
        with self.store.transaction() as db:
            db.execute("UPDATE accounts SET balance=500 WHERE id=?",
                       (self.owner_a,))
        # A worker is polling (presence basis for estimates).
        self.store.note_worker_contact()

    def token(self, subject):
        claims = {"iss": self.ISSUER, "aud": self.AUDIENCE, "sub": subject,
                  "client_id": "si-app", "scope": "aiqs:use",
                  "iat": int(time.time()), "exp": int(time.time()) + 600}
        return jwt.encode(claims, self.key, algorithm="RS256",
                          headers={"typ": "at+jwt"})

    def build(self, **kwargs):
        user = JWTVerifier(public_key=self.key.public_key(),
                           issuer=self.ISSUER, audience=self.AUDIENCE)
        worker = JWTVerifier(public_key=self.key.public_key(),
                             issuer=self.ISSUER,
                             audience="https://si.example.test/worker",
                             required_scope="aiqs:worker")
        return create_application(self.store, user, worker,
                                  allowed_workers={"synthetic-worker"},
                                  service=self.service, **kwargs)

    def open(self, enabled="1", **kwargs):
        env = {} if enabled is None else {"AIQS_SI_APP_ENABLED": enabled}
        with mock.patch.dict(os.environ, env):
            if enabled is None:
                os.environ.pop("AIQS_SI_APP_ENABLED", None)
            app = self.build(**kwargs)
        return TestClient(app, base_url="http://127.0.0.1:8000",
                          raise_server_exceptions=False)

    def auth(self, subject="user-a"):
        return {"Authorization": "Bearer " + self.token(subject)}

    def register_target(self, client):
        response = client.post("/api/app/targets", headers=self.auth(),
                               json={"request_key": "target-1",
                                     "name": "Fixture Target",
                                     "photo_base64": PHOTO})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["target_id"]

    def submit_scan(self, client, key="app-scan-1"):
        target_id = self.register_target(client)
        response = client.post("/api/app/jobs/scan", headers=self.auth(),
                               json={"request_key": key,
                                     "target_ids": [target_id],
                                     "intention": "app fixture intention",
                                     "self_love_joy": JOY})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_estimate_endpoint_scan_and_irradiation(self):
        with self.open(estimate_policy=POLICY) as client:
            scan = client.get("/api/app/estimate?operation=scan",
                              headers=self.auth())
            self.assertEqual(scan.status_code, 200, scan.text)
            self.assertEqual(scan.json(), {
                "operation": "scan",
                "estimate": {"min_seconds": 40, "max_seconds": 80,
                             "includes_queue_wait": True, "jobs_ahead": 0}})
            irr = client.get("/api/app/estimate?operation=irradiation&count=9",
                             headers=self.auth())
            self.assertEqual(irr.json()["estimate"]["min_seconds"], 79)
            self.assertEqual(irr.json()["estimate"]["max_seconds"], 159)

    def test_estimate_endpoint_validation_and_auth(self):
        with self.open(estimate_policy=POLICY) as client:
            self.assertEqual(client.get(
                "/api/app/estimate?operation=scan").status_code, 401)
            for query, error in [("", "invalid_operation"),
                                 ("operation=reading", "invalid_operation"),
                                 ("operation=irradiation", "invalid_count"),
                                 ("operation=irradiation&count=0",
                                  "invalid_count"),
                                 ("operation=irradiation&count=100",
                                  "invalid_count"),
                                 ("operation=irradiation&count=abc",
                                  "invalid_count"),
                                 ("operation=irradiation&count=3.5",
                                  "invalid_count")]:
                response = client.get(f"/api/app/estimate?{query}",
                                      headers=self.auth())
                self.assertEqual(response.status_code, 400, query)
                self.assertEqual(response.json()["error"], error, query)

    def test_estimate_endpoint_404_while_rollout_off(self):
        with self.open(enabled=None, estimate_policy=POLICY) as client:
            self.assertEqual(client.get("/api/app/estimate?operation=scan",
                                        headers=self.auth()).status_code, 404)

    def test_no_policy_means_null_estimate_everywhere(self):
        with self.open(estimate_policy=NO_POLICY) as client:
            for query in ("operation=scan",
                          "operation=irradiation&count=9"):
                response = client.get(f"/api/app/estimate?{query}",
                                      headers=self.auth())
                self.assertEqual(response.status_code, 200)
                self.assertIsNone(response.json()["estimate"], query)
            session = client.get("/api/app/session", headers=self.auth())
            self.assertEqual(session.json()["estimate"],
                             {"scan": False, "irradiation": False})
            submitted = self.submit_scan(client)
            self.assertIsNone(submitted["estimate"])
            view = client.get(f"/api/app/jobs/{submitted['job_id']}",
                              headers=self.auth()).json()
            self.assertIsNone(view["estimate"])
            Worker(self.store, SyntheticDevice()).run_one()
            prepared = client.post("/api/app/jobs/irradiate/prepare",
                                   headers=self.auth(),
                                   json={"scan_id": submitted["job_id"],
                                         "count": 3})
            self.assertEqual(prepared.status_code, 200, prepared.text)
            self.assertIsNone(prepared.json()["estimate"])
            response = client.post("/api/app/jobs/irradiate",
                                   headers=self.auth(),
                                   json={"request_key": "app-irr-np",
                                         "confirmation_id":
                                         prepared.json()["confirmation_id"]})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIsNone(response.json()["estimate"])

    def test_no_worker_contact_means_null_estimate(self):
        with self.store.transaction() as db:
            db.execute("DELETE FROM worker_contact")
        with self.open(estimate_policy=POLICY) as client:
            response = client.get("/api/app/estimate?operation=scan",
                                  headers=self.auth())
            self.assertEqual(response.status_code, 200)
            self.assertIsNone(response.json()["estimate"])

    def test_session_reports_estimate_availability(self):
        with self.open(estimate_policy=POLICY) as client:
            session = client.get("/api/app/session", headers=self.auth())
            self.assertEqual(session.json()["estimate"],
                             {"scan": True, "irradiation": True})
        with self.open(estimate_policy=IRRADIATION_ONLY) as client:
            session = client.get("/api/app/session", headers=self.auth())
            self.assertEqual(session.json()["estimate"],
                             {"scan": False, "irradiation": True})
            scan = client.get("/api/app/estimate?operation=scan",
                              headers=self.auth())
            self.assertIsNone(scan.json()["estimate"])

    def test_scan_submission_and_job_view_carry_the_estimate(self):
        with self.open(estimate_policy=POLICY) as client:
            submitted = self.submit_scan(client)
            self.assertEqual(submitted["estimate"]["min_seconds"], 40)
            self.assertEqual(submitted["estimate"]["max_seconds"], 80)
            view = client.get(f"/api/app/jobs/{submitted['job_id']}",
                              headers=self.auth()).json()
            self.assertEqual(view["state"], "queued")
            self.assertEqual(view["estimate"]["jobs_ahead"], 0)
            # A second scan queued behind the first sees the wait.
            behind = client.get("/api/app/estimate?operation=scan",
                                headers=self.auth()).json()
            self.assertEqual(behind["estimate"]["jobs_ahead"], 1)
            self.assertEqual(behind["estimate"]["min_seconds"], 80)
            self.assertEqual(behind["estimate"]["max_seconds"], 160)
            Worker(self.store, SyntheticDevice()).run_one()
            done = client.get(f"/api/app/jobs/{submitted['job_id']}",
                              headers=self.auth()).json()
            self.assertEqual(done["state"], "completed")
            self.assertIsNone(done["estimate"])

    def test_replayed_submission_has_no_estimate(self):
        # Replaying a finished scan must not dress it in a fresh
        # estimate: the job is terminal, there is nothing to wait
        # for (Astra round 1, finding 3).
        with self.open(estimate_policy=POLICY) as client:
            submitted = self.submit_scan(client)
            Worker(self.store, SyntheticDevice()).run_one()
            target_id = self.register_target(client)
            replay = client.post("/api/app/jobs/scan", headers=self.auth(),
                                 json={"request_key": "app-scan-1",
                                       "target_ids": [target_id],
                                       "intention": "app fixture intention",
                                       "self_love_joy": JOY})
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json()["job_id"], submitted["job_id"])
            self.assertEqual(replay.json()["state"], "completed")
            self.assertIsNone(replay.json()["estimate"])

    def test_prepare_and_irradiate_carry_the_estimate(self):
        with self.open(estimate_policy=POLICY) as client:
            submitted = self.submit_scan(client)
            Worker(self.store, SyntheticDevice()).run_one()
            prepared = client.post("/api/app/jobs/irradiate/prepare",
                                   headers=self.auth(),
                                   json={"scan_id": submitted["job_id"],
                                         "count": 3})
            self.assertEqual(prepared.status_code, 200, prepared.text)
            self.assertEqual(prepared.json()["estimate"]["min_seconds"], 26)
            self.assertEqual(prepared.json()["estimate"]["max_seconds"], 53)
            response = client.post("/api/app/jobs/irradiate",
                                   headers=self.auth(),
                                   json={"request_key": "app-irr-1",
                                         "confirmation_id":
                                         prepared.json()["confirmation_id"]})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["estimate"]["min_seconds"], 26)
            view = client.get(f"/api/app/jobs/{response.json()['job_id']}",
                              headers=self.auth()).json()
            self.assertEqual(view["estimate"]["max_seconds"], 53)

    def test_blocked_lane_answers_null_not_a_number(self):
        with self.open(estimate_policy=POLICY) as client:
            submitted = self.submit_scan(client)
            with self.store.transaction() as db:
                db.execute("UPDATE jobs SET state='unknown' WHERE id=?",
                           (submitted["job_id"],))
            response = client.get("/api/app/estimate?operation=scan",
                                  headers=self.auth())
            self.assertIsNone(response.json()["estimate"])

    def test_unknown_job_behind_removes_the_targets_estimate(self):
        # The unresolved-execution block is global: an unknown job
        # BEHIND the viewed job still removes its estimate.
        with self.open(estimate_policy=POLICY) as client:
            submitted = self.submit_scan(client)
            view = client.get(f"/api/app/jobs/{submitted['job_id']}",
                              headers=self.auth()).json()
            self.assertIsNotNone(view["estimate"])
            with self.store.transaction() as db:
                db.execute(
                    "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,NULL)",
                    (str(uuid.uuid4()), self.owner_a, "later-unknown",
                     json.dumps({"operation": "scan"}),
                     "fixture-digest", 0, "unknown"))
            view = client.get(f"/api/app/jobs/{submitted['job_id']}",
                              headers=self.auth()).json()
            self.assertEqual(view["state"], "queued")
            self.assertIsNone(view["estimate"])

    def _prepare_irradiation(self, client, scan_job_id, key):
        prepared = client.post("/api/app/jobs/irradiate/prepare",
                               headers=self.auth(),
                               json={"scan_id": scan_job_id, "count": 3})
        self.assertEqual(prepared.status_code, 200, prepared.text)
        response = client.post(
            "/api/app/jobs/irradiate", headers=self.auth(),
            json={"request_key": key,
                  "confirmation_id": prepared.json()["confirmation_id"]})
        self.assertEqual(response.status_code, 200, response.text)
        return prepared.json()["confirmation_id"], response.json()

    def test_irradiation_replay_in_later_states_has_no_estimate(self):
        # The irradiate handler returns the existing job for a
        # replayed request key + confirmation, so the submission
        # carrier itself must stop estimating once the job leaves
        # the queued state (Astra round 3).
        with self.open(estimate_policy=POLICY) as client:
            submitted = self.submit_scan(client)
            Worker(self.store, SyntheticDevice()).run_one()
            confirmation_id, first = self._prepare_irradiation(
                client, submitted["job_id"], "app-irr-replay")
            job_id = first["job_id"]
            self.assertEqual(first["state"], "queued")
            self.assertIsNotNone(first["estimate"])
            body = {"request_key": "app-irr-replay",
                    "confirmation_id": confirmation_id}

            def replay():
                response = client.post("/api/app/jobs/irradiate",
                                       headers=self.auth(), json=body)
                self.assertEqual(response.status_code, 200,
                                 response.text)
                self.assertEqual(response.json()["job_id"], job_id)
                return response.json()

            claimed = self.store.claim_next()
            self.assertEqual(claimed["id"], job_id)
            running = replay()
            self.assertEqual(running["state"], "running")
            self.assertIsNone(running["estimate"])
            self.store.finish(job_id, "failed", {"mode": "synthetic"})
            failed = replay()
            self.assertEqual(failed["state"], "failed")
            self.assertIsNone(failed["estimate"])

            # A second irradiation, driven to completion and then
            # replayed; a third, cancelled and replayed.
            confirmation_2, second = self._prepare_irradiation(
                client, submitted["job_id"], "app-irr-replay-2")
            Worker(self.store, SyntheticDevice()).run_one()
            done = client.post(
                "/api/app/jobs/irradiate", headers=self.auth(),
                json={"request_key": "app-irr-replay-2",
                      "confirmation_id": confirmation_2})
            self.assertEqual(done.status_code, 200, done.text)
            self.assertEqual(done.json()["job_id"], second["job_id"])
            self.assertEqual(done.json()["state"], "completed")
            self.assertIsNone(done.json()["estimate"])
            confirmation_id, third = self._prepare_irradiation(
                client, submitted["job_id"], "app-irr-replay-3")
            cancelled = client.post(
                f"/api/app/jobs/{third['job_id']}/cancel",
                headers=self.auth())
            self.assertEqual(cancelled.status_code, 200, cancelled.text)
            replayed = client.post(
                "/api/app/jobs/irradiate", headers=self.auth(),
                json={"request_key": "app-irr-replay-3",
                      "confirmation_id": confirmation_id})
            self.assertEqual(replayed.status_code, 200, replayed.text)
            self.assertEqual(replayed.json()["job_id"], third["job_id"])
            self.assertEqual(replayed.json()["state"], "cancelled")
            self.assertIsNone(replayed.json()["estimate"])

    def test_irradiation_view_loses_estimate_when_not_queued(self):
        with self.open(estimate_policy=POLICY) as client:
            submitted = self.submit_scan(client)
            Worker(self.store, SyntheticDevice()).run_one()
            prepared = client.post("/api/app/jobs/irradiate/prepare",
                                   headers=self.auth(),
                                   json={"scan_id": submitted["job_id"],
                                         "count": 3})
            response = client.post("/api/app/jobs/irradiate",
                                   headers=self.auth(),
                                   json={"request_key": "app-irr-states",
                                         "confirmation_id":
                                         prepared.json()["confirmation_id"]})
            self.assertEqual(response.status_code, 200, response.text)
            job_id = response.json()["job_id"]
            self.assertIsNotNone(response.json()["estimate"])
            for state in ("running", "completed", "failed", "cancelled"):
                with self.store.transaction() as db:
                    db.execute("UPDATE jobs SET state=? WHERE id=?",
                               (state, job_id))
                view = client.get(f"/api/app/jobs/{job_id}",
                                  headers=self.auth()).json()
                self.assertEqual(view["state"], state)
                self.assertIsNone(view["estimate"], state)

    def test_deeply_nested_lane_payload_does_not_break_views(self):
        # Another job's corrupt payload removes the basis; it must
        # not 500 the endpoint or degrade an unrelated job view.
        with self.open(estimate_policy=POLICY) as client:
            submitted = self.submit_scan(client)
            with self.store.transaction() as db:
                db.execute(
                    "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,NULL)",
                    (str(uuid.uuid4()), self.owner_a, "corrupt-1",
                     "[" * 5000 + "]" * 5000, "fixture-digest", 0,
                     "queued"))
            response = client.get("/api/app/estimate?operation=scan",
                                  headers=self.auth())
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIsNone(response.json()["estimate"])
            # The corrupt job sits BEHIND the submitted scan, so the
            # scan's own view keeps its estimate: corruption only
            # removes the basis it actually blocks.
            view = client.get(f"/api/app/jobs/{submitted['job_id']}",
                              headers=self.auth())
            self.assertEqual(view.status_code, 200, view.text)
            self.assertEqual(view.json()["state"], "queued")
            self.assertEqual(view.json()["estimate"]["min_seconds"], 40)


if __name__ == "__main__":
    unittest.main()
