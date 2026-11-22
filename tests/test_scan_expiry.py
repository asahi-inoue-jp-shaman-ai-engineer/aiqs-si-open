import tempfile
import time
import unittest
from pathlib import Path
from aiqs_si.store import Store
from aiqs_si.worker import Worker
from aiqs_si_device.synthetic import SyntheticDevice
from aiqs_si.outbox import CompletionOutbox
from prayer_fixtures import make_service, target, PHOTO, JOY


class ScanExpiryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name) / 'queue.sqlite')
        self.owner = self.store.create_test_account(100)
        self.service = make_service(self.store, {'field': [{'id': '1', 'text': 'test'}]})
        self.scan = self.service.submit_image_scan(self.owner, 'scan', 'private prayer', [PHOTO], self_love_joy=JOY)
        Worker(self.store, SyntheticDevice()).run_one()
        with self.store.transaction() as db:
            self.finished = db.execute('SELECT finished_at FROM content_lifecycle WHERE job=?', (self.scan['id'],)).fetchone()[0]

    def test_expiry_boundary_removes_content_and_blocks_replay(self):
        self.assertEqual(self.store.expire_scans(now=self.finished + 86399), 0)
        self.assertEqual(self.store.expire_scans(now=self.finished + 86400), 1)
        self.assertEqual(self.store.expire_scans(now=self.finished + 86400), 0)
        result = self.store.get(self.owner, self.scan['id'])
        self.assertEqual(result['payload'], {'operation': 'scan', 'content_removed': True})
        self.assertIsNone(result['result'])
        with self.assertRaises(ValueError):
            self.store.get_context(self.scan['id'])
        with self.assertRaises(ValueError):
            self.service.submit_irradiation(self.owner, 'send', self.scan['id'], count=1)
        with self.assertRaises(ValueError):
            self.service.submit_image_scan(self.owner, 'scan', 'private prayer', [PHOTO], self_love_joy=JOY)
        self.assertEqual(self.store.balance(self.owner), 100)

    def test_active_reference_protects_scan_and_stale_intake_cannot_submit(self):
        send = self.service.submit_irradiation(self.owner, 'send', self.scan['id'], count=1)
        self.assertEqual(self.store.expire_scans(now=self.finished + 86400), 0)
        self.store.cancel(self.owner, send['id'])
        self.assertTrue(self.store.get(self.owner, self.scan['id'])['payload']['content_removed'])
        self.assertEqual(self.store.expire_scans(now=self.finished + 86400), 0)
        with self.assertRaises(ValueError):
            self.store.submit(self.owner, 'stale', send['payload'], cost=1)
        self.assertEqual(self.store.balance(self.owner), 100)

    def test_lost_scan_ack_keeps_only_digest_on_worker_and_recovers_after_expiry(self):
        scan = self.service.submit_image_scan(self.owner, 'scan2', 'private second prayer', [PHOTO], self_love_joy=JOY)
        outbox = CompletionOutbox(self.store.path + '.outbox')
        finish = self.store.finish
        def lose_ack(*args, **kwargs):
            finish(*args, **kwargs)
            raise ConnectionError('synthetic lost acknowledgement')
        self.store.finish = lose_ack
        with self.assertRaises(ConnectionError):
            Worker(self.store, SyntheticDevice(), outbox=outbox).run_one()
        pending = outbox.pending()
        self.assertEqual(pending[0][1], 'scan_receipt')
        self.assertEqual(set(pending[0][2]), {'digest'})
        self.assertNotIn('private', str(pending))
        self.store.expire_scans(now=time.time() + 86401)
        self.store.finish = finish
        self.assertTrue(Worker(self.store, SyntheticDevice(), outbox=outbox).run_one())
        self.assertEqual(outbox.pending(), [])
        self.assertTrue(self.store.get(self.owner, scan['id'])['payload']['content_removed'])



class AppScanExpiryTests(unittest.TestCase):
    """Registered-target (app) scans carry no input_mode; their
    retention contract is the request-image one — content expires
    24 hours after completion and terminal cleanup minimizes both
    jobs — while the reusable target record survives (Astra review
    2026-11-22, batch 1). Isolated store: expiry counts are exact."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name) / 'queue.sqlite')
        self.owner = self.store.create_test_account(1000)
        self.service = make_service(self.store, {'field': [{'id': '1', 'text': 'test'}]})
        self.target_id = target(self.service, self.owner)
        self.scan = self.service.submit_scan(self.owner, 'app-scan', 'app intention',
                                             [self.target_id], self_love_joy=JOY)
        Worker(self.store, SyntheticDevice()).run_one()
        with self.store.transaction() as db:
            self.finished = db.execute('SELECT finished_at FROM content_lifecycle WHERE job=?',
                                       (self.scan['id'],)).fetchone()[0]

    def test_app_scan_expires_like_an_image_scan(self):
        self.assertEqual(self.store.expire_scans(now=self.finished + 86399), 0)
        self.assertEqual(self.store.expire_scans(now=self.finished + 86400), 1)
        result = self.store.get(self.owner, self.scan['id'])
        self.assertEqual(result['payload'], {'operation': 'scan', 'content_removed': True})
        self.assertIsNone(result['result'])
        with self.assertRaises(ValueError):
            self.store.get_context(self.scan['id'])
        with self.assertRaises(ValueError):
            self.service.submit_irradiation(self.owner, 'app-send', self.scan['id'], count=1)
        self.assertEqual(self.store.balance(self.owner), 1000)
        # The reusable target record is not scan content; it survives.
        self.assertEqual(self.store.get_target(self.owner, self.target_id)['name'],
                         'Synthetic Target')

    def test_app_irradiation_terminal_cleanup_minimizes_the_job(self):
        # The irradiation's own payload copies (entries, intention,
        # private context) are minimized at its terminal state, on
        # this channel too. The registered-target scan itself is a
        # session artifact: it survives for reuse until the 24-hour
        # expiry above removes it.
        send = self.service.submit_irradiation(self.owner, 'app-send', self.scan['id'], count=1)
        Worker(self.store, SyntheticDevice()).run_one()
        irradiation = self.store.get(self.owner, send['id'])
        self.assertEqual(irradiation['state'], 'completed')
        self.assertTrue(irradiation['payload']['content_removed'])
        self.assertNotIn('entries', irradiation['payload'])
        self.assertIn('completed_count', irradiation['result'])
        self.assertTrue(irradiation['result']['stopped'])
        with self.assertRaises(ValueError):
            self.store.get_context(send['id'])
        scan = self.store.get(self.owner, self.scan['id'])
        self.assertNotIn('content_removed', scan['payload'])
        self.assertEqual(self.store.expire_scans(now=self.finished + 86400), 1)
        self.assertTrue(self.store.get(self.owner, self.scan['id'])['payload']['content_removed'])
        self.assertEqual(self.store.get_target(self.owner, self.target_id)['name'],
                         'Synthetic Target')
