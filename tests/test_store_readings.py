"""Native-app AI reading charge discipline at the Store layer
(native-reading-design-20261122 §3.4/§4): reserve -> settle only
with the stored result, full release on failure, routed accounts
settling in D-Planet through the pending-ops outbox, the daily
cap, orphan reconciliation, and reading-text expiry tied to the
parent scan's content lifecycle. Synthetic fixtures only."""
import tempfile
import time
import unittest
from pathlib import Path

from aiqs_si.policies import LimitPolicy
from aiqs_si.store import ReadingChargeError, Store
from test_account_linking_l3 import FakeCreditPlanet

QUOTE = {"credits": 36, "price_version": "2026-11-18"}
CONSENT = {"owner": "owner", "operation": "reading", "credits": 36}


class ReadingStoreTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "readings.sqlite"

    def make_store(self, *, credits=500, routing=False, planet=None, cap=None):
        limit = LimitPolicy(scan_daily_quota_per_account=None,
                            scan_exposure_budget_total=None,
                            scan_units_per_job=10, claim_ttl_seconds=3600,
                            reading_daily_cap_per_account=cap)
        store = Store(self.path, dplanet_client=planet,
                      dplanet_routing_enabled=routing, limit_policy=limit)
        owner = store.create_test_account(credits)
        return store, owner

    def confirmation(self, store, owner, scan_id="scan-1"):
        return store.create_app_reading_confirmation(owner, scan_id, QUOTE)

    def reserve(self, store, owner, reading_id="reading-1", key="rk-1",
                confirmation_id=None, scan_id="scan-1"):
        confirmation_id = confirmation_id or self.confirmation(store, owner, scan_id)
        return store.reserve_reading(owner, reading_id, confirmation_id, key,
                                     credits=36, price_version="2026-11-18",
                                     consent=CONSENT)

    def ledger(self, store):
        with store.transaction() as db:
            return [(row["kind"], row["amount"]) for row in
                    db.execute("SELECT kind,amount FROM ledger WHERE job IS NULL "
                               "AND kind LIKE 'reading_%' ORDER BY id")]

    # --- local accounts ---

    def test_reserve_then_settle_charges_once_and_stores_text(self):
        store, owner = self.make_store()
        self.reserve(store, owner)
        self.assertEqual(store.balance(owner), 464)
        settled = store.settle_reading(
            owner, "reading-1", text="読み解き本文", model="google/gemini-2.5-flash",
            prompt_tokens=900, completion_tokens=1100, provider_cost_microusd=3300)
        self.assertEqual(settled["state"], "completed")
        self.assertEqual(settled["text"], "読み解き本文")
        self.assertFalse(settled["text_expired"])
        self.assertEqual(store.balance(owner), 464)
        self.assertEqual(self.ledger(store),
                         [("reading_reserve", -36), ("reading_settle", 0)])
        with store.transaction() as db:
            row = db.execute("SELECT model, provider_cost_microusd, route "
                             "FROM app_readings WHERE id='reading-1'").fetchone()
            self.assertEqual(row["model"], "google/gemini-2.5-flash")
            self.assertEqual(row["provider_cost_microusd"], 3300)
            self.assertEqual(row["route"], "native-reading")
            consent = db.execute("SELECT credits FROM app_reading_consents "
                                 "WHERE reading_id='reading-1'").fetchone()
            self.assertEqual(consent["credits"], 36)

    def test_settled_text_round_trips_with_paragraphs_intact(self):
        # Summary-first format (2026-11-22): the stored body is one
        # text — summary block, blank lines, paragraph breaks — and
        # must come back byte-identical (no newline collapsing).
        store, owner = self.make_store()
        self.reserve(store, owner)
        text = ("【要約】\n"
                "一行目の要点です。\n二行目の要点です。\n三行目の要点です。\n"
                "四行目の要点です。\n五行目の要点です。"
                "\n\n【キーワード群】\n第一段落の本文です。続きの文です。"
                "\n\n第二段落の本文です。"
                "\n\n【インテンション】\n意図についての本文です。"
                "\n\n【自己愛アファーメーション】\n締めの本文です。")
        settled = store.settle_reading(
            owner, "reading-1", text=text, model="google/gemini-2.5-flash",
            prompt_tokens=900, completion_tokens=1100, provider_cost_microusd=3300)
        self.assertEqual(settled["text"], text)
        self.assertIn("\n\n【キーワード群】", settled["text"])
        self.assertEqual(settled["text"].count("\n\n"), 4)

    def test_reserve_then_release_restores_everything(self):
        store, owner = self.make_store()
        self.reserve(store, owner)
        released = store.release_reading(owner, "reading-1",
                                         error_code="reading_unavailable")
        self.assertEqual(released["state"], "failed")
        self.assertEqual(released["error_code"], "reading_unavailable")
        self.assertEqual(store.balance(owner), 500)
        self.assertEqual(self.ledger(store),
                         [("reading_reserve", -36), ("reading_release", 36)])
        # Release is idempotent on a terminal row.
        again = store.release_reading(owner, "reading-1",
                                      error_code="reading_unavailable")
        self.assertEqual(again["state"], "failed")
        self.assertEqual(store.balance(owner), 500)
        self.assertEqual(len(self.ledger(store)), 2)

    def test_insufficient_balance_writes_nothing(self):
        store, owner = self.make_store(credits=10)
        with self.assertRaises(ReadingChargeError) as caught:
            self.reserve(store, owner)
        self.assertEqual(caught.exception.code, "reading_insufficient_balance")
        self.assertEqual(store.balance(owner), 10)
        self.assertIsNone(store.reading_by_request(owner, "rk-1"))
        self.assertEqual(self.ledger(store), [])

    def test_confirmation_is_one_shot_and_quote_bound(self):
        store, owner = self.make_store()
        confirmation_id = self.confirmation(store, owner)
        self.reserve(store, owner, confirmation_id=confirmation_id)
        store.release_reading(owner, "reading-1", error_code="reading_unavailable")
        with self.assertRaises(ReadingChargeError) as caught:
            self.reserve(store, owner, reading_id="reading-2", key="rk-2",
                         confirmation_id=confirmation_id)
        self.assertEqual(caught.exception.code, "confirmation_already_used")
        # A parked quote that no longer matches the charged price is
        # rejected before anything is written.
        other = self.confirmation(store, owner)
        with self.assertRaises(ReadingChargeError) as caught:
            store.reserve_reading(owner, "reading-3", other, "rk-3", credits=35,
                                  price_version="2026-11-18", consent=CONSENT)
        self.assertEqual(caught.exception.code, "quote_changed")
        self.assertIsNone(store.reading_by_request(owner, "rk-3"))

    def test_expired_confirmation_rejected(self):
        store, owner = self.make_store()
        confirmation_id = self.confirmation(store, owner)
        with store.transaction() as db:
            db.execute("UPDATE app_reading_confirmations SET expires_at=? WHERE id=?",
                       (time.time() - 1, confirmation_id))
        with self.assertRaises(ReadingChargeError) as caught:
            self.reserve(store, owner, confirmation_id=confirmation_id)
        self.assertEqual(caught.exception.code, "confirmation_expired")

    def test_daily_cap_counts_reserved_and_completed(self):
        store, owner = self.make_store(cap=1)
        self.reserve(store, owner)
        store.settle_reading(owner, "reading-1", text="本文", model="m",
                             prompt_tokens=None, completion_tokens=None,
                             provider_cost_microusd=None)
        with self.assertRaises(ReadingChargeError) as caught:
            self.reserve(store, owner, reading_id="reading-2", key="rk-2")
        self.assertEqual(caught.exception.code, "reading_daily_cap")
        self.assertEqual(store.balance(owner), 464)

    def test_orphan_reservations_are_reconciled_to_full_release(self):
        store, owner = self.make_store()
        self.reserve(store, owner)
        self.assertEqual(store.reconcile_orphan_readings(now=time.time() + 599), 0)
        self.assertEqual(store.balance(owner), 464)
        self.assertEqual(store.reconcile_orphan_readings(now=time.time() + 601), 1)
        reading = store.get_reading(owner, "reading-1")
        self.assertEqual(reading["state"], "failed")
        self.assertEqual(reading["error_code"], "interrupted")
        self.assertEqual(store.balance(owner), 500)

    def test_reading_text_expires_with_the_parent_scan(self):
        store, owner = self.make_store()
        job = store.submit(owner, "scan-key",
                           {"operation": "scan", "input_mode": "request-images-v1"},
                           cost=0)
        store.claim_next()
        store.finish(job["id"], "completed", {"mode": "synthetic"})
        self.reserve(store, owner, scan_id=job["id"])
        store.settle_reading(owner, "reading-1", text="消える本文", model="m",
                             prompt_tokens=None, completion_tokens=None,
                             provider_cost_microusd=None)
        with store.transaction() as db:
            db.execute("UPDATE content_lifecycle SET finished_at=? WHERE job=?",
                       (time.time() - 90000, job["id"]))
        self.assertEqual(store.expire_scans(), 1)
        reading = store.get_reading(owner, "reading-1")
        self.assertEqual(reading["state"], "completed")
        self.assertIsNone(reading["text"])
        self.assertTrue(reading["text_expired"])
        # The charge record itself survives for audit.
        self.assertEqual(reading["credits"], 36)

    def test_reading_text_expires_with_a_registered_target_scan(self):
        # Same lifecycle as the image-mode parent, but the scan
        # payload carries no input_mode (app path; Astra batch 1).
        store, owner = self.make_store()
        job = store.submit(owner, "scan-key",
                           {"operation": "scan", "target_ids": ["target-1"]},
                           cost=0)
        store.claim_next()
        store.finish(job["id"], "completed", {"mode": "synthetic"})
        self.reserve(store, owner, scan_id=job["id"])
        store.settle_reading(owner, "reading-1", text="消える本文", model="m",
                             prompt_tokens=None, completion_tokens=None,
                             provider_cost_microusd=None)
        with store.transaction() as db:
            db.execute("UPDATE content_lifecycle SET finished_at=? WHERE job=?",
                       (time.time() - 90000, job["id"]))
        self.assertEqual(store.expire_scans(), 1)
        reading = store.get_reading(owner, "reading-1")
        self.assertIsNone(reading["text"])
        self.assertTrue(reading["text_expired"])
        self.assertEqual(reading["credits"], 36)

    # --- routed (linked) accounts: §9 resolution ---

    def make_routed(self, planet_balance=1000):
        planet = FakeCreditPlanet(balance=planet_balance)
        store, owner = self.make_store(routing=True, planet=planet)
        store.record_account_link(owner, "player-1", "link-1")
        return store, owner, planet

    def test_routed_reserve_and_settle_use_the_reading_id_key(self):
        store, owner, planet = self.make_routed()
        self.reserve(store, owner)
        # The D-Planet reservation is keyed by the reading id (an
        # opaque string on that side); the SI balance never moves.
        self.assertEqual(planet.reserved, {"reading-1": 36})
        self.assertEqual(store.balance(owner), 500)
        store.settle_reading(owner, "reading-1", text="本文", model="m",
                             prompt_tokens=None, completion_tokens=None,
                             provider_cost_microusd=None)
        self.assertEqual(planet.closed, {"reading-1": ("settled", 36)})
        self.assertEqual(planet.wallet, 964)
        self.assertEqual(store.pending_dplanet_ops(), [])
        self.assertEqual(self.ledger(store),
                         [("reading_reserve", -36), ("reading_settle", 0)])

    def test_routed_release_refunds_in_dplanet(self):
        store, owner, planet = self.make_routed()
        self.reserve(store, owner)
        store.release_reading(owner, "reading-1", error_code="reading_unavailable")
        self.assertEqual(planet.wallet, 1000)
        self.assertEqual(planet.closed, {"reading-1": ("released", 36)})
        self.assertEqual(store.balance(owner), 500)

    def test_routed_insufficient_balance_writes_nothing(self):
        store, owner, planet = self.make_routed(planet_balance=10)
        with self.assertRaises(ReadingChargeError) as caught:
            self.reserve(store, owner)
        self.assertEqual(caught.exception.code, "reading_insufficient_balance")
        self.assertIsNone(store.reading_by_request(owner, "rk-1"))
        self.assertEqual(self.ledger(store), [])

    def test_routed_settle_delivery_failure_queues_a_pending_op(self):
        store, owner, planet = self.make_routed()
        self.reserve(store, owner)
        planet.flaky.add(("settle", "reading-1"))
        store.settle_reading(owner, "reading-1", text="本文", model="m",
                             prompt_tokens=None, completion_tokens=None,
                             provider_cost_microusd=None)
        pending = store.pending_dplanet_ops()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["phase"], "settle")
        self.assertEqual(pending[0]["job"], "reading-1")
        result = store.retry_pending_dplanet_ops()
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(planet.closed, {"reading-1": ("settled", 36)})


if __name__ == "__main__":
    unittest.main()
