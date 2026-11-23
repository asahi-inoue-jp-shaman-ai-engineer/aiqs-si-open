"""Job display summaries (app "show everything" contract, 2026-11-23):
the submit-time snapshot of target display names, intention and
self-love affirmation that the app confirmation screen and history
present back to the owner. The snapshot lives outside the content
lifecycle, so it survives scan expiry and irradiation terminal
cleanup; jobs that predate summaries fall back to the payload while
its content survives. The public MCP serializer never gains these
fields. Synthetic fixtures only."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from aiqs_si.dplanet import DPlanetError
from aiqs_si.serializers import public_job_view
from aiqs_si.store import Store
from aiqs_si.worker import Worker
from aiqs_si_device.synthetic import SyntheticDevice
from prayer_fixtures import PHOTO, JOY, make_service as Service, target

CATALOG = {"field": [{"id": "one", "text": "test"}]}
ISSUER = "https://issuer.example.test/auth/v1"
INTENTION = "fixture intention for the summary"


class JobDisplaySummaryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name) / "summary.sqlite")
        self.service = Service(self.store, CATALOG)
        self.owner = self.store.register_identity(ISSUER, "summary-user")
        with self.store.transaction() as db:
            db.execute("UPDATE accounts SET balance=500 WHERE id=?", (self.owner,))

    def completed_scan(self, key="scan-1", intention=INTENTION):
        job = self.service.submit_scan(
            self.owner, key, intention, [target(self.service, self.owner)],
            self_love_joy=JOY)
        Worker(self.store, SyntheticDevice()).run_one()
        self.assertEqual(self.store.get(self.owner, job["id"])["state"], "completed")
        return job["id"]

    def expire(self, job_id):
        with self.store.transaction() as db:
            db.execute("UPDATE content_lifecycle SET finished_at=? WHERE job=?",
                       (time.time() - 90000, job_id))
        self.assertEqual(self.store.expire_scans(), 1)
        expired = self.store.get(self.owner, job_id)
        self.assertTrue(expired["payload"].get("content_removed"))

    # --- snapshot at submission ---

    def test_scan_submit_records_summary(self):
        job = self.service.submit_scan(
            self.owner, "scan-1", INTENTION, [target(self.service, self.owner)],
            self_love_joy=JOY)
        summary = self.store.display_summary(self.owner, job)
        self.assertEqual(summary["targets"], ["Synthetic Target"])
        self.assertEqual(summary["intention"], INTENTION)
        self.assertEqual(summary["self_love_joy"], JOY)

    def test_replay_keeps_the_first_snapshot(self):
        target_id = target(self.service, self.owner)
        first = self.service.submit_scan(self.owner, "scan-1", INTENTION,
                                         [target_id], self_love_joy=JOY)
        self.store.rename_target(self.owner, target_id, "Renamed Target")
        replay = self.service.submit_scan(self.owner, "scan-1", INTENTION,
                                          [target_id], self_love_joy=JOY)
        self.assertEqual(replay["id"], first["id"])
        summary = self.store.display_summary(self.owner, first)
        self.assertEqual(summary["targets"], ["Synthetic Target"])

    def test_snapshot_ignores_later_rename_and_delete(self):
        target_id = target(self.service, self.owner)
        job = self.service.submit_scan(self.owner, "scan-1", INTENTION,
                                       [target_id], self_love_joy=JOY)
        self.store.rename_target(self.owner, target_id, "Renamed Target")
        self.assertEqual(self.store.display_summary(self.owner, job)["targets"],
                         ["Synthetic Target"])
        self.store.delete_target(self.owner, target_id)
        self.assertEqual(self.store.display_summary(self.owner, job)["targets"],
                         ["Synthetic Target"])

    def test_image_scan_summary_has_words_but_no_target_names(self):
        job = self.service.submit_image_scan(self.owner, "image-1", INTENTION,
                                             [PHOTO], self_love_joy=JOY)
        summary = self.store.display_summary(self.owner, job)
        self.assertEqual(summary["targets"], [])
        self.assertEqual(summary["intention"], INTENTION)
        self.assertEqual(summary["self_love_joy"], JOY)

    # --- survival across the content lifecycle ---

    def test_summary_survives_scan_expiry(self):
        scan_id = self.completed_scan()
        self.expire(scan_id)
        summary = self.store.display_summary(
            self.owner, self.store.get(self.owner, scan_id))
        self.assertEqual(summary["targets"], ["Synthetic Target"])
        self.assertEqual(summary["intention"], INTENTION)
        self.assertEqual(summary["self_love_joy"], JOY)

    def test_summary_survives_irradiation_terminal_cleanup(self):
        scan_id = self.completed_scan()
        job = self.service.submit_irradiation(self.owner, "irr-1", scan_id, count=3)
        Worker(self.store, SyntheticDevice()).run_one()
        finished = self.store.get(self.owner, job["id"])
        self.assertEqual(finished["state"], "completed")
        # Terminal cleanup minimized the irradiation payload itself.
        self.assertTrue(finished["payload"].get("content_removed"))
        self.assertNotIn("intention", finished["payload"])
        summary = self.store.display_summary(self.owner, finished)
        self.assertEqual(summary["targets"], ["Synthetic Target"])
        self.assertEqual(summary["intention"], INTENTION)
        self.assertEqual(summary["self_love_joy"], JOY)

    # --- fallback for jobs that predate summaries ---

    def test_legacy_job_falls_back_to_payload_then_to_empty(self):
        scan_id = self.completed_scan()
        with self.store.transaction() as db:
            db.execute("DELETE FROM job_display_summaries WHERE job=?", (scan_id,))
        job = self.store.get(self.owner, scan_id)
        summary = self.store.display_summary(self.owner, job)
        self.assertEqual(summary["targets"], ["Synthetic Target"])
        self.assertEqual(summary["intention"], INTENTION)
        self.assertEqual(summary["self_love_joy"], JOY)
        self.expire(scan_id)
        summary = self.store.display_summary(
            self.owner, self.store.get(self.owner, scan_id))
        self.assertEqual(summary["targets"], [])
        self.assertIsNone(summary["intention"])
        self.assertIsNone(summary["self_love_joy"])

    # --- history rows carry the fields ---

    def test_recent_app_sessions_carry_display_fields(self):
        scan_id = self.completed_scan()
        self.store.record_app_job(scan_id, owner=self.owner, operation="scan")
        self.expire(scan_id)
        sessions = self.store.recent_app_sessions(self.owner, limit=20)
        self.assertEqual(len(sessions), 1)
        session = sessions[0]
        self.assertEqual(session["job_id"], scan_id)
        self.assertEqual(session["targets"], ["Synthetic Target"])
        self.assertEqual(session["intention"], INTENTION)
        self.assertEqual(session["self_love_joy"], JOY)

    # --- the public boundary does not move ---

    def test_public_job_view_has_no_display_fields(self):
        scan_id = self.completed_scan()
        view = public_job_view(self.store, self.store.get(self.owner, scan_id))
        self.assertNotIn("targets", view)
        self.assertNotIn("intention", view)
        self.assertNotIn("self_love_joy", view)

    def test_public_view_and_proof_carry_no_display_values(self):
        scan_id = self.completed_scan()
        target_id = self.store.get_by_request(
            self.owner, "scan-1")["payload"]["target_ids"][0]
        view = public_job_view(self.store, self.store.get(self.owner, scan_id))
        dumped = json.dumps(view)
        self.assertNotIn(INTENTION, dumped)
        self.assertNotIn(JOY, dumped)
        self.assertNotIn("Synthetic Target", dumped)
        self.assertNotIn(target_id, dumped)
        self.assertNotIn(PHOTO[:32], dumped)
        proof = self.store.execution_proof(self.owner, scan_id)
        dumped = json.dumps(proof)
        self.assertNotIn(INTENTION, dumped)
        self.assertNotIn(JOY, dumped)
        self.assertNotIn("Synthetic Target", dumped)
        self.assertNotIn(target_id, dumped)
        self.assertNotIn(PHOTO[:32], dumped)
        self.assertNotIn("targets", proof)

    # --- owner enforcement on the reader (Astra round 1) ---

    def test_display_summary_rejects_foreign_owner(self):
        scan_id = self.completed_scan()
        other = self.store.register_identity(ISSUER, "other-user")
        with self.assertRaises(KeyError):
            self.store.display_summary(other, {"id": scan_id})

    def test_display_summary_ignores_caller_supplied_payload(self):
        scan_id = self.completed_scan()
        with self.store.transaction() as db:
            db.execute("DELETE FROM job_display_summaries WHERE job=?", (scan_id,))
        forged = {"id": scan_id, "payload": {"operation": "scan",
                                             "intention": "forged intention",
                                             "self_love_joy": "forged",
                                             "target_ids": []}}
        summary = self.store.display_summary(self.owner, forged)
        self.assertEqual(summary["intention"], INTENTION)
        self.assertEqual(summary["self_love_joy"], JOY)
        self.assertEqual(summary["targets"], ["Synthetic Target"])

    def test_record_app_job_rejects_foreign_job(self):
        scan_id = self.completed_scan()
        other = self.store.register_identity(ISSUER, "other-user")
        with self.assertRaises(KeyError):
            self.store.record_app_job(scan_id, owner=other, operation="scan")

    def test_history_excludes_inconsistent_attribution(self):
        scan_id = self.completed_scan()
        other = self.store.register_identity(ISSUER, "other-user")
        # A forged attribution row (written around record_app_job's
        # validation) must not surface the job in either history:
        # the history join requires the job's real owner to match.
        with self.store.transaction() as db:
            db.execute("INSERT INTO app_jobs VALUES (?,?,?,?,?)",
                       (scan_id, other, "scan", None, time.time()))
        self.assertEqual(self.store.recent_app_sessions(other, limit=20), [])
        self.assertEqual(self.store.recent_app_sessions(self.owner, limit=20), [])

    # --- malformed stored data (Astra round 1) ---

    def test_fallback_skips_foreign_and_malformed_targets(self):
        scan_id = self.completed_scan()
        other = self.store.register_identity(ISSUER, "other-user")
        foreign = self.store.register_target(other, "foreign-key",
                                             {"name": "Foreign Target"})
        own = self.store.get_by_request(self.owner, "scan-1")["payload"]["target_ids"][0]
        with self.store.transaction() as db:
            db.execute("INSERT INTO targets VALUES (?,?,?,?)",
                       ("malformed-target", self.owner, "bad-key", "not-json{"))
            db.execute("DELETE FROM job_display_summaries WHERE job=?", (scan_id,))
            payload = {"operation": "scan", "intention": INTENTION,
                       "self_love_joy": JOY,
                       "target_ids": [foreign, "malformed-target", own]}
            db.execute("UPDATE jobs SET payload=? WHERE id=?",
                       (json.dumps(payload), scan_id))
        summary = self.store.display_summary(
            self.owner, self.store.get(self.owner, scan_id))
        self.assertEqual(summary["targets"], ["Synthetic Target"])
        self.assertEqual(summary["intention"], INTENTION)

    def test_history_survives_malformed_payload_when_summary_exists(self):
        scan_id = self.completed_scan()
        self.store.record_app_job(scan_id, owner=self.owner, operation="scan")
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET payload=? WHERE id=?",
                       ("{broken json", scan_id))
        sessions = self.store.recent_app_sessions(self.owner, limit=20)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["targets"], ["Synthetic Target"])
        self.assertEqual(sessions[0]["intention"], INTENTION)
        summary = self.store.display_summary(self.owner, {"id": scan_id})
        self.assertEqual(summary["targets"], ["Synthetic Target"])

    def test_deeply_nested_stored_data_never_breaks_display_reads(self):
        nested = "[" * 2000 + "0" + "]" * 2000
        scan_id = self.completed_scan()
        own = self.store.get_by_request(self.owner, "scan-1")["payload"]["target_ids"][0]
        # A summary whose targets column nests past the JSON
        # decoder's recursion limit reads as empty names.
        with self.store.transaction() as db:
            db.execute("UPDATE job_display_summaries SET targets=? WHERE job=?",
                       (nested, scan_id))
        summary = self.store.display_summary(self.owner, {"id": scan_id})
        self.assertEqual(summary["targets"], [])
        self.assertEqual(summary["intention"], INTENTION)
        # A payload nested the same way (no summary) reads as
        # empty fields.
        with self.store.transaction() as db:
            db.execute("DELETE FROM job_display_summaries WHERE job=?", (scan_id,))
            db.execute("UPDATE jobs SET payload=? WHERE id=?", (nested, scan_id))
        summary = self.store.display_summary(self.owner, {"id": scan_id})
        self.assertEqual(summary, {"targets": [], "intention": None,
                                   "self_love_joy": None})
        # A target profile nested the same way is unresolvable,
        # not an error, on the fallback path.
        with self.store.transaction() as db:
            db.execute("UPDATE targets SET profile=? WHERE id=?", (nested, own))
            payload = {"operation": "scan", "intention": INTENTION,
                       "self_love_joy": JOY, "target_ids": [own]}
            db.execute("UPDATE jobs SET payload=? WHERE id=?",
                       (json.dumps(payload), scan_id))
        summary = self.store.display_summary(self.owner, {"id": scan_id})
        self.assertEqual(summary["targets"], [])
        self.assertEqual(summary["intention"], INTENTION)

    def test_history_resolves_each_distinct_target_once(self):
        first_id = self.completed_scan()
        target_id = self.store.get_by_request(
            self.owner, "scan-1")["payload"]["target_ids"][0]
        second_id = self.service.submit_scan(
            self.owner, "scan-2", INTENTION, [target_id],
            self_love_joy=JOY)["id"]
        Worker(self.store, SyntheticDevice()).run_one()
        for job_id in (first_id, second_id):
            self.store.record_app_job(job_id, owner=self.owner, operation="scan")
        with self.store.transaction() as db:
            db.execute("DELETE FROM job_display_summaries")
        calls = []
        original = Store._profile_name

        def counting(db, owner, target_id_):
            calls.append(target_id_)
            return original(db, owner, target_id_)

        with mock.patch.object(Store, "_profile_name", staticmethod(counting)):
            sessions = self.store.recent_app_sessions(self.owner, limit=20)
        self.assertEqual(len(sessions), 2)
        self.assertEqual(calls, [target_id])
        for session in sessions:
            self.assertEqual(session["targets"], ["Synthetic Target"])

    # --- unencodable stored text (Astra round 3) ---

    def test_fallback_skips_unencodable_target_id(self):
        scan_id = self.completed_scan()
        own = self.store.get_by_request(self.owner, "scan-1")["payload"]["target_ids"][0]
        with self.store.transaction() as db:
            db.execute("DELETE FROM job_display_summaries WHERE job=?", (scan_id,))
            payload = {"operation": "scan", "intention": INTENTION,
                       "self_love_joy": JOY,
                       "target_ids": ["\ud800", own]}
            db.execute("UPDATE jobs SET payload=? WHERE id=?",
                       (json.dumps(payload), scan_id))
        summary = self.store.display_summary(self.owner, {"id": scan_id})
        self.assertEqual(summary["targets"], ["Synthetic Target"])
        self.assertEqual(summary["intention"], INTENTION)

    def test_valid_unicode_names_are_preserved(self):
        target_id = self.store.register_target(self.owner, "uni-target",
                                               {"name": "テスト🌟"})
        job = self.store.submit(self.owner, "uni-1",
                                {"operation": "scan", "intention": INTENTION,
                                 "self_love_joy": JOY, "catalog_digest": "x",
                                 "target_ids": [target_id]}, cost=0)
        summary = self.store.display_summary(self.owner, job)
        self.assertEqual(summary["targets"], ["テスト🌟"])

    def test_unencodable_target_name_is_rejected(self):
        target_id = self.store.register_target(self.owner, "named-target",
                                               {"name": "Before"})
        with self.assertRaises(ValueError):
            self.store.rename_target(self.owner, target_id, "\ud800abc")
        with self.assertRaises(ValueError):
            self.service.register_target(self.owner, "bad-name-2",
                                         name="\ud800abc")

    # --- summary-write failures roll the submission back ---

    def funded_owner(self):
        owner = self.store.create_test_account(500)
        target_id = self.store.register_target(owner, "fault-target",
                                               {"name": "Fault Target"})
        payload = {"operation": "scan", "intention": INTENTION,
                   "self_love_joy": JOY, "catalog_digest": "x",
                   "target_ids": [target_id]}
        return owner, payload

    def assert_paid_submit_rolled_back(self, owner, key):
        self.assertIsNone(self.store.get_by_request(owner, key))
        self.assertEqual(self.store.balance(owner), 500)
        with self.store.transaction() as db:
            summaries = db.execute(
                "SELECT count(*) FROM job_display_summaries").fetchone()[0]
            reserves = db.execute(
                "SELECT count(*) FROM ledger WHERE owner=? AND kind='reserve'",
                (owner,)).fetchone()[0]
        self.assertEqual(summaries, 0)
        self.assertEqual(reserves, 0)

    def test_summary_failure_rolls_back_local_submit(self):
        owner, payload = self.funded_owner()
        with mock.patch.object(self.store, "_record_display_summary",
                               side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.store.submit(owner, "fault-1", payload, cost=50)
        self.assert_paid_submit_rolled_back(owner, "fault-1")

    def test_failure_after_summary_insert_rolls_back_local_submit(self):
        owner, payload = self.funded_owner()
        original = self.store._record_display_summary

        def after_insert(db, job, owner_, payload_):
            original(db, job, owner_, payload_)
            raise RuntimeError("injected after insert")

        with mock.patch.object(self.store, "_record_display_summary",
                               after_insert):
            with self.assertRaises(RuntimeError):
                self.store.submit(owner, "fault-2", payload, cost=50)
        self.assert_paid_submit_rolled_back(owner, "fault-2")

    def test_fallback_repeated_target_ids_are_bounded(self):
        scan_id = self.completed_scan()
        own = self.store.get_by_request(self.owner, "scan-1")["payload"]["target_ids"][0]
        with self.store.transaction() as db:
            db.execute("DELETE FROM job_display_summaries WHERE job=?", (scan_id,))
            payload = {"operation": "scan", "intention": INTENTION,
                       "self_love_joy": JOY, "target_ids": [own] * 50}
            db.execute("UPDATE jobs SET payload=? WHERE id=?",
                       (json.dumps(payload), scan_id))
        summary = self.store.display_summary(
            self.owner, self.store.get(self.owner, scan_id))
        self.assertEqual(summary["targets"],
                         ["Synthetic Target"] * Store._DISPLAY_TARGET_LIMIT)


class RoutedPlanet:
    """D-Planet double for routed submissions: reserve can apply
    and then lose the response once (the ambiguous window)."""

    def __init__(self, balance=1000):
        self.wallet = balance
        self.reserved = {}
        self.reject = None
        self.ambiguous_once = False

    def reserve(self, player_id, job_id, amount):
        if self.reject is not None:
            raise self.reject
        if job_id in self.reserved:
            return {"state": "reserved", "balance": self.wallet}
        if amount > self.wallet:
            raise DPlanetError(402, "insufficient")
        self.wallet -= amount
        self.reserved[job_id] = amount
        if self.ambiguous_once:
            self.ambiguous_once = False
            raise DPlanetError(None, "unreachable")
        return {"state": "reserved", "balance": self.wallet}

    def release(self, player_id, job_id, amount):
        if job_id in self.reserved:
            self.wallet += self.reserved.pop(job_id)
        return {"state": "released", "balance": self.wallet}

    def settle(self, player_id, job_id):
        self.reserved.pop(job_id, None)
        return {"state": "settled"}

    def balance(self, player_id):
        return {"balance": self.wallet}


class RoutedDisplaySummaryTests(unittest.TestCase):
    """Summaries on the D-Planet routed submission path (Astra
    round 1): exactly one per committed job, none for a job that
    was never created."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.fake = RoutedPlanet()
        self.store = Store(Path(folder.name) / "routed.sqlite",
                           dplanet_client=self.fake, dplanet_routing_enabled=True)
        self.owner = self.store.create_test_account(500)
        self.store.record_account_link(self.owner, "player-1", "link-1")
        self.target_id = self.store.register_target(
            self.owner, "routed-target", {"name": "Routed Target"})

    def payload(self):
        return {"operation": "scan", "intention": INTENTION,
                "self_love_joy": JOY, "catalog_digest": "x",
                "target_ids": [self.target_id]}

    def summary_rows(self):
        with self.store.transaction() as db:
            return db.execute("SELECT job FROM job_display_summaries").fetchall()

    def test_routed_submit_records_summary(self):
        job = self.store.submit(self.owner, "r-1", self.payload(), cost=50)
        summary = self.store.display_summary(self.owner, job)
        self.assertEqual(summary["targets"], ["Routed Target"])
        self.assertEqual(summary["intention"], INTENTION)
        self.assertEqual(summary["self_love_joy"], JOY)
        self.assertEqual(len(self.summary_rows()), 1)

    def test_routed_rejection_records_no_summary(self):
        self.fake.reject = DPlanetError(402, "insufficient")
        with self.assertRaises(ValueError):
            self.store.submit(self.owner, "r-2", self.payload(), cost=50)
        self.assertEqual(self.summary_rows(), [])

    def test_routed_ambiguous_retry_records_exactly_one_summary(self):
        self.fake.ambiguous_once = True
        with self.assertRaises(ValueError):
            self.store.submit(self.owner, "r-3", self.payload(), cost=50)
        self.assertEqual(self.summary_rows(), [])
        uncertain = self.store.uncertain_dplanet_reservations()
        self.assertEqual(len(uncertain), 1)
        op_id = uncertain[0]["op_id"]
        job = self.store.submit(self.owner, "r-3", self.payload(), cost=50)
        # The retry adopted the recorded operation id instead of
        # minting a fresh one: one remote reservation, one job and
        # one summary, all under that exact id.
        self.assertEqual(job["id"], op_id)
        self.assertEqual(list(self.fake.reserved), [op_id])
        rows = self.summary_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["job"], op_id)
        summary = self.store.display_summary(self.owner, job)
        self.assertEqual(summary["targets"], ["Routed Target"])

    def test_routed_summary_failure_compensates_reservation(self):
        with mock.patch.object(self.store, "_record_display_summary",
                               side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.store.submit(self.owner, "r-4", self.payload(), cost=50)
        # The insert transaction rolled back (no job, no summary)
        # and the fenced compensation released the remote
        # reservation (queued in the outbox and delivered
        # best-effort by the compensator itself): the wallet is
        # whole and nothing is left pending.
        self.assertIsNone(self.store.get_by_request(self.owner, "r-4"))
        self.assertEqual(self.summary_rows(), [])
        self.assertEqual(self.store.pending_dplanet_ops(), [])
        self.assertEqual(self.fake.reserved, {})
        self.assertEqual(self.fake.wallet, 1000)

    def test_routed_failure_after_summary_insert_compensates(self):
        original = self.store._record_display_summary

        def after_insert(db, job, owner, payload):
            original(db, job, owner, payload)
            raise RuntimeError("injected after insert")

        with mock.patch.object(self.store, "_record_display_summary",
                               after_insert):
            with self.assertRaises(RuntimeError):
                self.store.submit(self.owner, "r-6", self.payload(), cost=50)
        self.assertIsNone(self.store.get_by_request(self.owner, "r-6"))
        self.assertEqual(self.summary_rows(), [])
        self.assertEqual(self.store.pending_dplanet_ops(), [])
        self.assertEqual(self.fake.reserved, {})
        self.assertEqual(self.fake.wallet, 1000)

    def test_concurrent_same_key_routed_submits_share_one_summary(self):
        barrier = threading.Barrier(2)
        arrivals = []
        original_reserve = self.fake.reserve

        def slow_reserve(player_id, job_id, amount):
            result = original_reserve(player_id, job_id, amount)
            arrivals.append(job_id)
            # A broken barrier FAILS this test: the overlap must
            # really happen, not degrade into a serialized replay.
            barrier.wait(timeout=10)
            return result

        self.fake.reserve = slow_reserve
        releases = []
        original_release = self.fake.release

        def counting_release(player_id, job_id, amount):
            releases.append(job_id)
            return original_release(player_id, job_id, amount)

        self.fake.release = counting_release
        writes = []
        original_writer = self.store._record_display_summary

        def counting_writer(db, job, owner, payload):
            writes.append(job)
            return original_writer(db, job, owner, payload)

        results, errors = [], []

        def run():
            try:
                results.append(self.store.submit(self.owner, "r-5",
                                                 self.payload(), cost=50))
            except Exception as error:  # collected for assertion below
                errors.append(error)

        with mock.patch.object(self.store, "_record_display_summary",
                               counting_writer):
            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
        for thread in threads:
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(arrivals), 2)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["id"], results[1]["id"])
        rows = self.summary_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["job"], results[0]["id"])
        # Exactly one snapshot was written, for the winning job,
        # and the winner's reservation was never released.
        self.assertEqual(writes, [results[0]["id"]])
        self.assertEqual(releases, [])
        self.assertEqual(list(self.fake.reserved), [results[0]["id"]])
        self.assertEqual(self.fake.wallet, 950)
        self.assertEqual(self.store.pending_dplanet_ops(), [])


if __name__ == "__main__":
    unittest.main()
