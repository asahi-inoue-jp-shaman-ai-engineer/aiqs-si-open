"""Completion notifications (foundation work item 2): the per-job
wait/notify choice, the finish-transaction outbox, the delivery
cycle (escalation, claim, bounded retries, reclaim, expiry), the
resume token binding, and the unconfigured fail-soft. Synthetic
fixtures only; the mailer is a fake and no address here is real."""
import hashlib
import re
import tempfile
import time
import unittest
from pathlib import Path

import httpx

from aiqs_si.notifications import (
    CLAIM_STALE_SECONDS, ESCALATION_GRACE_SECONDS, MAX_DELIVERY_ATTEMPTS,
    NOTIFICATION_TTL_SECONDS, MailDeliveryError, NotificationDelivery,
    ResendMailer, build_notification_delivery, load_templates,
    render_completion)
from aiqs_si.store import Store

EMAIL = "user-a@fixture.example.test"
OTHER_EMAIL = "user-b@fixture.example.test"


class FakeMailer:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send(self, *, to, subject, text, idempotency_key=None,
             sender=None):
        if self.fail:
            raise MailDeliveryError("transport")
        self.sent.append({"to": to, "subject": subject, "text": text,
                          "idempotency_key": idempotency_key})


class JobNotificationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name) / "notify.sqlite")
        self.owner = self.store.register_identity(
            "https://issuer.example.test/auth/v1", "user-a")
        self.other = self.store.register_identity(
            "https://issuer.example.test/auth/v1", "user-b")

    def job(self, key="job-1", operation="scan"):
        return self.store.submit(self.owner, key, {"operation": operation},
                                 cost=0)["id"]

    def notify_job(self, key="job-1", mode="notify", email=EMAIL):
        job_id = self.job(key)
        self.store.set_job_notification(
            job_id, owner=self.owner, operation="scan", mode=mode, email=email)
        return job_id

    def finish(self, job_id, state="completed"):
        """Claim-then-finish, the worker's path (finish requires a
        running job). Jobs in these tests are finished one at a
        time, so claim_next always binds the intended job."""
        self.store.claim_next()
        self.store.finish(job_id, state, {"ok": state == "completed"})

    # --- the recorded choice ---

    def test_choice_is_recorded_and_first_record_wins(self):
        job_id = self.notify_job(mode="wait")
        self.store.set_job_notification(
            job_id, owner=self.owner, operation="scan", mode="notify",
            email=EMAIL)
        row = self.store.job_notification(job_id)
        self.assertEqual(row["mode"], "wait")
        self.assertEqual(row["status"], "armed")
        self.assertEqual(row["owner"], self.owner)

    def test_choice_validation(self):
        job_id = self.job()
        with self.assertRaises(ValueError):
            self.store.set_job_notification(
                job_id, owner=self.owner, operation="scan", mode="notify",
                email=None)
        with self.assertRaises(ValueError):
            self.store.set_job_notification(
                job_id, owner=self.owner, operation="scan", mode="later",
                email=EMAIL)
        with self.assertRaises(ValueError):
            self.store.set_job_notification(
                job_id, owner=self.owner, operation="reading", mode="wait",
                email=None)
        with self.assertRaises(ValueError):
            self.store.set_job_notification(
                job_id, owner=self.owner, operation="scan", mode="wait",
                email="not-an-address")
        self.assertIsNone(self.store.job_notification(job_id))

    def test_jobs_without_a_choice_never_notify(self):
        # MCP / internal jobs get no row: finishing one creates
        # nothing and the cycle stays silent.
        job_id = self.job()
        self.finish(job_id)
        self.assertIsNone(self.store.job_notification(job_id))
        self.store.notification_delivery = NotificationDelivery(
            self.store, FakeMailer())
        self.store.run_notification_cycle()
        self.assertIsNone(self.store.job_notification(job_id))

    # --- finish-transaction outbox ---

    def test_notify_mode_queues_in_the_finish_transaction(self):
        job_id = self.notify_job()
        self.finish(job_id)
        row = self.store.job_notification(job_id)
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["outcome"], "completed")
        self.assertIsNotNone(row["finished_at"])
        self.assertIsNotNone(row["queued_at"])

    def test_failed_finish_also_queues_with_its_outcome(self):
        job_id = self.notify_job()
        self.finish(job_id, "failed")
        row = self.store.job_notification(job_id)
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["outcome"], "failed")

    def test_wait_mode_stays_armed_at_finish(self):
        job_id = self.notify_job(mode="wait")
        self.finish(job_id)
        row = self.store.job_notification(job_id)
        self.assertEqual(row["status"], "armed")
        self.assertIsNotNone(row["finished_at"])

    def test_owner_cancel_suppresses_and_erases_address(self):
        job_id = self.notify_job()
        self.store.cancel(self.owner, job_id)
        row = self.store.job_notification(job_id)
        self.assertEqual(row["status"], "suppressed")
        self.assertIsNone(row["email"])

    # --- escalation sweep ---

    def test_wait_mode_escalates_only_after_the_grace_window(self):
        job_id = self.notify_job(mode="wait")
        self.finish(job_id)
        finished_at = self.store.job_notification(job_id)["finished_at"]
        counts = self.store.escalate_completion_notifications(
            finished_at + ESCALATION_GRACE_SECONDS - 1)
        self.assertEqual(counts["escalated"], 0)
        self.assertEqual(self.store.job_notification(job_id)["status"], "armed")
        counts = self.store.escalate_completion_notifications(
            finished_at + ESCALATION_GRACE_SECONDS + 1)
        self.assertEqual(counts["escalated"], 1)
        self.assertEqual(self.store.job_notification(job_id)["status"], "queued")

    def test_wait_mode_without_address_never_escalates(self):
        job_id = self.notify_job(mode="wait", email=None)
        self.finish(job_id)
        finished_at = self.store.job_notification(job_id)["finished_at"]
        self.store.escalate_completion_notifications(
            finished_at + ESCALATION_GRACE_SECONDS + 1)
        self.assertEqual(self.store.job_notification(job_id)["status"], "armed")

    def test_viewed_result_suppresses_armed_and_queued(self):
        armed = self.notify_job("job-armed", mode="wait")
        self.finish(armed)
        self.store.mark_notification_viewed(self.owner, armed)
        row = self.store.job_notification(armed)
        self.assertEqual(row["status"], "suppressed")
        self.assertIsNone(row["email"])
        queued = self.notify_job("job-queued")
        self.finish(queued)
        self.assertEqual(self.store.job_notification(queued)["status"], "queued")
        self.store.mark_notification_viewed(self.owner, queued)
        self.assertEqual(self.store.job_notification(queued)["status"],
                         "suppressed")
        # Another owner's view marks nothing.
        third = self.notify_job("job-third")
        self.finish(third)
        self.store.mark_notification_viewed(self.other, third)
        self.assertEqual(self.store.job_notification(third)["status"], "queued")

    def test_records_expire_after_the_ttl(self):
        job_id = self.notify_job(mode="wait")
        self.finish(job_id)
        finished_at = self.store.job_notification(job_id)["finished_at"]
        counts = self.store.escalate_completion_notifications(
            finished_at + NOTIFICATION_TTL_SECONDS + 1)
        self.assertEqual(counts["expired"], 1)
        row = self.store.job_notification(job_id)
        self.assertEqual(row["status"], "expired")
        self.assertIsNone(row["email"])

    def test_late_record_for_finished_job_is_stamped_at_insert(self):
        # Late attribution path: the choice is recorded after the
        # job already finished, so the transition hook never ran.
        # The insert itself stamps the finish (Astra round-2 F1);
        # the sweep's backfill remains for rows whose note was lost
        # (covered separately below).
        job_id = self.job()
        self.finish(job_id)
        self.store.set_job_notification(
            job_id, owner=self.owner, operation="scan", mode="wait",
            email=EMAIL)
        row = self.store.job_notification(job_id)
        self.assertIsNotNone(row["finished_at"])
        self.assertEqual(row["outcome"], "completed")

    def test_stale_claim_is_reclaimed(self):
        job_id = self.notify_job()
        self.finish(job_id)
        claimed = self.store.claim_completion_notifications(time.time())
        self.assertEqual([row["job"] for row in claimed], [job_id])
        self.assertEqual(self.store.job_notification(job_id)["status"],
                         "sending")
        counts = self.store.escalate_completion_notifications(
            time.time() + CLAIM_STALE_SECONDS + 1)
        self.assertEqual(counts["reclaimed"], 1)
        self.assertEqual(self.store.job_notification(job_id)["status"],
                         "queued")

    # --- delivery cycle ---

    def delivery(self, mailer):
        return NotificationDelivery(self.store, mailer,
                                    base_url="https://asi.iboga.jp")

    def test_delivery_sends_once_and_binds_a_hashed_resume_token(self):
        job_id = self.notify_job()
        self.finish(job_id)
        mailer = FakeMailer()
        self.assertEqual(self.delivery(mailer).run_once(), 1)
        self.assertEqual(len(mailer.sent), 1)
        mail = mailer.sent[0]
        self.assertEqual(mail["to"], EMAIL)
        self.assertIn("スキャン", mail["subject"])
        self.assertIn("完了", mail["subject"])
        match = re.search(r"#resume=([A-Za-z0-9_-]+)", mail["text"])
        self.assertIsNotNone(match)
        token = match.group(1)
        row = self.store.job_notification(job_id)
        self.assertEqual(row["status"], "sent")
        self.assertIsNone(row["email"])  # erased once sent
        # Only the hash is stored; the plaintext exists in the mail.
        self.assertEqual(row["resume_token_hash"],
                         hashlib.sha256(token.encode()).hexdigest())
        self.assertNotIn(token, str(row))
        # A second cycle sends nothing more: one job, one mail.
        self.assertEqual(self.delivery(mailer).run_once(), 0)
        self.assertEqual(len(mailer.sent), 1)
        # Redemption is owner-bound and repeatable until expiry.
        self.assertEqual(self.store.redeem_resume_token(self.owner, token),
                         {"job_id": job_id, "operation": "scan"})
        self.assertEqual(self.store.redeem_resume_token(self.owner, token),
                         {"job_id": job_id, "operation": "scan"})
        self.assertIsNone(self.store.redeem_resume_token(self.other, token))
        self.assertIsNone(self.store.redeem_resume_token(self.owner, "nope"))

    def test_resume_token_expires(self):
        job_id = self.notify_job()
        self.finish(job_id)
        mailer = FakeMailer()
        self.delivery(mailer).run_once()
        token = re.search(r"#resume=([A-Za-z0-9_-]+)",
                          mailer.sent[0]["text"]).group(1)
        row = self.store.job_notification(job_id)
        with self.assertRaises(ValueError):
            self.store.redeem_resume_token(
                self.owner, token, now=row["resume_expires_at"] + 1)

    def test_failed_send_retries_with_backoff_then_expires(self):
        job_id = self.notify_job()
        self.finish(job_id)
        mailer = FakeMailer(fail=True)
        delivery = self.delivery(mailer)
        for attempt in range(1, MAX_DELIVERY_ATTEMPTS + 1):
            self.assertEqual(delivery.run_once(), 0)
            row = self.store.job_notification(job_id)
            if attempt < MAX_DELIVERY_ATTEMPTS:
                self.assertEqual(row["status"], "queued")
                self.assertEqual(row["attempts"], attempt)
                self.assertGreater(row["next_attempt_at"], time.time())
                with self.store.transaction() as db:
                    db.execute("UPDATE job_notifications "
                               "SET next_attempt_at=0 WHERE job=?", (job_id,))
            else:
                self.assertEqual(row["status"], "expired")
                self.assertEqual(row["attempts"], MAX_DELIVERY_ATTEMPTS)
                self.assertIsNone(row["email"])
                self.assertIsNone(row["resume_token"])
                # Delivery ended, but the locator survives: an
                # earlier attempt may have been accepted, and its
                # link must work until its own expiry (round-3 F3).
                self.assertIsNotNone(row["resume_token_hash"])
        self.assertEqual(mailer.sent, [])

    # --- Astra round-2 regressions ---

    def test_late_notify_insert_for_finished_job_queues_at_once(self):
        job_id = self.job("late-notify")
        self.finish(job_id)
        self.store.set_job_notification(
            job_id, owner=self.owner, operation="scan", mode="notify",
            email=EMAIL)
        row = self.store.job_notification(job_id)
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["outcome"], "completed")
        self.assertIsNotNone(row["finished_at"])

    def test_late_wait_insert_for_finished_job_arms_with_stamp(self):
        job_id = self.job("late-wait")
        self.finish(job_id, "failed")
        self.store.set_job_notification(
            job_id, owner=self.owner, operation="scan", mode="wait",
            email=EMAIL)
        row = self.store.job_notification(job_id)
        self.assertEqual(row["status"], "armed")
        self.assertEqual(row["outcome"], "failed")
        self.assertIsNotNone(row["finished_at"])

    def test_late_insert_for_cancelled_job_is_suppressed(self):
        job_id = self.job("late-cancel")
        self.store.cancel(self.owner, job_id)
        self.store.set_job_notification(
            job_id, owner=self.owner, operation="scan", mode="notify",
            email=EMAIL)
        row = self.store.job_notification(job_id)
        self.assertEqual(row["status"], "suppressed")
        self.assertIsNone(row["email"])

    def test_backfill_queues_a_notify_row_whose_note_was_lost(self):
        job_id = self.notify_job("lost-note")
        self.finish(job_id)
        with self.store.transaction() as db:
            db.execute("UPDATE job_notifications SET status='armed', "
                       "finished_at=NULL, outcome=NULL, queued_at=NULL, "
                       "next_attempt_at=NULL WHERE job=?", (job_id,))
        counts = self.store.escalate_completion_notifications(time.time())
        self.assertEqual(counts["backfilled"], 1)
        self.assertEqual(self.store.job_notification(job_id)["status"],
                         "queued")

    def test_claim_fencing_blocks_a_stale_senders_outcome(self):
        job_id = self.notify_job("fenced")
        self.finish(job_id)
        first = self.store.claim_completion_notifications(time.time())[0]
        with self.store.transaction() as db:
            db.execute("UPDATE job_notifications SET claimed_at=0 "
                       "WHERE job=?", (job_id,))
        self.store.escalate_completion_notifications(time.time())
        second = self.store.claim_completion_notifications(
            time.time() + 10000)[0]
        self.assertNotEqual(first["claim_id"], second["claim_id"])
        self.assertFalse(self.store.mark_notification_failed(
            job_id, "transport", claim_id=first["claim_id"]))
        self.assertEqual(self.store.job_notification(job_id)["status"],
                         "sending")
        self.assertTrue(self.store.mark_notification_failed(
            job_id, "transport", claim_id=second["claim_id"]))
        self.assertEqual(self.store.job_notification(job_id)["status"],
                         "queued")

    def test_reclaim_counts_an_attempt_and_keeps_the_token(self):
        job_id = self.notify_job("reclaim-token")
        self.finish(job_id)
        mailer = FakeMailer(fail=True)
        self.delivery(mailer).run_once()  # mints token, send fails
        row = self.store.job_notification(job_id)
        self.assertIsNotNone(row["resume_token_hash"])
        token_hash = row["resume_token_hash"]
        claimed = self.store.claim_completion_notifications(
            time.time() + 10000)
        self.assertEqual(len(claimed), 1)
        with self.store.transaction() as db:
            db.execute("UPDATE job_notifications SET claimed_at=0 "
                       "WHERE job=?", (job_id,))
        self.store.escalate_completion_notifications(time.time())
        row = self.store.job_notification(job_id)
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["attempts"], 2)  # failure + reclaim
        self.assertEqual(row["resume_token_hash"], token_hash)

    def test_retry_reuses_the_token_and_idempotency_key(self):
        job_id = self.notify_job("stable-payload")
        self.finish(job_id)
        mailer = FakeMailer(fail=True)
        delivery = self.delivery(mailer)
        delivery.run_once()
        with self.store.transaction() as db:
            db.execute("UPDATE job_notifications SET next_attempt_at=0 "
                       "WHERE job=?", (job_id,))
        mailer.fail = False
        # Capture the successful send by swapping the fake's record.
        self.assertEqual(delivery.run_once(), 1)
        self.assertEqual(len(mailer.sent), 1)
        sent = mailer.sent[0]
        self.assertEqual(sent["idempotency_key"],
                         "aiqs-completion-" + job_id)
        token = re.search(r"#resume=([A-Za-z0-9_-]+)", sent["text"]).group(1)
        row = self.store.job_notification(job_id)
        self.assertEqual(row["status"], "sent")
        self.assertIsNone(row["resume_token"])  # plaintext erased
        self.assertEqual(row["resume_token_hash"],
                         hashlib.sha256(token.encode()).hexdigest())
        self.assertEqual(self.store.redeem_resume_token(self.owner, token),
                         {"job_id": job_id, "operation": "scan"})

    def test_viewed_during_sending_suppresses_the_retry(self):
        job_id = self.notify_job("viewed-sending")
        self.finish(job_id)
        claimed = self.store.claim_completion_notifications(time.time())
        self.assertEqual(len(claimed), 1)
        self.store.mark_notification_viewed(self.owner, job_id)
        row = self.store.job_notification(job_id)
        self.assertEqual(row["status"], "sending")  # in flight, not recalled
        self.assertIsNotNone(row["viewed_at"])
        self.assertTrue(self.store.mark_notification_failed(
            job_id, "transport", claim_id=claimed[0]["claim_id"]))
        self.assertEqual(self.store.job_notification(job_id)["status"],
                         "suppressed")

    def test_mailer_contract_violation_is_isolated_per_message(self):
        class BadMailer:
            def __init__(self):
                self.sent = []

            def send(self, *, to, subject, text, idempotency_key=None,
             sender=None):
                if to == EMAIL:
                    raise RuntimeError("contract violation")
                self.sent.append(to)

        first = self.notify_job("bad-1")
        self.finish(first)
        second = self.job("bad-2")
        self.store.set_job_notification(
            second, owner=self.owner, operation="scan", mode="notify",
            email=OTHER_EMAIL)
        self.finish(second)
        mailer = BadMailer()
        self.assertEqual(self.delivery(mailer).run_once(), 1)
        self.assertEqual(mailer.sent, [OTHER_EMAIL])
        row = self.store.job_notification(first)
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["last_error"], "internal_error")
        self.assertEqual(self.store.job_notification(second)["status"],
                         "sent")

    def test_failed_outcome_uses_the_ended_wording(self):
        job_id = self.notify_job()
        self.finish(job_id, "failed")
        mailer = FakeMailer()
        self.delivery(mailer).run_once()
        self.assertIn("終了", mailer.sent[0]["subject"])

    # --- Astra round-3 regressions ---

    def test_accepted_then_timed_out_mail_keeps_its_link(self):
        # The provider accepts, the response is lost, every retry
        # also reports failure, and the record exhausts its
        # attempts: the delivered mail's link must still redeem.
        class AcceptThenFailMailer:
            def __init__(self):
                self.sent = []

            def send(self, *, to, subject, text, idempotency_key=None,
                     sender=None):
                self.sent.append({"to": to, "subject": subject,
                                  "text": text})
                raise MailDeliveryError("transport")

        job_id = self.notify_job("accepted-lost")
        self.finish(job_id)
        mailer = AcceptThenFailMailer()
        delivery = self.delivery(mailer)
        for _ in range(MAX_DELIVERY_ATTEMPTS):
            delivery.run_once()
            with self.store.transaction() as db:
                db.execute("UPDATE job_notifications "
                           "SET next_attempt_at=0 WHERE job=?", (job_id,))
        row = self.store.job_notification(job_id)
        self.assertEqual(row["status"], "expired")
        self.assertIsNone(row["email"])
        self.assertEqual(len(mailer.sent), MAX_DELIVERY_ATTEMPTS)
        # Every attempt mailed the identical payload (same link).
        texts = {mail["text"] for mail in mailer.sent}
        self.assertEqual(len(texts), 1)
        token = re.search(r"#resume=([A-Za-z0-9_-]+)",
                          mailer.sent[0]["text"]).group(1)
        self.assertEqual(self.store.redeem_resume_token(self.owner, token),
                         {"job_id": job_id, "operation": "scan"})
        with self.assertRaises(ValueError):
            self.store.redeem_resume_token(
                self.owner, token, now=row["resume_expires_at"] + 1)

    def test_envelope_survives_a_configuration_change(self):
        job_id = self.notify_job("envelope")
        self.finish(job_id)
        failing = FakeMailer(fail=True)
        self.delivery(failing).run_once()  # envelope persisted
        row = self.store.job_notification(job_id)
        self.assertIsNotNone(row["mail_subject"])
        with self.store.transaction() as db:
            db.execute("UPDATE job_notifications SET next_attempt_at=0 "
                       "WHERE job=?", (job_id,))
        # A new delivery with different wording and origin retries:
        # the stored envelope wins, byte for byte.
        changed = NotificationDelivery(
            self.store, FakeMailer(), base_url="https://other.example.test",
            templates={"completed_subject": "CHANGED {operation_label}",
                       "completed_body": "CHANGED {resume_url}",
                       "ended_subject": "CHANGED {operation_label}",
                       "ended_body": "CHANGED {resume_url}"})
        mailer = changed.mailer
        self.assertEqual(changed.run_once(), 1)
        self.assertEqual(mailer.sent[0]["subject"], row["mail_subject"])
        self.assertEqual(mailer.sent[0]["text"], row["mail_body"])
        self.assertIn("asi.iboga.jp", mailer.sent[0]["text"])

    def test_view_marker_suppresses_a_record_inserted_later(self):
        job_id = self.job("viewed-first")
        self.finish(job_id)
        self.store.mark_result_viewed(self.owner, job_id)
        self.store.set_job_notification(
            job_id, owner=self.owner, operation="scan", mode="notify",
            email=EMAIL)
        row = self.store.job_notification(job_id)
        self.assertEqual(row["status"], "suppressed")
        self.assertIsNone(row["email"])

    def test_gate_stops_a_queued_batch_member_viewed_mid_cycle(self):
        first = self.notify_job("gate-1")
        self.finish(first)
        second = self.notify_job("gate-2")
        self.finish(second)
        store = self.store

        class ViewingMailer(FakeMailer):
            def send(self, *, to, subject, text, idempotency_key=None,
                     sender=None):
                # While the first mail is being sent, the owner
                # views the second job's result in the app.
                store.mark_result_viewed(store_owner, second)
                return super().send(to=to, subject=subject, text=text,
                                    idempotency_key=idempotency_key,
                                    sender=sender)

        store_owner = self.owner
        mailer = ViewingMailer()
        self.assertEqual(self.delivery(mailer).run_once(), 1)
        self.assertEqual(len(mailer.sent), 1)  # only the first job
        self.assertEqual(self.store.job_notification(second)["status"],
                         "suppressed")

    def test_ipv6_base_url_round_trips(self):
        mailer = FakeMailer()
        delivery = NotificationDelivery(self.store, mailer,
                                        base_url="https://[::1]")
        self.assertEqual(delivery.base_url, "https://[::1]")
        delivery = NotificationDelivery(self.store, mailer,
                                        base_url="https://[::1]:8443/")
        self.assertEqual(delivery.base_url, "https://[::1]:8443")
        built = build_notification_delivery(
            self.store, {"AIQS_SI_NOTIFY_BASE_URL": "https://[::1]"},
            mailer=mailer)
        self.assertEqual(built.base_url, "https://[::1]")

    # --- fail-soft while unconfigured ---

    def test_cycle_without_delivery_only_escalates(self):
        job_id = self.notify_job(mode="wait")
        self.finish(job_id)
        finished_at = self.store.job_notification(job_id)["finished_at"]
        with self.store.transaction() as db:
            db.execute("UPDATE job_notifications SET finished_at=? "
                       "WHERE job=?",
                       (finished_at - ESCALATION_GRACE_SECONDS - 5, job_id))
        self.store.run_notification_cycle()  # no delivery attached
        self.assertEqual(self.store.job_notification(job_id)["status"],
                         "queued")

    def test_base_url_validation_is_strict_and_fail_soft(self):
        mailer = FakeMailer()
        with self.assertRaises(ValueError):
            NotificationDelivery(self.store, mailer,
                                 base_url="https://example.test/path")
        with self.assertRaises(ValueError):
            NotificationDelivery(self.store, mailer,
                                 base_url="https://[::1")
        built = build_notification_delivery(
            self.store, {"AIQS_SI_NOTIFY_BASE_URL":
                         "https://example.test/path"}, mailer=mailer)
        self.assertEqual(built.base_url, "https://asi.iboga.jp")
        built = build_notification_delivery(
            self.store, {"AIQS_SI_NOTIFY_BASE_URL":
                         "https://notify.example.test/"},
            mailer=mailer)
        self.assertEqual(built.base_url, "https://notify.example.test")

    def test_build_delivery_fail_soft(self):
        self.assertIsNone(build_notification_delivery(self.store, {}))
        self.assertIsNone(build_notification_delivery(
            self.store, {"AIQS_SI_RESEND_API_KEY": "  "}))
        built = build_notification_delivery(
            self.store, {"AIQS_SI_RESEND_API_KEY": "fixture-key"})
        self.assertIsInstance(built, NotificationDelivery)
        injected = build_notification_delivery(
            self.store, {}, mailer=FakeMailer())
        self.assertIsInstance(injected, NotificationDelivery)


