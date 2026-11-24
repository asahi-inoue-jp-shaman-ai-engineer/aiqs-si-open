"""App API for completion notifications (foundation work item 2):
the per-submission completion choice on scan/irradiate, the
notify_unavailable honesty gate, the session availability flag,
the viewed-suppression through the job view, and the resume-token
redemption endpoint. Synthetic fixtures only; the mailer is a fake
and every address is a fixture domain."""
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from aiqs_si.application import create_application
from aiqs_si.auth import JWTVerifier
from aiqs_si.store import Store
from aiqs_si.worker import Worker
from aiqs_si_device.synthetic import SyntheticDevice
from prayer_fixtures import PHOTO, JOY, make_service as Service

CATALOG = {"field": [{"id": "one", "text": "test"}]}
EMAIL_A = "user-a@fixture.example.test"


class FakeMailer:
    def __init__(self):
        self.sent = []

    def send(self, *, to, subject, text, idempotency_key=None,
             sender=None):
        self.sent.append({"to": to, "subject": subject, "text": text,
                          "idempotency_key": idempotency_key})


class AppNotifyApiTests(unittest.TestCase):
    ISSUER = "https://issuer.example.test/auth/v1"
    AUDIENCE = "https://si.example.test/mcp"

    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name) / "app.sqlite")
        self.service = Service(self.store, CATALOG)
        self.owner_a = self.store.register_identity(self.ISSUER, "user-a")
        self.owner_b = self.store.register_identity(self.ISSUER, "user-b")
        with self.store.transaction() as db:
            db.execute("UPDATE accounts SET balance=500 WHERE id=?",
                       (self.owner_a,))
            db.execute("UPDATE accounts SET balance=500 WHERE id=?",
                       (self.owner_b,))
        self.mailer = FakeMailer()

    def token(self, subject, email=None):
        claims = {"iss": self.ISSUER, "aud": self.AUDIENCE, "sub": subject,
                  "client_id": "si-app", "scope": "aiqs:use",
                  "iat": int(time.time()), "exp": int(time.time()) + 600}
        if email is not None:
            claims["email"] = email
        return jwt.encode(claims, self.key, algorithm="RS256",
                          headers={"typ": "at+jwt"})

    def auth(self, subject="user-a", email=None):
        return {"Authorization": "Bearer " + self.token(subject, email)}

    def open(self, **kwargs):
        user = JWTVerifier(public_key=self.key.public_key(),
                           issuer=self.ISSUER, audience=self.AUDIENCE)
        worker = JWTVerifier(public_key=self.key.public_key(),
                             issuer=self.ISSUER,
                             audience="https://si.example.test/worker",
                             required_scope="aiqs:worker")
        with mock.patch.dict(os.environ, {"AIQS_SI_APP_ENABLED": "1"}):
            app = create_application(
                self.store, user, worker,
                allowed_workers={"synthetic-worker"}, service=self.service,
                **kwargs)
        return TestClient(app, base_url="http://127.0.0.1:8000",
                          raise_server_exceptions=False)

    def register_target(self, client, subject="user-a", email=None):
        response = client.post(
            "/api/app/targets", headers=self.auth(subject, email),
            json={"request_key": "target-1", "name": "Fixture Target",
                  "photo_base64": PHOTO})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["target_id"]

    def submit_scan(self, client, *, email=EMAIL_A, completion=None,
                    key="scan-1"):
        target_id = self.register_target(client, email=email)
        body = {"request_key": key, "target_ids": [target_id],
                "intention": "notify fixture", "self_love_joy": JOY}
        if completion is not None:
            body["completion"] = completion
        return client.post("/api/app/jobs/scan",
                           headers=self.auth(email=email), json=body)

    # --- session availability ---

    def test_session_reports_notify_availability(self):
        with self.open(notification_mailer=self.mailer) as client:
            view = client.get("/api/app/session",
                              headers=self.auth(email=EMAIL_A)).json()
            self.assertTrue(view["notify"]["available"])
            view = client.get("/api/app/session",
                              headers=self.auth()).json()
            self.assertFalse(view["notify"]["available"])
        with self.open() as client:  # no mailer configured at all
            view = client.get("/api/app/session",
                              headers=self.auth(email=EMAIL_A)).json()
            self.assertFalse(view["notify"]["available"])

    # --- the choice on submission ---

    def test_scan_notify_choice_is_recorded_from_the_token_claim(self):
        with self.open(notification_mailer=self.mailer) as client:
            response = self.submit_scan(client, completion={"mode": "notify"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["completion"],
                             {"mode": "notify", "notification": "armed",
                              "deliverable": True})
            row = self.store.job_notification(response.json()["job_id"])
            self.assertEqual(row["email"], EMAIL_A)
            self.assertEqual(row["operation"], "scan")

    def test_scan_defaults_to_wait_and_wait_captures_the_address(self):
        with self.open(notification_mailer=self.mailer) as client:
            response = self.submit_scan(client)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["completion"]["mode"], "wait")
            row = self.store.job_notification(response.json()["job_id"])
            # Wait mode still captures the address so an unviewed
            # result can escalate to mail instead of being dropped.
            self.assertEqual(row["email"], EMAIL_A)

    def test_completion_deliverable_follows_the_record(self):
        # Combined review finding 2: the client may promise a
        # completion mail only when THIS job's record can
        # actually be mailed. A wait-mode submission from a
        # token without an email claim records an armed row
        # with no address — the escalation requires one, so the
        # view must report deliverable=False even though the
        # lifecycle state is a promise state.
        with self.open(notification_mailer=self.mailer) as client:
            response = self.submit_scan(client, email=None)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["completion"],
                             {"mode": "wait", "notification": "armed",
                              "deliverable": False})
            row = self.store.job_notification(response.json()["job_id"])
            self.assertIsNone(row["email"])
        # Delivery unconfigured: an addressed record is still
        # not deliverable.
        with self.open() as client:
            response = self.submit_scan(client, key="scan-unconf")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["completion"]["notification"],
                             "armed")
            self.assertFalse(response.json()["completion"]["deliverable"])

    def test_completion_deliverable_dies_when_a_sending_record_is_viewed(self):
        # Combined review round 2, F2: a record claimed by the
        # dispatcher ('sending') keeps its status and address
        # when the result is viewed — the mail may already be
        # on the wire — but the delivery gate suppresses viewed
        # records, so the view must stop calling it deliverable.
        with self.open(notification_mailer=self.mailer) as client:
            response = self.submit_scan(client, completion={"mode": "notify"})
            job_id = response.json()["job_id"]
            Worker(self.store, SyntheticDevice()).run_one()
            # Freeze the record mid-delivery, in the state the
            # dispatcher leaves between claim and send.
            with self.store.transaction() as db:
                db.execute("UPDATE job_notifications SET status='sending' "
                           "WHERE job=?", (job_id,))
            view = client.get(f"/api/app/jobs/{job_id}",
                              headers=self.auth(email=EMAIL_A))
            self.assertEqual(view.json()["completion"],
                             {"mode": "notify", "notification": "sending",
                              "deliverable": True})
            ack = client.post(f"/api/app/jobs/{job_id}/viewed",
                              headers=self.auth(email=EMAIL_A), json={})
            self.assertEqual(ack.status_code, 200, ack.text)
            self.assertEqual(ack.json()["completion"],
                             {"mode": "notify", "notification": "sending",
                              "deliverable": False})

    def test_notify_refused_before_submission_when_unconfigured(self):
        with self.open() as client:  # delivery not configured
            response = self.submit_scan(client, completion={"mode": "notify"})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"], "notify_unavailable")
            self.assertIsNone(self.store.get_by_request(self.owner_a, "scan-1"))

    def test_notify_refused_without_an_email_claim(self):
        with self.open(notification_mailer=self.mailer) as client:
            target_id = self.register_target(client)
            response = client.post(
                "/api/app/jobs/scan", headers=self.auth(),
                json={"request_key": "scan-noemail",
                      "target_ids": [target_id], "intention": "x",
                      "self_love_joy": JOY,
                      "completion": {"mode": "notify"}})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"], "notify_unavailable")
            self.assertIsNone(
                self.store.get_by_request(self.owner_a, "scan-noemail"))

    def test_malformed_completion_is_a_client_error(self):
        with self.open(notification_mailer=self.mailer) as client:
            for bad in ({"mode": "later"}, "notify", {"mode": 3}):
                response = self.submit_scan(client, completion=bad,
                                            key="scan-bad")
                self.assertEqual(response.status_code, 400, bad)
                self.assertEqual(response.json()["error"],
                                 "invalid_completion_mode")
            self.assertIsNone(self.store.get_by_request(self.owner_a,
                                                        "scan-bad"))

    def test_replay_keeps_the_first_choice(self):
        with self.open(notification_mailer=self.mailer) as client:
            first = self.submit_scan(client, completion={"mode": "wait"})
            job_id = first.json()["job_id"]
            replay = self.submit_scan(client, completion={"mode": "notify"})
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json()["job_id"], job_id)
            self.assertEqual(replay.json()["completion"]["mode"], "wait")
            self.assertEqual(self.store.job_notification(job_id)["mode"],
                             "wait")

    # --- viewed suppression through the job view ---

    def test_viewing_the_finished_job_suppresses_the_mail(self):
        # Batch 2d, MEDIUM 6: the status GET alone no longer
        # records a view — the wait screens poll it, and a poll
        # landing just after completion used to suppress the
        # completion mail for a user who never saw the result.
        # The explicit ack is what suppresses the mail now.
        with self.open(notification_mailer=self.mailer) as client:
            response = self.submit_scan(client, completion={"mode": "notify"})
            job_id = response.json()["job_id"]
            Worker(self.store, SyntheticDevice()).run_one()
            self.assertEqual(
                self.store.job_notification(job_id)["status"], "queued")
            view = client.get(f"/api/app/jobs/{job_id}",
                              headers=self.auth(email=EMAIL_A))
            self.assertEqual(view.status_code, 200, view.text)
            self.assertEqual(view.json()["state"], "completed")
            self.assertEqual(view.json()["completion"],
                             {"mode": "notify", "notification": "queued",
                              "deliverable": True})
            ack = client.post(f"/api/app/jobs/{job_id}/viewed",
                              headers=self.auth(email=EMAIL_A), json={})
            self.assertEqual(ack.status_code, 200, ack.text)
            self.assertEqual(ack.json()["viewed"], True)
            self.assertEqual(ack.json()["completion"],
                             {"mode": "notify", "notification": "suppressed",
                              "deliverable": False})
            self.store.run_notification_cycle()
            self.assertEqual(self.mailer.sent, [])

    def test_polling_without_ack_keeps_the_completion_mail(self):
        # The MEDIUM 6 failure shape end to end: the wait screen
        # polls the finished job (GET), the user never renders
        # the result (no ack), and the mail still goes out.
        with self.open(notification_mailer=self.mailer) as client:
            response = self.submit_scan(client, completion={"mode": "notify"})
            job_id = response.json()["job_id"]
            Worker(self.store, SyntheticDevice()).run_one()
            for _ in range(3):
                view = client.get(f"/api/app/jobs/{job_id}",
                                  headers=self.auth(email=EMAIL_A))
                self.assertEqual(view.status_code, 200, view.text)
            self.store.run_notification_cycle()
            self.assertEqual(len(self.mailer.sent), 1)

    def test_ack_for_an_in_flight_job_records_nothing(self):
        # The ack endpoint refuses to record a view for a job
        # that has not finished: the marker would cancel the
        # completion mail the user is still owed.
        with self.open(notification_mailer=self.mailer) as client:
            response = self.submit_scan(client, completion={"mode": "notify"})
            job_id = response.json()["job_id"]
            ack = client.post(f"/api/app/jobs/{job_id}/viewed",
                              headers=self.auth(email=EMAIL_A), json={})
            self.assertEqual(ack.status_code, 200, ack.text)
            self.assertEqual(ack.json()["viewed"], False)
            Worker(self.store, SyntheticDevice()).run_one()
            self.store.run_notification_cycle()
            self.assertEqual(len(self.mailer.sent), 1)

    def test_ack_for_an_unknown_job_records_nothing(self):
        # Round 2 (Astra): 'unknown' is not an acknowledgeable
        # outcome. The owner who opens an unknown job has seen
        # uncertainty, not a result; recording that view
        # suppressed the notification of the later RESOLVED
        # outcome permanently. The ack is refused, and when the
        # job is resolved the mail still goes out.
        with self.open(notification_mailer=self.mailer) as client:
            response = self.submit_scan(client, completion={"mode": "notify"})
            job_id = response.json()["job_id"]
            claimed = self.store.claim_next(worker="synthetic-worker")
            self.assertEqual(claimed["id"], job_id)
            self.store.finish(job_id, "unknown",
                              {"reason": "execution_outcome_unknown"},
                              worker="synthetic-worker")
            ack = client.post(f"/api/app/jobs/{job_id}/viewed",
                              headers=self.auth(email=EMAIL_A), json={})
            self.assertEqual(ack.status_code, 200, ack.text)
            self.assertEqual(ack.json()["viewed"], False)
            self.assertNotEqual(
                self.store.job_notification(job_id)["status"], "suppressed")
            self.store.resolve_unknown(
                job_id, completed_count=0,
                stop_evidence_digest="0" * 64)
            self.store.run_notification_cycle()
            self.assertEqual(len(self.mailer.sent), 1)

    def test_ack_requires_ownership(self):
        with self.open(notification_mailer=self.mailer) as client:
            response = self.submit_scan(client, completion={"mode": "notify"})
            job_id = response.json()["job_id"]
            Worker(self.store, SyntheticDevice()).run_one()
            ack = client.post(
                f"/api/app/jobs/{job_id}/viewed",
                headers=self.auth("user-b",
                                  email="user-b@fixture.example.test"),
                json={})
            self.assertEqual(ack.status_code, 404, ack.text)
            self.store.run_notification_cycle()
            self.assertEqual(len(self.mailer.sent), 1)

    def test_job_view_reports_removed_scan_content(self):
        # Batch 2d, MEDIUM 8: after the 24-hour scan expiry, the
        # job view must say the content is gone — the client
        # lands the user on the expired screen instead of an
        # empty ranking presented as a success.
        import time as _time
        with self.open(notification_mailer=self.mailer) as client:
            response = self.submit_scan(client, completion={"mode": "wait"})
            job_id = response.json()["job_id"]
            Worker(self.store, SyntheticDevice()).run_one()
            view = client.get(f"/api/app/jobs/{job_id}",
                              headers=self.auth(email=EMAIL_A))
            self.assertEqual(view.json()["content_removed"], False)
            self.assertIsNotNone(view.json()["result"])
            removed = self.store.expire_scans(now=_time.time() + 90000)
            self.assertEqual(removed, 1)
            view = client.get(f"/api/app/jobs/{job_id}",
                              headers=self.auth(email=EMAIL_A))
            self.assertEqual(view.status_code, 200, view.text)
            self.assertEqual(view.json()["state"], "completed")
            self.assertEqual(view.json()["content_removed"], True)
            self.assertIsNone(view.json()["result"])

    # --- resume redemption ---

    def delivered_token(self, client):
        response = self.submit_scan(client, completion={"mode": "notify"})
        job_id = response.json()["job_id"]
        Worker(self.store, SyntheticDevice()).run_one()
        self.store.run_notification_cycle()
        self.assertEqual(len(self.mailer.sent), 1)
        mail = self.mailer.sent[0]
        self.assertEqual(mail["to"], EMAIL_A)
        token = re.search(r"#resume=([A-Za-z0-9_-]+)", mail["text"]).group(1)
        return job_id, token

    def test_resume_redeems_for_the_owner_only(self):
        with self.open(notification_mailer=self.mailer) as client:
            job_id, token = self.delivered_token(client)
            redeemed = client.post("/api/app/resume",
                                   headers=self.auth(email=EMAIL_A),
                                   json={"token": token})
            self.assertEqual(redeemed.status_code, 200, redeemed.text)
            self.assertEqual(redeemed.json(),
                             {"job_id": job_id, "operation": "scan"})
            # The link still works on a second click (mail prefetch /
            # re-open), and the job view behind the session serves it.
            again = client.post("/api/app/resume",
                                headers=self.auth(email=EMAIL_A),
                                json={"token": token})
            self.assertEqual(again.status_code, 200)
            view = client.get(f"/api/app/jobs/{job_id}",
                              headers=self.auth(email=EMAIL_A))
            self.assertEqual(view.json()["state"], "completed")
            # Another account learns nothing from the same token.
            other = client.post("/api/app/resume",
                                headers=self.auth("user-b"),
                                json={"token": token})
            self.assertEqual(other.status_code, 404)
            self.assertEqual(other.json()["error"], "resume_not_found")
            # Unknown and malformed tokens.
            unknown = client.post("/api/app/resume",
                                  headers=self.auth(email=EMAIL_A),
                                  json={"token": "z" * 43})
            self.assertEqual(unknown.status_code, 404)
            malformed = client.post("/api/app/resume",
                                    headers=self.auth(email=EMAIL_A),
                                    json={"token": "short"})
            self.assertEqual(malformed.status_code, 400)
            self.assertEqual(malformed.json()["error"], "resume_invalid")
            anonymous = client.post("/api/app/resume", json={"token": token})
            self.assertEqual(anonymous.status_code, 401)

    # --- irradiation ---

    def completed_scan(self, client):
        response = self.submit_scan(client, key="scan-for-irr")
        scan_id = response.json()["job_id"]
        Worker(self.store, SyntheticDevice()).run_one()
        return scan_id

    def test_irradiate_notify_choice_and_unavailable_gate(self):
        with self.open(notification_mailer=self.mailer) as client:
            scan_id = self.completed_scan(client)
            prepared = client.post("/api/app/jobs/irradiate/prepare",
                                   headers=self.auth(email=EMAIL_A),
                                   json={"scan_id": scan_id, "count": 3})
            confirmation_id = prepared.json()["confirmation_id"]
            response = client.post(
                "/api/app/jobs/irradiate", headers=self.auth(email=EMAIL_A),
                json={"request_key": "irr-1",
                      "confirmation_id": confirmation_id,
                      "completion": {"mode": "notify"}})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["completion"],
                             {"mode": "notify", "notification": "armed",
                              "deliverable": True})
            row = self.store.job_notification(response.json()["job_id"])
            self.assertEqual(row["operation"], "irradiation")
            self.assertEqual(row["email"], EMAIL_A)

    def test_scan_replay_survives_losing_the_email_claim(self):
        # Astra round-2 F9: an accepted notify scan replayed with a
        # token that no longer carries the claim must still answer
        # with the job and its stored choice.
        with self.open(notification_mailer=self.mailer) as client:
            first = self.submit_scan(client, completion={"mode": "notify"})
            job_id = first.json()["job_id"]
            target_id = self.register_target(client)
            replay = client.post(
                "/api/app/jobs/scan", headers=self.auth(),
                json={"request_key": "scan-1", "target_ids": [target_id],
                      "intention": "notify fixture", "self_love_joy": JOY,
                      "completion": {"mode": "notify"}})
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json()["job_id"], job_id)
            self.assertEqual(replay.json()["completion"]["mode"], "notify")

    def test_scan_crash_recovery_uses_the_submission_intent(self):
        # Astra round-3 F2: intent written, job submitted, then the
        # attempt crashed before attribution. The retry must NOT be
        # refused as a foreign channel, and the recorded choice must
        # be the intent's original one — not what the retry presents.
        with self.open(notification_mailer=self.mailer) as client:
            target_id = self.register_target(client, email=EMAIL_A)
            intent, _created = self.store.record_app_submission_intent(
                self.owner_a, "crash-1", operation="scan", mode="notify",
                email=EMAIL_A)
            job = self.service.submit_scan(
                self.owner_a, "crash-1", "crash fixture", [target_id],
                self_love_joy=JOY, app_intent_id=intent["intent_id"])
            self.assertIsNone(self.store.app_job(job["id"]))
            retry = client.post(
                "/api/app/jobs/scan", headers=self.auth(),
                json={"request_key": "crash-1", "target_ids": [target_id],
                      "intention": "crash fixture", "self_love_joy": JOY,
                      "completion": {"mode": "wait"}})
            self.assertEqual(retry.status_code, 200, retry.text)
            self.assertEqual(retry.json()["job_id"], job["id"])
            self.assertEqual(retry.json()["completion"]["mode"], "notify")
            row = self.store.job_notification(job["id"])
            self.assertEqual(row["mode"], "notify")
            self.assertEqual(row["email"], EMAIL_A)
            self.assertIsNotNone(self.store.app_job(job["id"]))
            # The intent was consumed by the attribution.
            self.assertIsNone(
                self.store.app_submission_intent(self.owner_a, "crash-1"))

    def test_rejected_submission_cannot_bless_a_foreign_job(self):
        # Astra round-4 F11: an app attempt rejected by the service
        # discards its intent, so a later foreign-channel job that
        # reuses the key is NOT attributed by an app replay.
        with self.open(notification_mailer=self.mailer) as client:
            target_id = self.register_target(client, email=EMAIL_A)
            rejected = client.post(
                "/api/app/jobs/scan", headers=self.auth(email=EMAIL_A),
                json={"request_key": "shared-key",
                      "target_ids": ["no-such-target"],
                      "intention": "x", "self_love_joy": JOY,
                      "completion": {"mode": "notify"}})
            self.assertEqual(rejected.status_code, 404, rejected.text)
            self.assertIsNone(
                self.store.app_submission_intent(self.owner_a, "shared-key"))
            # Another channel creates a scan under the same key.
            foreign = self.service.submit_scan(
                self.owner_a, "shared-key", "foreign fixture",
                [target_id], self_love_joy=JOY)
            replay = client.post(
                "/api/app/jobs/scan", headers=self.auth(email=EMAIL_A),
                json={"request_key": "shared-key",
                      "target_ids": [target_id],
                      "intention": "foreign fixture", "self_love_joy": JOY,
                      "completion": {"mode": "notify"}})
            self.assertEqual(replay.status_code, 409, replay.text)
            self.assertIsNone(self.store.app_job(foreign["id"]))
            self.assertIsNone(self.store.job_notification(foreign["id"]))

    def test_winning_intent_governs_a_fresh_submission(self):
        # Astra round-4 F2: an earlier attempt's durable intent —
        # not this request's presentation — is what gets recorded.
        with self.open(notification_mailer=self.mailer) as client:
            target_id = self.register_target(client, email=EMAIL_A)
            self.store.record_app_submission_intent(
                self.owner_a, "intent-key", operation="scan",
                mode="notify", email=EMAIL_A)
            response = client.post(
                "/api/app/jobs/scan", headers=self.auth(),
                json={"request_key": "intent-key",
                      "target_ids": [target_id],
                      "intention": "winner fixture", "self_love_joy": JOY,
                      "completion": {"mode": "wait"}})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["completion"]["mode"],
                             "notify")
            row = self.store.job_notification(response.json()["job_id"])
            self.assertEqual(row["mode"], "notify")
            self.assertEqual(row["email"], EMAIL_A)

    def test_availability_policy_applies_to_the_winning_intent(self):
        # Astra round-5 F2: the fresh-submission availability check
        # judges the WINNING intent, not the presented choice.
        with self.open() as client:  # no mailer: delivery unconfigured
            target_id = self.register_target(client, email=EMAIL_A)
            # A durable notify intent refuses even a wait retry.
            self.store.record_app_submission_intent(
                self.owner_a, "winner-notify", operation="scan",
                mode="notify", email=EMAIL_A)
            refused = client.post(
                "/api/app/jobs/scan", headers=self.auth(email=EMAIL_A),
                json={"request_key": "winner-notify",
                      "target_ids": [target_id],
                      "intention": "winner fixture", "self_love_joy": JOY,
                      "completion": {"mode": "wait"}})
            self.assertEqual(refused.status_code, 400, refused.text)
            self.assertEqual(refused.json()["error"], "notify_unavailable")
            self.assertIsNone(
                self.store.get_by_request(self.owner_a, "winner-notify"))
            # A durable wait intent is never blocked by a presented
            # notify the caller cannot honour.
            self.store.record_app_submission_intent(
                self.owner_a, "winner-wait", operation="scan",
                mode="wait", email=None)
            accepted = client.post(
                "/api/app/jobs/scan", headers=self.auth(),
                json={"request_key": "winner-wait",
                      "target_ids": [target_id],
                      "intention": "winner fixture", "self_love_joy": JOY,
                      "completion": {"mode": "notify"}})
            self.assertEqual(accepted.status_code, 200, accepted.text)
            self.assertEqual(accepted.json()["completion"]["mode"], "wait")

    def test_foreign_job_winning_the_submit_race_is_not_attributed(self):
        # Astra round-5 F11: a foreign job created between the API
        # lookup and the service lookup is returned by submit_scan;
        # attribution must refuse it inside the store transaction.
        with self.open(notification_mailer=self.mailer) as client:
            target_id = self.register_target(client, email=EMAIL_A)
            original = self.service.submit_scan

            def racing(owner, key, intention, targets, **kwargs):
                original(owner, key, intention, targets,
                         self_love_joy=kwargs.get("self_love_joy"))
                return original(owner, key, intention, targets, **kwargs)

            self.service.submit_scan = racing
            try:
                response = client.post(
                    "/api/app/jobs/scan", headers=self.auth(email=EMAIL_A),
                    json={"request_key": "race-key",
                          "target_ids": [target_id],
                          "intention": "race fixture", "self_love_joy": JOY,
                          "completion": {"mode": "notify"}})
            finally:
                del self.service.submit_scan
            self.assertEqual(response.status_code, 409, response.text)
            foreign = self.store.get_by_request(self.owner_a, "race-key")
            self.assertIsNotNone(foreign)
            self.assertIsNone(self.store.app_job(foreign["id"]))
            self.assertIsNone(self.store.job_notification(foreign["id"]))

    def test_intent_binding_rejects_cross_operation_and_confirmation(self):
        # Astra round-5 F13: operation and confirmation reference
        # are part of the intent binding.
        with self.open(notification_mailer=self.mailer) as client:
            target_id = self.register_target(client, email=EMAIL_A)
            self.store.record_app_submission_intent(
                self.owner_a, "bound-key", operation="irradiation",
                mode="notify", email=EMAIL_A, reference="conf-x")
            scan = client.post(
                "/api/app/jobs/scan", headers=self.auth(email=EMAIL_A),
                json={"request_key": "bound-key",
                      "target_ids": [target_id],
                      "intention": "bound fixture", "self_love_joy": JOY,
                      "completion": {"mode": "notify"}})
            self.assertEqual(scan.status_code, 409, scan.text)
            self.assertIsNone(
                self.store.get_by_request(self.owner_a, "bound-key"))
            # Same key, different confirmation than the intent's.
            scan_id = self.completed_scan(client)
            confirmations = []
            for _ in range(2):
                prepared = client.post(
                    "/api/app/jobs/irradiate/prepare",
                    headers=self.auth(email=EMAIL_A),
                    json={"scan_id": scan_id, "count": 9})
                confirmations.append(prepared.json()["confirmation_id"])
            self.store.record_app_submission_intent(
                self.owner_a, "irr-bound", operation="irradiation",
                mode="notify", email=EMAIL_A,
                reference=confirmations[0])
            mismatched = client.post(
                "/api/app/jobs/irradiate", headers=self.auth(email=EMAIL_A),
                json={"request_key": "irr-bound",
                      "confirmation_id": confirmations[1],
                      "completion": {"mode": "wait"}})
            self.assertEqual(mismatched.status_code, 409, mismatched.text)
            confirmation_b = self.store.app_confirmation(
                self.owner_a, confirmations[1])
            self.assertIsNone(confirmation_b["used_job"])
            self.assertIsNone(
                self.store.get_by_request(self.owner_a, "irr-bound"))

    def test_winner_operation_is_rechecked_after_the_record(self):
        # Astra round-6 F13: an irradiation intent inserted between
        # the scan handler's initial lookup and its record call
        # becomes the winner — the scan must refuse the key instead
        # of consuming another operation's intent.
        with self.open(notification_mailer=self.mailer) as client:
            target_id = self.register_target(client, email=EMAIL_A)
            original = self.store.record_app_submission_intent

            def interleave(owner, key, **kwargs):
                if kwargs.get("operation") == "scan":
                    original(owner, key, operation="irradiation",
                             mode="notify", email=EMAIL_A,
                             reference="conf-z")
                return original(owner, key, **kwargs)

            self.store.record_app_submission_intent = interleave
            try:
                response = client.post(
                    "/api/app/jobs/scan", headers=self.auth(email=EMAIL_A),
                    json={"request_key": "interleave-key",
                          "target_ids": [target_id],
                          "intention": "interleave fixture",
                          "self_love_joy": JOY,
                          "completion": {"mode": "notify"}})
            finally:
                del self.store.record_app_submission_intent
            self.assertEqual(response.status_code, 409, response.text)
            self.assertIsNone(
                self.store.get_by_request(self.owner_a, "interleave-key"))
            intent = self.store.app_submission_intent(
                self.owner_a, "interleave-key")
            self.assertEqual(intent["operation"], "irradiation")

    def test_irradiate_recovery_preserves_an_existing_notify_intent(self):
        # Astra round-2 F7, as redesigned in round 4 and renamed
        # in round 7: a pre-existing durable intent stands
        # verbatim at late-attribution recovery (it was recorded
        # as a valid promise); only an intent created by the
        # recovering request itself downgrades to wait while
        # delivery is unconfigured.
        with self.open() as client:  # delivery not configured
            scan_id = self.completed_scan(client)
            prepared = client.post("/api/app/jobs/irradiate/prepare",
                                   headers=self.auth(email=EMAIL_A),
                                   json={"scan_id": scan_id, "count": 3})
            confirmation_id = prepared.json()["confirmation_id"]
            # Simulate the crashed first attempt, in the shape the
            # real flow leaves behind: the intent was recorded, the
            # confirmation claimed, and the job submitted WITH the
            # intent stamp — but attribution never landed.
            intent, _created = self.store.record_app_submission_intent(
                self.owner_a, "irr-rec", operation="irradiation",
                mode="notify", email=EMAIL_A, reference=confirmation_id)
            self.store.claim_app_confirmation(self.owner_a, confirmation_id,
                                              "irr-rec")
            job = self.service.submit_irradiation(
                self.owner_a, "irr-rec", scan_id, count=3,
                app_intent_id=intent["intent_id"],
                confirmation_id=confirmation_id)
            response = client.post(
                "/api/app/jobs/irradiate", headers=self.auth(email=EMAIL_A),
                json={"request_key": "irr-rec",
                      "confirmation_id": confirmation_id,
                      "completion": {"mode": "notify"}})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["job_id"], job["id"])
            # The pre-existing intent stands verbatim (it was
            # recorded as a valid promise); only an intent created
            # by the recovering request itself downgrades to wait.
            # Deliverable is False HERE, though (combined review
            # finding 2): this app instance has no delivery
            # configured, so the recorded promise cannot be
            # honoured by it and the client must not repeat it.
            self.assertEqual(response.json()["completion"],
                             {"mode": "notify", "notification": "armed",
                              "deliverable": False})
            row = self.store.job_notification(job["id"])
            self.assertEqual(row["mode"], "notify")

    def test_unstamped_foreign_irradiation_is_not_recovered(self):
        # Astra round-7 F14: identical terms do not prove origin.
        # An unstamped same-key irradiation is refused by API
        # recovery even with the intent and claim in place.
        with self.open(notification_mailer=self.mailer) as client:
            scan_id = self.completed_scan(client)
            prepared = client.post("/api/app/jobs/irradiate/prepare",
                                   headers=self.auth(email=EMAIL_A),
                                   json={"scan_id": scan_id, "count": 3})
            confirmation_id = prepared.json()["confirmation_id"]
            self.store.record_app_submission_intent(
                self.owner_a, "irr-foreign", operation="irradiation",
                mode="notify", email=EMAIL_A, reference=confirmation_id)
            self.store.claim_app_confirmation(self.owner_a, confirmation_id,
                                              "irr-foreign")
            foreign = self.service.submit_irradiation(
                self.owner_a, "irr-foreign", scan_id, count=3)
            response = client.post(
                "/api/app/jobs/irradiate", headers=self.auth(email=EMAIL_A),
                json={"request_key": "irr-foreign",
                      "confirmation_id": confirmation_id,
                      "completion": {"mode": "notify"}})
            self.assertEqual(response.status_code, 409, response.text)
            self.assertIsNone(self.store.app_job(foreign["id"]))
            self.assertIsNone(self.store.job_notification(foreign["id"]))
            self.assertIsNotNone(self.store.app_submission_intent(
                self.owner_a, "irr-foreign"))
            confirmation = self.store.app_confirmation(
                self.owner_a, confirmation_id)
            self.assertEqual(confirmation["used_job"],
                             "intent:irr-foreign")

    def test_irradiate_notify_unavailable_consumes_nothing(self):
        with self.open() as client:  # delivery not configured
            scan_id = self.completed_scan(client)
            prepared = client.post("/api/app/jobs/irradiate/prepare",
                                   headers=self.auth(email=EMAIL_A),
                                   json={"scan_id": scan_id, "count": 3})
            confirmation_id = prepared.json()["confirmation_id"]
            refused = client.post(
                "/api/app/jobs/irradiate", headers=self.auth(email=EMAIL_A),
                json={"request_key": "irr-1",
                      "confirmation_id": confirmation_id,
                      "completion": {"mode": "notify"}})
            self.assertEqual(refused.status_code, 400)
            self.assertEqual(refused.json()["error"], "notify_unavailable")
            self.assertIsNone(self.store.get_by_request(self.owner_a, "irr-1"))
            # The same confirmation still works with wait mode.
            accepted = client.post(
                "/api/app/jobs/irradiate", headers=self.auth(email=EMAIL_A),
                json={"request_key": "irr-1",
                      "confirmation_id": confirmation_id,
                      "completion": {"mode": "wait"}})
            self.assertEqual(accepted.status_code, 200, accepted.text)


if __name__ == "__main__":
    unittest.main()
