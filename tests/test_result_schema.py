"""Regression tests for the strict public result schema
(result_schema; Astra final round #2).

The canary is a nested private object placed in a public-schema
field by a (buggy or compromised) worker. It must be rejected
before persistence by Store.finish, and — for rows written before
the schema existed — it must be sanitized out of both public
surfaces: public_job_view (MCP / app / cancellation) and
execution_proof (MCP / app / internal NPC path).
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from aiqs_si.store import Store
from aiqs_si.serializers import public_job_view
from aiqs_si.result_schema import validate_worker_result

CANARY = {"private": {"catalog_digest": "CANARY", "threshold": 123}}


class ResultSchemaTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = Store(Path(self.folder.name) / "queue.sqlite")
        self.owner = self.store.create_test_account(5000)

    def submit_claimed(self, key, payload, cost=0):
        job = self.store.submit(self.owner, key, payload, cost=cost)
        self.store.claim_next(worker="w1")
        return job

    # --- finish-time validation (fail-closed before persistence) ---

    def test_finish_rejects_nested_mode_canary(self):
        job = self.submit_claimed("canary-mode", {"operation": "scan"})
        with self.assertRaises(ValueError):
            self.store.finish(job["id"], "completed",
                              {"mode": CANARY, "scan": {}}, worker="w1")
        # Nothing persisted: the job is still running and result-free.
        row = self.store.get(self.owner, job["id"])
        self.assertEqual(row["state"], "running")
        self.assertIsNone(row.get("result"))

    def test_finish_rejects_free_form_reason_and_mode(self):
        job = self.submit_claimed("free-reason", {"operation": "scan"})
        for result in ({"reason": "PRIVATE DIAGNOSTIC TEXT"},
                       {"mode": "anything-goes"},
                       {"reason": {"nested": True}}):
            with self.assertRaises(ValueError):
                self.store.finish(job["id"], "failed", result, worker="w1")
        self.assertEqual(self.store.get(self.owner, job["id"])["state"], "running")

    def test_finish_rejects_mistyped_counts_and_flags(self):
        job = self.submit_claimed("mistyped", {"operation": "scan"})
        for result in ({"completed_count": "3"},
                       {"completed_count": CANARY},
                       {"completed_count": -1},
                       {"completed_count": True},
                       {"stop_command_acknowledged": "yes"},
                       {"physical_output_verified": 1},
                       {"scan": "not-an-object"},
                       {"catalog_digest": 42}):
            with self.assertRaises(ValueError):
                self.store.finish(job["id"], "failed", result, worker="w1")

    def test_finish_accepts_legitimate_result_shapes(self):
        # Hardware irradiation shape, including private payload keys
        # (entry_ids, shots, device observation) that never publish.
        job = self.submit_claimed("hw-shape", {"operation": "scan"})
        self.store.finish(job["id"], "completed", {
            "mode": "hardware", "entry_ids": ["1"], "shots": [{"n": 1}],
            "device_observation": {"bits": 3}, "completed_count": 3,
            "requested_count": 3, "stop_command_acknowledged": True,
            "physical_output_verified": False}, worker="w1")
        self.assertEqual(self.store.get(self.owner, job["id"])["state"], "completed")
        # Worker failure shape.
        job2 = self.submit_claimed("fail-shape", {"operation": "scan"})
        self.store.finish(job2["id"], "failed",
                          {"reason": "catalog_unavailable_or_invalid"}, worker="w1")
        self.assertEqual(self.store.get(self.owner, job2["id"])["state"], "failed")

    def test_validate_worker_result_rejects_non_object(self):
        for bad in (None, [], "result", 42):
            with self.assertRaises(ValueError):
                validate_worker_result(bad)

    # --- scan entries (Astra diff review: nested scan channel) ---

    def test_finish_rejects_poisoned_scan_entries(self):
        job = self.submit_claimed("scan-poison", {"operation": "scan"})
        digest_int = int("ab" * 32, 16)  # a whole digest as one integer
        for scan in (
                {"final_entries": [{"id": "one", "hits": digest_int, "rank": 1}]},
                {"final_entries": [{"id": "one", "hits": 3, "rank": 0}]},
                {"final_entries": [{"id": ["x"], "hits": 3, "rank": 1}]},
                {"final_entries": [{"id": "one", "hits": True, "rank": 1}]},
                {"final_entries": ["not-an-object"]},
                {"observer_coordinate_applied": "yes"}):
            with self.assertRaises(ValueError):
                self.store.finish(job["id"], "completed",
                                  {"mode": "synthetic", "scan": scan}, worker="w1")
        self.assertEqual(self.store.get(self.owner, job["id"])["state"], "running")

    def test_finish_accepts_legitimate_scan_result(self):
        job = self.submit_claimed("scan-ok", {"operation": "scan"})
        self.store.finish(job["id"], "completed", {
            "mode": "synthetic",
            "scan": {"observer_coordinate_applied": True,
                     "final_entries": [{"id": "one", "hits": 42, "rank": 1},
                                       {"id": "two", "hits": 7, "rank": 2}]},
        }, worker="w1")
        self.assertEqual(self.store.get(self.owner, job["id"])["state"], "completed")

    def test_finish_rejects_rank_beyond_the_entry_list(self):
        job = self.submit_claimed("rank-cap", {"operation": "scan"})
        with self.assertRaises(ValueError):
            self.store.finish(job["id"], "completed", {
                "mode": "synthetic",
                "scan": {"final_entries": [{"id": "one", "hits": 1, "rank": 2}]},
            }, worker="w1")

    def test_resolved_job_acknowledges_stale_result_with_expired_claim(self):
        # Astra diff review round 2 (Item 2, MEDIUM): the full
        # recovery sequence — schema rejection, claim expiry,
        # operator resolution, stale outbox retry. The retry must be
        # acknowledged on claim IDENTITY alone (generation 1, lease
        # expired); a mismatched generation is still refused, and
        # expiry still blocks finishes for unresolved jobs.
        job = self.submit_claimed("resolved-expired", {"operation": "scan"})
        with self.assertRaises(ValueError):
            self.store.finish(job["id"], "completed", {"reason": "FREE FORM"},
                              worker="w1", claim_generation=1)
        db = sqlite3.connect(self.store.path)
        db.execute("UPDATE worker_claims SET expires_at=0 WHERE job=?",
                   (job["id"],))
        db.commit()
        db.close()
        with self.assertRaises(PermissionError):
            self.store.finish(job["id"], "completed", {"reason": "FREE FORM"},
                              worker="w1", claim_generation=1)
        self.store.finish(job["id"], "unknown",
                          {"reason": "execution_outcome_unknown"}, worker="w1")
        self.store.resolve_unknown(job["id"], completed_count=0,
                                   stop_evidence_digest="a" * 64)
        self.store.finish(job["id"], "completed", {"reason": "FREE FORM"},
                          worker="w1", claim_generation=1)
        row = self.store.get(self.owner, job["id"])
        self.assertEqual(row["state"], "cancelled")
        self.assertEqual(row["result"]["reason"], "operator_reconciled")
        with self.assertRaises(PermissionError):
            self.store.finish(job["id"], "completed", {"reason": "FREE FORM"},
                              worker="w1", claim_generation=2)

    def test_resolved_scan_receipt_acknowledges_with_expired_claim(self):
        # Astra diff review round 3 (Item 2, MEDIUM): the scan
        # receipt path (reconcile_scan_receipt) has the same
        # recovery rule as finish — identity-only acknowledgement
        # for a resolved job, strict freshness otherwise.
        job = self.submit_claimed("receipt-expired", {"operation": "scan"})
        self.store.finish(job["id"], "unknown",
                          {"reason": "execution_outcome_unknown"}, worker="w1")
        self.store.resolve_unknown(job["id"], completed_count=0,
                                   stop_evidence_digest="a" * 64)
        db = sqlite3.connect(self.store.path)
        db.execute("UPDATE worker_claims SET expires_at=0 WHERE job=?",
                   (job["id"],))
        db.commit()
        db.close()
        self.store.reconcile_scan_receipt(job["id"], "f" * 64,
                                          worker="w1", claim_generation=1)
        # The identity exception is narrow: a different worker or a
        # mismatched generation is refused even for a resolved job.
        with self.assertRaises(PermissionError):
            self.store.reconcile_scan_receipt(job["id"], "f" * 64,
                                              worker="w2", claim_generation=1)
        with self.assertRaises(PermissionError):
            self.store.reconcile_scan_receipt(job["id"], "f" * 64,
                                              worker="w1", claim_generation=2)
        # Control: an unresolved job with an expired claim still
        # raises PermissionError on the same path.
        job2 = self.submit_claimed("receipt-live", {"operation": "scan"})
        db = sqlite3.connect(self.store.path)
        db.execute("UPDATE worker_claims SET expires_at=0 WHERE job=?",
                   (job2["id"],))
        db.commit()
        db.close()
        with self.assertRaises(PermissionError):
            self.store.reconcile_scan_receipt(job2["id"], "f" * 64,
                                              worker="w1", claim_generation=1)

    def test_resolved_job_acknowledges_schema_invalid_stale_result(self):
        # Astra diff review (Item 2, MEDIUM): after an authoritative
        # operator resolution, a stale outbox result is acknowledged
        # as the no-op it is even when its body would fail the
        # schema — otherwise the worker's outbox wedges forever on a
        # result that no longer matters.
        job = self.submit_claimed("resolved-stale", {"operation": "scan"})
        self.store.finish(job["id"], "unknown",
                          {"reason": "execution_outcome_unknown"}, worker="w1")
        self.store.resolve_unknown(job["id"], completed_count=0,
                                   stop_evidence_digest="a" * 64)
        self.store.finish(job["id"], "completed",
                          {"reason": "FREE FORM STALE TEXT"}, worker="w1")
        row = self.store.get(self.owner, job["id"])
        self.assertEqual(row["state"], "cancelled")
        self.assertEqual(row["result"]["reason"], "operator_reconciled")

    # --- read-side sanitization of pre-schema (legacy) rows ---

    def legacy_finish(self, key, payload, result, cost=0):
        """Write a terminal row directly, bypassing finish — the
        shape of data persisted before the schema existed."""
        job = self.store.submit(self.owner, key, {"operation": "scan"}, cost=cost)
        db = sqlite3.connect(self.store.path)
        db.execute("UPDATE jobs SET state='completed', payload=?, result=? WHERE id=?",
                   (json.dumps(payload), json.dumps(result), job["id"]))
        db.commit()
        db.close()
        return job

    def test_proof_sanitizes_legacy_canary(self):
        job = self.legacy_finish("legacy-proof", {"operation": "irradiation",
                                                  "irradiation_count": 3},
                                 {"mode": CANARY, "requested_count": CANARY,
                                  "completed_count": CANARY,
                                  "stop_command_acknowledged": CANARY}, cost=117)
        proof = self.store.execution_proof(self.owner, job["id"])
        self.assertIsNone(proof["execution"]["mode"])
        # Out-of-schema counts fall back to server-side values only.
        self.assertEqual(proof["execution"]["requested_count"], 3)
        self.assertIsNone(proof["execution"]["completed_count"])
        self.assertIsNone(proof["execution"]["stop_command_acknowledged"])
        self.assertNotIn("CANARY", json.dumps(proof))

    def test_public_view_sanitizes_legacy_canary(self):
        job = self.legacy_finish("legacy-view", {"operation": "irradiation",
                                                 "irradiation_count": 3},
                                 {"mode": CANARY, "reason": "PRIVATE DIAGNOSTIC",
                                  "requested_count": "3", "completed_count": 3,
                                  "stop_command_acknowledged": CANARY})
        view = public_job_view(self.store, self.store.get(self.owner, job["id"]))
        self.assertNotIn("mode", view["result"])
        self.assertNotIn("reason", view["result"])
        # Batch 2d (MEDIUM 5) contract: counts in the public view are
        # derived from the server-side payload/progress records and
        # included in the view; the stored legacy value is never
        # passed through. The stored string "3" must not surface —
        # the int 3 below is derived from the payload reservation.
        self.assertEqual(view["result"]["requested_count"], 3)
        self.assertIsInstance(view["result"]["requested_count"], int)
        self.assertEqual(view["result"]["completed_count"], 3)
        self.assertNotIn("stop_command_acknowledged", view["result"])
        self.assertNotIn("CANARY", json.dumps(view))
        self.assertNotIn("PRIVATE DIAGNOSTIC", json.dumps(view))

    def test_public_view_drops_legacy_counts_without_payload_record(self):
        # The fallback path of the batch-2d contract (Antigravity
        # glass audit, view D supplement): with no payload
        # reservation to derive from, the stored legacy figures
        # pass through the schema sanitizer only — a string count
        # is dropped, never published.
        job = self.legacy_finish("legacy-view-nopayload",
                                 {"operation": "irradiation"},
                                 {"mode": CANARY, "requested_count": "3",
                                  "completed_count": "3"})
        view = public_job_view(self.store, self.store.get(self.owner, job["id"]))
        self.assertNotIn("requested_count", view["result"])
        self.assertNotIn("completed_count", view["result"])
        self.assertNotIn("CANARY", json.dumps(view))

    def test_public_view_drops_poisoned_legacy_scan_entries(self):
        digest = self.store.save_catalog(
            {"field": [{"id": "one", "text": "First"},
                       {"id": "two", "text": "Second"}]})
        job = self.legacy_finish(
            "legacy-scan", {"operation": "scan", "catalog_digest": digest},
            {"mode": "synthetic",
             "scan": {"observer_coordinate_applied": "yes",
                      "final_entries": [
                          {"id": ["x"], "hits": 1, "rank": 1},
                          {"id": "one", "hits": int("ab" * 32, 16), "rank": 1},
                          {"id": "two", "hits": 5, "rank": 2}]}})
        view = public_job_view(self.store, self.store.get(self.owner, job["id"]))
        entries = view["result"]["scan"]["final_entries"]
        # The unhashable-id entry is dropped; the read is not poisoned.
        self.assertEqual({e["entry_id"] for e in entries}, {"one", "two"})
        by_id = {e["entry_id"]: e for e in entries}
        # A digest-sized hits integer never publishes.
        self.assertIsNone(by_id["one"]["hits"])
        self.assertEqual(by_id["two"]["hits"], 5)
        # A8 fix: publication order is hits descending, and an
        # entry whose hits do not publish sorts below every entry
        # with a known count (this assertion used to pin the old
        # per-field rank order, ["one", "two"]).
        self.assertEqual([e["entry_id"] for e in entries], ["two", "one"])
        self.assertIs(view["result"]["scan"]["observer_coordinate_applied"], False)

    def test_public_view_dedupes_entries_and_caps_rank(self):
        # Astra diff review round 2 (Item 2, MEDIUM): a repeated
        # entry id publishes once (its second hits value — a covert
        # chunk carrier — never leaves), and a rank beyond the
        # entry list publishes as None.
        digest = self.store.save_catalog(
            {"field": [{"id": "one", "text": "First"},
                       {"id": "two", "text": "Second"}]})
        job = self.legacy_finish(
            "legacy-dupe", {"operation": "scan", "catalog_digest": digest},
            {"mode": "synthetic",
             "scan": {"final_entries": [
                 {"id": "one", "hits": 11, "rank": 1},
                 {"id": "one", "hits": 2222, "rank": 2},
                 {"id": "two", "hits": 5, "rank": 99}]}})
        view = public_job_view(self.store, self.store.get(self.owner, job["id"]))
        entries = view["result"]["scan"]["final_entries"]
        self.assertEqual([e["entry_id"] for e in entries], ["one", "two"])
        self.assertEqual(entries[0]["hits"], 11)
        self.assertIsNone(entries[1]["rank"])

    def test_public_view_publishes_at_most_the_engine_contract(self):
        # Astra diff review round 4 (Item 2): publication is capped
        # at the engine contract of 10 entries, however many
        # distinct resolvable entries a stored result carries.
        catalog = {"field": [{"id": f"e{n}", "text": f"Entry {n}"}
                             for n in range(12)]}
        digest = self.store.save_catalog(catalog)
        job = self.legacy_finish(
            "legacy-twelve", {"operation": "scan", "catalog_digest": digest},
            {"mode": "synthetic",
             "scan": {"final_entries": [
                 {"id": f"e{n}", "hits": 100 - n, "rank": n + 1}
                 for n in range(12)]}})
        view = public_job_view(self.store, self.store.get(self.owner, job["id"]))
        entries = view["result"]["scan"]["final_entries"]
        self.assertEqual(len(entries), 10)
        self.assertEqual([e["rank"] for e in entries], list(range(1, 11)))

    def test_public_view_bounds_rank_by_the_published_list(self):
        # Astra diff review round 3 (Item 2, MEDIUM): duplicate
        # padding must not stretch the published rank range — a
        # rank is capped by the list actually published.
        digest = self.store.save_catalog(
            {"field": [{"id": "one", "text": "First"},
                       {"id": "two", "text": "Second"}]})
        padded = [{"id": "one", "hits": 1, "rank": 5}] * 4
        padded.append({"id": "two", "hits": 2, "rank": 2})
        job = self.legacy_finish(
            "legacy-padded", {"operation": "scan", "catalog_digest": digest},
            {"mode": "synthetic", "scan": {"final_entries": padded}})
        view = public_job_view(self.store, self.store.get(self.owner, job["id"]))
        entries = view["result"]["scan"]["final_entries"]
        self.assertEqual({e["entry_id"] for e in entries}, {"one", "two"})
        by_id = {e["entry_id"]: e for e in entries}
        self.assertIsNone(by_id["one"]["rank"])
        self.assertEqual(by_id["two"]["rank"], 2)

    def test_public_view_keeps_legitimate_values(self):
        job = self.legacy_finish("legacy-ok", {"operation": "irradiation",
                                               "irradiation_count": 9},
                                 {"mode": "synthetic", "requested_count": 9,
                                  "completed_count": 9, "simulated_count": 9,
                                  "stop_command_acknowledged": True,
                                  "executed_physically": False})
        view = public_job_view(self.store, self.store.get(self.owner, job["id"]))
        self.assertEqual(view["result"]["mode"], "synthetic")
        self.assertEqual(view["result"]["completed_count"], 9)
        self.assertEqual(view["result"]["simulated_count"], 9)
        self.assertIs(view["result"]["stop_command_acknowledged"], True)
        self.assertIs(view["result"]["executed_physically"], False)


if __name__ == "__main__":
    unittest.main()