class MaintenanceIsolationTests(unittest.TestCase):
    def test_wedged_notification_cycle_does_not_stall_maintenance(self):
        import asyncio
        import threading

        from aiqs_si.maintenance import Maintenance

        class WedgedStore:
            def __init__(self):
                self.release = threading.Event()
                self.expired = 0
                self.cycles = 0

            def expire_scans(self):
                self.expired += 1

            def run_notification_cycle(self):
                self.cycles += 1
                self.release.wait(30)

        store = WedgedStore()
        maintenance = Maintenance(store)
        maintenance.notification_timeout_seconds = 0.2

        async def three_checks():
            for _ in range(3):
                await maintenance.check()

        try:
            asyncio.run(three_checks())
            # Single-flight: the wedged cycle was entered once, was
            # never duplicated, and unrelated work ran every time.
            self.assertEqual(store.cycles, 1)
            self.assertEqual(store.expired, 3)
            self.assertTrue(maintenance.healthy)
        finally:
            store.release.set()
        # Once the wedged cycle drains, the next check runs it again.
        for _ in range(50):
            if maintenance._notification_future.done():
                break
            time.sleep(0.1)
        asyncio.run(maintenance.check())
        self.assertEqual(store.cycles, 2)


class IntentSchemaMigrationTests(unittest.TestCase):
    def test_pre_round4_database_upgrades_in_place(self):
        # Astra round-5 F12: a database whose intent table predates
        # intent_id/reference/consent gains the columns on open,
        # initialization is idempotent, and a legacy row is upgraded
        # — without manufactured provenance — on its next record.
        import sqlite3

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "legacy.sqlite"
        raw = sqlite3.connect(path)
        raw.execute(
            "CREATE TABLE app_submission_intents ("
            "owner TEXT NOT NULL, request_key TEXT NOT NULL, "
            "operation TEXT NOT NULL, mode TEXT NOT NULL, "
            "email TEXT, created_at REAL NOT NULL, "
            "PRIMARY KEY(owner, request_key))")
        raw.commit()
        raw.close()
        store = Store(path)
        Store(path)  # opening twice must stay a no-op
        owner = store.register_identity(
            "https://issuer.example.test/auth/v1", "user-legacy")
        with store.transaction() as db:
            columns = {row[1] for row in db.execute(
                "PRAGMA table_info(app_submission_intents)").fetchall()}
            db.execute(
                "INSERT INTO app_submission_intents(owner, request_key, "
                "intent_id, operation, mode, email, reference, consent, "
                "created_at) VALUES (?,?,NULL,'scan','notify',?,NULL,"
                "NULL,0)", (owner, "legacy-key", EMAIL))
        self.assertTrue({"intent_id", "reference", "consent"} <= columns)
        winner, created = store.record_app_submission_intent(
            owner, "legacy-key", operation="scan", mode="wait",
            email=None)
        self.assertFalse(created)
        self.assertEqual(winner["mode"], "notify")  # first record wins
        self.assertTrue(winner["intent_id"])


class IntentReconciliationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name) / "reconcile.sqlite")
        self.owner = self.store.register_identity(
            "https://issuer.example.test/auth/v1", "user-a")

    def test_sweep_attributes_a_crashed_scan_from_its_intent(self):
        intent, _created = self.store.record_app_submission_intent(
            self.owner, "sweep-1", operation="scan", mode="notify",
            email=EMAIL)
        job = self.store.submit(
            self.owner, "sweep-1",
            {"operation": "scan", "app_intent": intent["intent_id"]},
            cost=0)
        with self.store.transaction() as db:
            db.execute("UPDATE app_submission_intents SET created_at=0")
        self.assertIsNone(self.store.app_job(job["id"]))
        self.store.run_notification_cycle()
        self.assertIsNotNone(self.store.app_job(job["id"]))
        row = self.store.job_notification(job["id"])
        self.assertEqual(row["mode"], "notify")
        self.assertEqual(row["email"], EMAIL)
        self.assertIsNone(
            self.store.app_submission_intent(self.owner, "sweep-1"))

    def test_sweep_never_attributes_an_unmarked_job(self):
        self.store.record_app_submission_intent(
            self.owner, "sweep-2", operation="scan", mode="notify",
            email=EMAIL)
        job = self.store.submit(self.owner, "sweep-2",
                                {"operation": "scan"}, cost=0)
        with self.store.transaction() as db:
            db.execute("UPDATE app_submission_intents SET created_at=0")
        self.store.run_notification_cycle()
        self.assertIsNone(self.store.app_job(job["id"]))
        self.assertIsNone(self.store.job_notification(job["id"]))

    def test_reconciliation_survives_terminal_cleanup(self):
        # Astra round-5 F2: content minimization keeps the
        # app_intent stamp, so a scan that FAILED before the first
        # reconciliation pass is still attributable afterwards.
        intent, _created = self.store.record_app_submission_intent(
            self.owner, "sweep-3", operation="scan", mode="notify",
            email=EMAIL)
        job = self.store.submit(
            self.owner, "sweep-3",
            {"operation": "scan", "app_intent": intent["intent_id"]},
            cost=0)
        self.store.claim_next()
        self.store.finish(job["id"], "failed", {"ok": False})
        minimized = self.store.get_by_request(self.owner, "sweep-3")
        self.assertEqual(minimized["payload"].get("app_intent"),
                         intent["intent_id"])
        with self.store.transaction() as db:
            db.execute("UPDATE app_submission_intents SET created_at=0")
        self.store.run_notification_cycle()
        self.assertIsNotNone(self.store.app_job(job["id"]))
        row = self.store.job_notification(job["id"])
        self.assertEqual(row["mode"], "notify")
        self.assertEqual(row["status"], "queued")  # failure reported

    def test_sweep_finalizes_a_crashed_irradiation(self):
        # Astra round-5 F2: the intent's durable consent snapshot +
        # confirmation reference let maintenance finalize an
        # irradiation whose handler crashed after submission.
        scan = self.store.submit(self.owner, "sweep-scan",
                                 {"operation": "scan"}, cost=0)
        self.store.record_app_job(scan["id"], owner=self.owner,
                                  operation="scan")
        self.store.claim_next()
        self.store.finish(scan["id"], "completed", {"mode": "synthetic"})
        confirmation_id = self.store.create_app_confirmation(
            self.owner, scan["id"],
            {"count": 9, "credits_per_shot_tenths": 39, "credits": 0})
        self.store.claim_app_confirmation(self.owner, confirmation_id,
                                          "sweep-irr")
        consent = {"owner": self.owner, "subject": "user-a",
                   "client_id": "si-app", "confirmation_id": confirmation_id,
                   "consented_at": "2026-11-23T00:00:00+00:00",
                   "operation": "irradiation", "count": 9,
                   "credits_per_shot": 39, "total_credits": 35}
        intent, _created = self.store.record_app_submission_intent(
            self.owner, "sweep-irr", operation="irradiation",
            mode="notify", email=EMAIL, reference=confirmation_id,
            consent=consent)
        job = self.store.submit(
            self.owner, "sweep-irr",
            {"operation": "irradiation", "scan_id": scan["id"],
             "irradiation_count": 9, "app_intent": intent["intent_id"],
             "quote": {"credits": 0, "credits_per_shot": 3.9}},
            cost=0)
        with self.store.transaction() as db:
            db.execute("UPDATE app_submission_intents SET created_at=0")
        self.store.run_notification_cycle()
        attributed = self.store.app_job(job["id"])
        self.assertIsNotNone(attributed)
        self.assertEqual(attributed["consent"]["confirmation_id"],
                         confirmation_id)
        row = self.store.job_notification(job["id"])
        self.assertEqual(row["mode"], "notify")
        self.assertIsNone(
            self.store.app_submission_intent(self.owner, "sweep-irr"))
        confirmation = self.store.app_confirmation(self.owner,
                                                   confirmation_id)
        self.assertEqual(confirmation["used_job"], job["id"])

    def test_discarded_intent_blocks_an_in_flight_creation(self):
        # Astra round-6 F2: attempt A discards the intent while
        # attempt B (which resolved it as winner) is between record
        # and creation. B's creation must fail rather than commit a
        # job no reconciliation can ever find.
        intent, _created = self.store.record_app_submission_intent(
            self.owner, "inflight-1", operation="scan", mode="notify",
            email=EMAIL)
        self.store.discard_app_submission_intent(
            self.owner, "inflight-1", intent["intent_id"])
        with self.assertRaises(ValueError):
            self.store.submit(
                self.owner, "inflight-1",
                {"operation": "scan", "app_intent": intent["intent_id"]},
                cost=0, require_intent_id=intent["intent_id"])
        self.assertIsNone(
            self.store.get_by_request(self.owner, "inflight-1"))
        # Control: with the intent alive, the same creation succeeds.
        intent2, _created = self.store.record_app_submission_intent(
            self.owner, "inflight-2", operation="scan", mode="notify",
            email=EMAIL)
        job = self.store.submit(
            self.owner, "inflight-2",
            {"operation": "scan", "app_intent": intent2["intent_id"]},
            cost=0, require_intent_id=intent2["intent_id"])
        self.assertIsNotNone(job)

    def test_legacy_null_intent_never_attributes_a_foreign_job(self):
        # Astra round-6 F11: a migrated intent with NULL id and an
        # unmarked foreign job share "no stamp, no id" — that
        # absence must never count as a match. The NULL row exists
        # only in a database migrated from the pre-round-4 schema,
        # so the fixture builds exactly that shape.
        import sqlite3

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "legacy-race.sqlite"
        raw = sqlite3.connect(path)
        raw.execute(
            "CREATE TABLE app_submission_intents ("
            "owner TEXT NOT NULL, request_key TEXT NOT NULL, "
            "operation TEXT NOT NULL, mode TEXT NOT NULL, "
            "email TEXT, created_at REAL NOT NULL, "
            "PRIMARY KEY(owner, request_key))")
        raw.commit()
        raw.close()
        store = Store(path)
        owner = store.register_identity(
            "https://issuer.example.test/auth/v1", "user-legacy-race")
        with store.transaction() as db:
            db.execute(
                "INSERT INTO app_submission_intents(owner, request_key, "
                "operation, mode, email, created_at) VALUES (?,?,"
                "'scan','notify',?,?)",
                (owner, "legacy-race", EMAIL, time.time() - 3600))
        foreign = store.submit(owner, "legacy-race",
                               {"operation": "scan"}, cost=0)
        store.run_notification_cycle()
        self.assertIsNone(store.app_job(foreign["id"]))
        self.assertIsNone(store.job_notification(foreign["id"]))
        self.assertIsNotNone(
            store.app_submission_intent(owner, "legacy-race"))

    def test_sweep_rejects_a_foreign_irradiation(self):
        # Astra round-6 F14: a claimed confirmation and a durable
        # intent do not finalize a same-key irradiation that was
        # created by another channel for a different scan.
        scans = []
        for name in ("sweep-a", "sweep-b"):
            scan = self.store.submit(self.owner, name,
                                     {"operation": "scan"}, cost=0)
            self.store.record_app_job(scan["id"], owner=self.owner,
                                      operation="scan")
            self.store.claim_next()
            self.store.finish(scan["id"], "completed",
                              {"mode": "synthetic"})
            scans.append(scan)
        confirmation_id = self.store.create_app_confirmation(
            self.owner, scans[0]["id"],
            {"count": 9, "credits_per_shot_tenths": 39, "credits": 0})
        self.store.claim_app_confirmation(self.owner, confirmation_id,
                                          "sweep-foreign")
        self.store.record_app_submission_intent(
            self.owner, "sweep-foreign", operation="irradiation",
            mode="notify", email=EMAIL, reference=confirmation_id,
            consent={"confirmation_id": confirmation_id})
        foreign = self.store.submit(
            self.owner, "sweep-foreign",
            {"operation": "irradiation", "scan_id": scans[1]["id"],
             "irradiation_count": 9,
             "quote": {"credits": 0, "credits_per_shot": 3.9}},
            cost=0)
        with self.store.transaction() as db:
            db.execute("UPDATE app_submission_intents SET created_at=0")
        self.store.run_notification_cycle()
        self.assertIsNone(self.store.app_job(foreign["id"]))
        self.assertIsNone(self.store.job_notification(foreign["id"]))
        self.assertIsNotNone(self.store.app_submission_intent(
            self.owner, "sweep-foreign"))
        confirmation = self.store.app_confirmation(self.owner,
                                                   confirmation_id)
        self.assertEqual(confirmation["used_job"],
                         "intent:sweep-foreign")

    def _claimed_confirmation(self, key):
        scan = self.store.submit(self.owner, key + "-scan",
                                 {"operation": "scan"}, cost=0)
        self.store.record_app_job(scan["id"], owner=self.owner,
                                  operation="scan")
        self.store.claim_next()
        self.store.finish(scan["id"], "completed", {"mode": "synthetic"})
        confirmation_id = self.store.create_app_confirmation(
            self.owner, scan["id"],
            {"count": 9, "credits_per_shot_tenths": 39, "credits": 0})
        self.store.claim_app_confirmation(self.owner, confirmation_id, key)
        return scan, confirmation_id

    def test_release_keeps_the_claim_once_a_job_exists(self):
        # Astra round-7 F15: a failing same-key twin must not clear
        # the shared claim marker after the job was committed.
        _scan, confirmation_id = self._claimed_confirmation("f15-a")
        self.store.submit(self.owner, "f15-a",
                          {"operation": "scan"}, cost=0)
        self.store.release_app_confirmation_claim(
            self.owner, confirmation_id, "f15-a")
        confirmation = self.store.app_confirmation(self.owner,
                                                   confirmation_id)
        self.assertEqual(confirmation["used_job"], "intent:f15-a")

    def test_creation_validates_the_confirmation_claim(self):
        # Astra round-7 F15: if the claim WAS released before
        # creation, creation itself refuses — no charged job can be
        # committed against a claim nobody holds.
        _scan, confirmation_id = self._claimed_confirmation("f15-b")
        self.store.release_app_confirmation_claim(
            self.owner, confirmation_id, "f15-b")
        confirmation = self.store.app_confirmation(self.owner,
                                                   confirmation_id)
        self.assertIsNone(confirmation["used_job"])  # release worked
        with self.assertRaises(ValueError):
            self.store.submit(
                self.owner, "f15-b", {"operation": "scan"}, cost=0,
                require_confirmation_id=confirmation_id)
        self.assertIsNone(self.store.get_by_request(self.owner, "f15-b"))

    def test_gate_consults_the_durable_view_marker(self):
        job_id = self.store.submit(self.owner, "gate-marker",
                                   {"operation": "scan"}, cost=0)["id"]
        self.store.set_job_notification(
            job_id, owner=self.owner, operation="scan", mode="notify",
            email=EMAIL)
        self.store.claim_next()
        self.store.finish(job_id, "completed", {"ok": True})
        claimed = self.store.claim_completion_notifications(time.time())
        self.assertEqual(len(claimed), 1)
        # Only the marker lands (the notification row is untouched),
        # simulating a view whose record update never committed.
        with self.store.transaction() as db:
            db.execute("INSERT INTO job_result_views(job, owner, "
                       "viewed_at) VALUES (?,?,?)",
                       (job_id, self.owner, time.time()))
        self.assertEqual(
            self.store.notification_delivery_gate(
                job_id, claim_id=claimed[0]["claim_id"]), "suppressed")
        self.assertEqual(self.store.job_notification(job_id)["status"],
                         "suppressed")


