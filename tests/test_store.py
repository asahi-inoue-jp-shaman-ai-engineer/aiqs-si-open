from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from aiqs_si.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "test.sqlite")
        self.owner = self.store.create_test_account(10)

    def submit(self, key="one", cost=3):
        return self.store.submit(self.owner, key, {"mode": "synthetic"}, cost=cost)

    def test_concurrent_replay_charges_once(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = list(pool.map(lambda _: self.submit(), range(16)))
        self.assertEqual(len({job["id"] for job in jobs}), 1)
        self.assertEqual(self.store.balance(self.owner), 7)
        with self.assertRaises(ValueError):
            self.store.submit(self.owner, "one", {"different": True}, cost=3)

    def test_no_overspend(self):
        def attempt(i):
            try:
                return self.submit(str(i), 6)
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = list(pool.map(attempt, range(8)))
        self.assertEqual(sum(job is not None for job in jobs), 1)
        self.assertEqual(self.store.balance(self.owner), 4)

    def test_cancel_release_once(self):
        job = self.submit()
        self.store.cancel(self.owner, job["id"])
        self.store.cancel(self.owner, job["id"])
        self.assertEqual(self.store.balance(self.owner), 10)
        self.assertIsNone(self.store.claim_next())

    def test_worker_exclusion_and_unknown_no_retry(self):
        job = self.submit()
        self.submit("two")
        self.assertEqual(self.store.claim_next()["id"], job["id"])
        self.assertIsNone(self.store.claim_next())
        self.store.finish(job["id"], "unknown", {"reason": "execution_outcome_unknown"})
        restarted = Store(self.store.path)
        self.assertIsNone(restarted.claim_next())
        self.assertEqual(restarted.balance(self.owner), 4)
        self.assertEqual(self.submit()["state"], "unknown")

    def test_simultaneous_workers_claim_only_one_fifo_job(self):
        first = self.submit("first", 1)
        second = self.submit("second", 1)
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda i: Store(self.store.path).claim_next(worker=str(i)), range(8)))
        claimed = [job for job in claims if job is not None]
        self.assertEqual([job["id"] for job in claimed], [first["id"]])
        self.store.finish(first["id"], "completed", {"stopped": True})
        self.assertEqual(self.store.claim_next()["id"], second["id"])

    def test_owner_isolation(self):
        job = self.submit()
        other = self.store.create_test_account()
        for action in (self.store.get, self.store.cancel, self.store.queue_status):
            with self.assertRaises(KeyError):
                action(other, job["id"])

    def test_queue_position_updates_without_other_user_details(self):
        first = self.submit("first", 1)
        other = self.store.create_test_account(10)
        second = self.store.submit(other, "second", {"private": "not disclosed"}, cost=1)
        third = self.submit("third", 1)
        status = self.store.queue_status(self.owner, third["id"])
        self.assertEqual(status["jobs_ahead"], 2)
        self.assertEqual(status["position"], 3)
        self.assertNotIn(second["id"], str(status))
        self.assertNotIn(other, str(status))
        self.assertIsNone(status["estimated_wait_seconds"])
        self.store.cancel(other, second["id"])
        self.assertEqual(self.store.queue_status(self.owner, third["id"])["position"], 2)
        self.store.claim_next()
        self.store.finish(first["id"], "unknown", {"reason": "execution_outcome_unknown"})
        self.assertTrue(self.store.queue_status(self.owner, third["id"])["blocked_by_unresolved_execution"])
        self.assertIsNone(self.store.claim_next())
        self.assertIsNone(self.store.queue_status(self.owner, first["id"])["position"])

    def test_confirmed_failure_refund_and_completion_settlement(self):
        job = self.submit()
        self.store.claim_next()
        self.store.finish(job["id"], "failed", {"executed": False})
        self.assertEqual(self.store.balance(self.owner), 10)
        second = self.submit("two")
        self.store.claim_next()
        self.store.finish(second["id"], "completed", {"mode": "synthetic"})
        self.assertEqual(self.store.balance(self.owner), 7)
        with self.assertRaises(ValueError):
            self.store.finish(second["id"], "completed", {})


if __name__ == "__main__":
    unittest.main()


class ClaimDisplayFieldTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "display.sqlite3")
        self.owner = self.store.create_test_account(10)

    def submit(self, key="display-1"):
        return self.store.submit(self.owner, key,
                                 {"operation": "scan", "catalog_digest": "x"}, cost=0)

    def test_claim_carries_short_id_and_recorded_name_only(self):
        self.submit()
        claimed = self.store.claim_next(worker='["iss","w"]')
        self.assertEqual(claimed["owner_short_id"], self.owner[:8])
        self.assertNotIn("owner_display_name", claimed)
        self.store.finish(claimed["id"], "failed", {"reason": "device_mode_mismatch"})
        job2 = self.submit("display-2")
        self.store.set_account_display(self.owner, "Fixture Name")
        claimed2 = self.store.claim_next(worker='["iss","w"]')
        self.assertEqual(claimed2["id"], job2["id"])
        self.assertEqual(claimed2["owner_display_name"], "Fixture Name")

    def test_set_account_display_validation(self):
        with self.assertRaises(ValueError):
            self.store.set_account_display(self.owner, "  ")
        with self.assertRaises(KeyError):
            self.store.set_account_display("missing-account", "Name")