class ResendMailerTests(unittest.TestCase):
    def test_send_success_and_status_failure(self):
        seen = {}

        def ok(request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json={"id": "fixture"})

        mailer = ResendMailer("fixture-key", "アイクスSI <asi@iboga.jp>",
                              transport=httpx.MockTransport(ok))
        mailer.send(to=EMAIL, subject="s", text="t")
        self.assertEqual(seen["url"], "https://api.resend.com/emails")
        self.assertEqual(seen["auth"], "Bearer fixture-key")

        def broken(request):
            return httpx.Response(500, json={})

        mailer = ResendMailer("fixture-key", "sender",
                              transport=httpx.MockTransport(broken))
        with self.assertRaises(MailDeliveryError) as caught:
            mailer.send(to=EMAIL, subject="s", text="t")
        self.assertEqual(str(caught.exception), "status_500")

    def test_invalid_recipient_never_reaches_transport(self):
        def handler(request):  # pragma: no cover - must not run
            raise AssertionError("transport called")

        mailer = ResendMailer("fixture-key", "sender",
                              transport=httpx.MockTransport(handler))
        with self.assertRaises(MailDeliveryError):
            mailer.send(to="not-an-address", subject="s", text="t")


class TemplateTests(unittest.TestCase):
    def test_override_and_bad_placeholder_fallback(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        base = Path(folder.name)
        (base / "completed_subject.txt").write_text(
            "{operation_label}が終わりました", encoding="utf-8")
        (base / "completed_body.txt").write_text(
            "本文 {unknown_placeholder}", encoding="utf-8")
        templates = load_templates(str(base))
        subject, text = render_completion(
            templates, outcome="completed", operation="irradiation",
            resume_url="https://asi.iboga.jp/app#resume=x")
        self.assertEqual(subject, "照射が終わりました")
        # The bad body fell back to the built-in wording.
        self.assertIn("照射が完了しました", text)
        self.assertIn("#resume=x", text)

    def test_attribute_traversal_override_is_rejected(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        base = Path(folder.name)
        (base / "completed_subject.txt").write_text(
            "{operation_label.missing} is not allowed", encoding="utf-8")
        (base / "completed_body.txt").write_text(
            "{resume_url:>999999}", encoding="utf-8")
        templates = load_templates(str(base))
        subject, text = render_completion(
            templates, outcome="completed", operation="scan",
            resume_url="https://asi.iboga.jp/app#resume=z")
        # Both parts fell back to the built-in wording.
        self.assertIn("スキャンが完了しました", subject)
        self.assertIn("#resume=z", text)

    def test_subject_cannot_carry_line_breaks(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        base = Path(folder.name)
        (base / "completed_subject.txt").write_text(
            "件名です\nBcc: someone@fixture.example.test", encoding="utf-8")
        templates = load_templates(str(base))
        subject, _text = render_completion(
            templates, outcome="completed", operation="scan",
            resume_url="https://asi.iboga.jp/app#resume=z")
        self.assertNotIn("\n", subject)
        self.assertNotIn("\r", subject)

    def test_builtin_templates_render(self):
        subject, text = render_completion(
            load_templates(None), outcome="failed", operation="scan",
            resume_url="https://asi.iboga.jp/app#resume=y")
        self.assertIn("スキャン", subject)
        self.assertIn("終了", subject)
        self.assertIn("#resume=y", text)


if __name__ == "__main__":
    unittest.main()
