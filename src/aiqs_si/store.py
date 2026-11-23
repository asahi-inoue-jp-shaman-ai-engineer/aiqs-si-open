"""Local job and test-credit ledger. No production accounts or payment provider."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import uuid
import time
from .catalog_store import CatalogStore
from .dplanet import DPlanetError
from .notifications import (CLAIM_STALE_SECONDS, ESCALATION_GRACE_SECONDS,
                            MAX_DELIVERY_ATTEMPTS, NOTIFICATION_TTL_SECONDS,
                            plausible_email)
from .grants import GrantStore
from .policies import load_grant_policy, load_limit_policy
from .referral import ReferralStore
from .pricing import purchase_quote, rate_display, session_charge_credits
from .result_schema import (public_bool, public_count, public_mode,
                            validate_worker_result)

# Historical default grant amounts. The effective values come from
# the grant policy (policies.load_grant_policy); these constants stay
# as the documented defaults other modules import.
SIGNUP_CAMPAIGN_CREDITS = 0
MIGRATION_CREDITS = 100
REFERRAL_CREDITS = 100
# Flat loyalty gift applied by an operator-invoked legacy-balance
# replacement measure; the effective value comes from the grant
# policy, this constant documents the default other modules import.
LEGACY_LOYALTY_GIFT_CREDITS = 100

DEVICE_ID_KEY_ENV = "AIQS_SI_DEVICE_ID_KEY"
PROFILE_SALT_ENV = "AIQS_SI_PROFILE_SALT"


def _display_text(value):
    """A string the app surface can hand back, or None: display
    values must survive strict UTF-8 encoding at response render
    time, so an unpaired surrogate from legacy storage reads as
    absent instead of breaking the whole response."""
    if not isinstance(value, str):
        return None
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return value


class ReadingChargeError(ValueError):
    """A reading reservation failed under a fixed error code.

    The code is one of the native-reading design §4.3 fixed codes
    (reading_insufficient_balance / reading_daily_cap /
    confirmation_* / quote_changed / reading_unavailable); the API
    layer answers with it verbatim and the app maps it to its fixed
    template text. Carries no amounts or provider detail.
    """

    def __init__(self, code):
        super().__init__(code)
        self.code = code


# Sentinel for record_app_job's provenance requirement: "not
# requested" (no check) is distinct from "required but missing"
# (fail closed), per Astra round-6 F11.
_PROVENANCE_NOT_REQUESTED = object()


class Store(CatalogStore, GrantStore, ReferralStore):
    def __init__(self, path, *, dplanet_client=None, dplanet_routing_enabled=False,
                 grant_policy=None, limit_policy=None):
        self.path = str(path)
        # D-Planet consumption routing (design §4) is opt-in per process:
        # without a client or with the flag off, every credit movement
        # stays on the SI ledger exactly as before linking existed.
        self._dplanet_client = dplanet_client
        self._dplanet_routing_enabled = bool(dplanet_routing_enabled)
        # Operator policies (OSS plan v4 Phase A): grant amounts and
        # invariants, scan/claim limits. Loaded from env JSON when the
        # composition does not inject them; defaults are the
        # historical values, so an unconfigured deployment is
        # unchanged.
        self.grant_policy = grant_policy if grant_policy is not None else load_grant_policy()
        self.limit_policy = limit_policy if limit_policy is not None else load_limit_policy()
        # Completion-notification delivery (notifications.py), attached
        # by the composition root when a sender is configured. None is
        # the fail-soft state: choices are recorded and escalate, but
        # no mail is promised or sent.
        self.notification_delivery = None
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY, balance INTEGER NOT NULL CHECK(balance >= 0));
                CREATE TABLE IF NOT EXISTS identities (
                    issuer TEXT NOT NULL, subject TEXT NOT NULL,
                    owner TEXT NOT NULL REFERENCES accounts(id),
                    PRIMARY KEY(issuer, subject));
                CREATE TABLE IF NOT EXISTS identity_links (
                    issuer TEXT NOT NULL, subject TEXT NOT NULL,
                    upstream_issuer TEXT NOT NULL, upstream_subject TEXT NOT NULL,
                    PRIMARY KEY(issuer,subject),
                    FOREIGN KEY(issuer,subject) REFERENCES identities(issuer,subject),
                    FOREIGN KEY(upstream_issuer,upstream_subject) REFERENCES identities(issuer,subject));
                CREATE TABLE IF NOT EXISTS identity_migrations (
                    issuer TEXT PRIMARY KEY, manifest_digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS identity_admissions (
                    issuer TEXT NOT NULL, subject TEXT NOT NULL,
                    disposition TEXT NOT NULL CHECK(disposition IN ('approved_new','quarantined')),
                    evidence_digest TEXT NOT NULL, PRIMARY KEY(issuer,subject));
                CREATE TABLE IF NOT EXISTS migration_awards (
                    owner TEXT PRIMARY KEY REFERENCES accounts(id));
                CREATE TABLE IF NOT EXISTS invitation_codes (
                    owner TEXT PRIMARY KEY REFERENCES accounts(id), code TEXT NOT NULL UNIQUE);
                CREATE TABLE IF NOT EXISTS registration_referrals (
                    invitee TEXT PRIMARY KEY REFERENCES accounts(id),
                    code TEXT REFERENCES invitation_codes(code));
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES accounts(id),
                    request_key TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,
                    cost INTEGER NOT NULL CHECK(cost >= 0),
                    state TEXT NOT NULL CHECK(state IN ('queued','running','completed','failed','cancelled','unknown')),
                    result TEXT, UNIQUE(owner, request_key));
                CREATE TABLE IF NOT EXISTS ledger (
                    id INTEGER PRIMARY KEY, owner TEXT NOT NULL REFERENCES accounts(id),
                    job TEXT REFERENCES jobs(id), kind TEXT NOT NULL, amount INTEGER NOT NULL,
                    UNIQUE(job, kind));
                CREATE TABLE IF NOT EXISTS catalogs (
                    digest TEXT PRIMARY KEY, content TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS worker_claims (
                    job TEXT PRIMARY KEY REFERENCES jobs(id), worker TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS targets (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES accounts(id),
                    request_key TEXT NOT NULL, profile TEXT NOT NULL, UNIQUE(owner,request_key));
                CREATE TABLE IF NOT EXISTS job_context (
                    job TEXT PRIMARY KEY REFERENCES jobs(id), content TEXT NOT NULL);
                -- Display summaries (app "show everything" contract,
                -- 2026-11-23): a submit-time snapshot of the fields
                -- the confirmation screen and history present back to
                -- the owner — the target display names resolved at
                -- submission, the intention and the self-love
                -- affirmation. This table deliberately lives OUTSIDE
                -- the content lifecycle: expire_scans and
                -- _terminal_cleanup minimize the payload and private
                -- context, but the summary is display metadata in the
                -- same persistence class as cost and state, so the
                -- history and a reopened scan stay fully labelled
                -- after the content itself is gone. The fields are
                -- the owner's own words, read back only through the
                -- owner-scoped app surface — never through the public
                -- serializer (serializers.public_job_view).
                CREATE TABLE IF NOT EXISTS job_display_summaries (
                    job TEXT PRIMARY KEY REFERENCES jobs(id),
                    targets TEXT NOT NULL,
                    intention TEXT,
                    self_love_joy TEXT,
                    created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS execution_progress (
                    job TEXT PRIMARY KEY REFERENCES jobs(id),
                    completed INTEGER NOT NULL DEFAULT 0,
                    stop_requested INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS purchase_receipts (
                    payment_id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES accounts(id),
                    yen INTEGER NOT NULL, credits INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS content_lifecycle (
                    job TEXT PRIMARY KEY REFERENCES jobs(id), finished_at REAL NOT NULL,
                    removed INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS completion_receipts (
                    job TEXT PRIMARY KEY REFERENCES jobs(id), state TEXT NOT NULL, digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS legacy_identity_imports (
                    issuer TEXT NOT NULL, subject TEXT NOT NULL, account_id TEXT NOT NULL,
                    opening_balance INTEGER NOT NULL, PRIMARY KEY(issuer,subject), UNIQUE(account_id));
                CREATE TABLE IF NOT EXISTS execution_resolutions (
                    job TEXT PRIMARY KEY REFERENCES jobs(id), completed INTEGER NOT NULL,
                    evidence_digest TEXT NOT NULL, resolved_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS execution_liveness (
                    job TEXT PRIMARY KEY REFERENCES jobs(id), seen_at REAL NOT NULL,
                    expired INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS account_display (
                    owner TEXT PRIMARY KEY REFERENCES accounts(id),
                    display_name TEXT NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS account_links (
                    si_owner TEXT PRIMARY KEY REFERENCES accounts(id),
                    dplanet_player_id TEXT NOT NULL,
                    link_id TEXT NOT NULL,
                    linked_at REAL NOT NULL,
                    unlinked_at REAL,
                    status TEXT NOT NULL CHECK(status IN ('active','unlinked')));
                CREATE TABLE IF NOT EXISTS legacy_replacement_receipts (
                    owner TEXT PRIMARY KEY REFERENCES accounts(id),
                    replaced_amount INTEGER NOT NULL,
                    gift_amount INTEGER NOT NULL,
                    created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS job_credit_routes (
                    job TEXT PRIMARY KEY REFERENCES jobs(id),
                    player_id TEXT NOT NULL, link_id TEXT NOT NULL);
                -- job carries no REFERENCES on purpose: a compensation op
                -- for a reservation whose local job failed to be created
                -- must still be recordable for reconciliation (§4.3).
                CREATE TABLE IF NOT EXISTS dplanet_pending_ops (
                    op_key TEXT PRIMARY KEY, job TEXT NOT NULL,
                    player_id TEXT NOT NULL, phase TEXT NOT NULL,
                    amount INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS job_devices (
                    job TEXT PRIMARY KEY REFERENCES jobs(id),
                    diode_serial_hash TEXT NOT NULL);
                -- Durable D-Planet reservation intents (Astra
                -- preventive review 2026-11-22, batch 2b item 1): the
                -- remote operation id exists only in memory between
                -- reserve and the local commit, so an ambiguous
                -- response (timeout / process death) used to leave no
                -- recovery record and a retry reserved again under a
                -- fresh id. The intent is written BEFORE the remote
                -- call with the owner, request key, payload
                -- fingerprint, operation id and amount; a retry with
                -- the same request key adopts the recorded operation
                -- id instead of minting a new one, and non-terminal
                -- rows are the reconciliation surface for uncertain
                -- outcomes. op_id carries no REFERENCES on purpose:
                -- the local job/reading may never come into being.
                CREATE TABLE IF NOT EXISTS dplanet_reservation_intents (
                    op_id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL REFERENCES accounts(id),
                    request_key TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('job','reading')),
                    fingerprint TEXT NOT NULL,
                    player_id TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK(state IN
                        ('intent','reserved','consumed','released','rejected','uncertain','release_pending')),
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(owner, request_key, kind));
                -- One balance transfer per link (design §4.4): the
                -- record is the idempotency anchor for the link-time
                -- move into the D-Planet wallet; 'pending' rows are
                -- delivered through dplanet_pending_ops (job column
                -- carries the link_id, phase 'transfer').
                CREATE TABLE IF NOT EXISTS link_transfers (
                    link_id TEXT PRIMARY KEY,
                    si_owner TEXT NOT NULL REFERENCES accounts(id),
                    player_id TEXT NOT NULL,
                    amount INTEGER NOT NULL CHECK(amount >= 0),
                    state TEXT NOT NULL CHECK(state IN ('pending','done')),
                    created_at REAL NOT NULL, updated_at REAL NOT NULL);
                -- Link-time transfers deferred while credit routing
                -- was off (batch 2b item 3): an EXPLICIT marker written
                -- by confirm_link at deferral time. The activation
                -- sweep (run_deferred_link_transfers) acts only on
                -- marked links — the absence of a transfer record
                -- alone never authorizes moving a balance, so links
                -- recorded by any other path keep their SI balance.
                CREATE TABLE IF NOT EXISTS link_transfer_deferrals (
                    link_id TEXT PRIMARY KEY,
                    si_owner TEXT NOT NULL REFERENCES accounts(id),
                    created_at REAL NOT NULL);
                -- Delegated jobs (prayer-npc-delegation-spec-20261120):
                -- every job created through the internal jobs API is
                -- attributed to the D-Planet player it was submitted
                -- for; irradiation rows also store the consent record
                -- the charge was accepted under (audit).
                CREATE TABLE IF NOT EXISTS delegated_jobs (
                    job TEXT PRIMARY KEY REFERENCES jobs(id),
                    player_id TEXT NOT NULL,
                    si_owner TEXT NOT NULL REFERENCES accounts(id),
                    link_id TEXT NOT NULL,
                    operation TEXT NOT NULL CHECK(operation IN ('scan','irradiation')),
                    consent TEXT,
                    created_at REAL NOT NULL);
                -- Standalone app jobs (si-standalone-app-design-20261122
                -- §5.3): every job created through the user-session app
                -- API is attributed to the account it was submitted
                -- for; irradiation rows also store the consent record
                -- the server generated when the confirmation screen
                -- was passed (§5.4). The app surface only ever reads
                -- or acts on jobs attributed here.
                CREATE TABLE IF NOT EXISTS app_jobs (
                    job TEXT PRIMARY KEY REFERENCES jobs(id),
                    si_owner TEXT NOT NULL REFERENCES accounts(id),
                    operation TEXT NOT NULL CHECK(operation IN ('scan','irradiation')),
                    consent TEXT,
                    created_at REAL NOT NULL);
                -- Completion notifications for app jobs (foundation
                -- work item 2, 2026-11-23): the user's per-job
                -- wait/notify choice and the mail lifecycle. Lives
                -- OUTSIDE the content lifecycle on purpose: expiry of
                -- scan content must not erase the fact that a result
                -- is waiting, and the stored address is erased when
                -- the record reaches a terminal state instead.
                CREATE TABLE IF NOT EXISTS job_notifications (
                    job TEXT PRIMARY KEY REFERENCES jobs(id),
                    owner TEXT NOT NULL REFERENCES accounts(id),
                    operation TEXT NOT NULL CHECK(operation IN ('scan','irradiation')),
                    mode TEXT NOT NULL CHECK(mode IN ('wait','notify')),
                    email TEXT,
                    status TEXT NOT NULL CHECK(status IN
                        ('armed','queued','sending','sent','suppressed','expired')),
                    created_at REAL NOT NULL,
                    finished_at REAL, outcome TEXT,
                    queued_at REAL, sent_at REAL, viewed_at REAL,
                    claimed_at REAL, claim_id TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL, last_error TEXT,
                    resume_token TEXT, resume_token_hash TEXT,
                    resume_expires_at REAL,
                    mail_subject TEXT, mail_body TEXT, mail_sender TEXT);
                -- App submission intent for scans (foundation item
                -- 2, Astra round-3 F2): written BEFORE the job is
                -- submitted, so a crash between submission,
                -- attribution and notification recording can be
                -- recovered with the ORIGINAL choice instead of an
                -- invented one. Consumed when attribution lands;
                -- stale rows are purged by the maintenance sweep.
                CREATE TABLE IF NOT EXISTS app_submission_intents (
                    owner TEXT NOT NULL REFERENCES accounts(id),
                    request_key TEXT NOT NULL,
                    intent_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    mode TEXT NOT NULL, email TEXT,
                    reference TEXT,
                    consent TEXT,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(owner, request_key));
                -- Durable "the owner saw the terminal result"
                -- marker (Astra round-3 F5): independent of the
                -- notification record, so a view that happens
                -- before the record exists is not lost.
                CREATE TABLE IF NOT EXISTS job_result_views (
                    job TEXT PRIMARY KEY REFERENCES jobs(id),
                    owner TEXT NOT NULL REFERENCES accounts(id),
                    viewed_at REAL NOT NULL);
                -- One-shot irradiation confirmations for the app
                -- (§5.4): the S5 confirmation screen's server-side
                -- record. An irradiate call is accepted only against
                -- an unused, unexpired confirmation owned by the
                -- caller; consumption and job attribution happen in
                -- the caller's transaction flow.
                CREATE TABLE IF NOT EXISTS app_irradiation_confirmations (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL REFERENCES accounts(id),
                    scan_id TEXT NOT NULL,
                    count INTEGER NOT NULL,
                    credits_per_shot INTEGER NOT NULL,
                    total_credits INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    used_job TEXT);
                -- Native-app AI readings (native-reading-design-
                -- 20261122 §3.4): a reading is NOT a job — it never
                -- enters the worker queue or the device path. The
                -- charge follows the same ledger discipline with
                -- job NULL: reading_reserve / reading_settle /
                -- reading_release (SQLite's UNIQUE(job, kind) never
                -- collides on NULL job rows).
                CREATE TABLE IF NOT EXISTS app_readings (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL REFERENCES accounts(id),
                    scan_id TEXT NOT NULL,
                    request_key TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('reserved','completed','failed')),
                    credits INTEGER NOT NULL,
                    price_version TEXT NOT NULL,
                    route TEXT NOT NULL DEFAULT 'native-reading',
                    model TEXT,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    provider_cost_microusd INTEGER,
                    text TEXT,
                    error_code TEXT,
                    day TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    finished_at REAL,
                    UNIQUE(owner, request_key));
                -- Durable alias bindings (Astra round-5 N11): a
                -- request key answered with an earlier unresolved
                -- reading for the same scan (the one-unresolved-
                -- purchase invariant) is bound to that reading, so
                -- the key's replay returns the same outcome
                -- forever instead of buying again once the first
                -- reading settles.
                CREATE TABLE IF NOT EXISTS app_reading_request_aliases (
                    owner TEXT NOT NULL REFERENCES accounts(id),
                    request_key TEXT NOT NULL,
                    confirmation_id TEXT NOT NULL,
                    reading_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(owner, request_key));
                -- Durable per-scan claims (Astra round-6 F3): a
                -- claim is acquired in the creation transaction
                -- and retained until the reading's financial
                -- operation is fully done — for routed readings,
                -- until the settle/release op has been DELIVERED
                -- (the delivery path releases the claim). While a
                -- claim stands, the scan has exactly one
                -- unresolved purchase, however the attempts race.
                CREATE TABLE IF NOT EXISTS app_reading_scan_claims (
                    owner TEXT NOT NULL REFERENCES accounts(id),
                    scan_id TEXT NOT NULL,
                    reading_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(owner, scan_id));
                -- One-shot reading confirmations: the reading twin
                -- of app_irradiation_confirmations (the S4c screen's
                -- server-side record of the shown price).
                CREATE TABLE IF NOT EXISTS app_reading_confirmations (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL REFERENCES accounts(id),
                    scan_id TEXT NOT NULL,
                    credits INTEGER NOT NULL,
                    price_version TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    used_reading TEXT);
                -- The consent record of the confirm tap, same shape
                -- of record as an irradiation consent (§3.4).
                CREATE TABLE IF NOT EXISTS app_reading_consents (
                    reading_id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL REFERENCES accounts(id),
                    confirmation_id TEXT NOT NULL,
                    credits INTEGER NOT NULL,
                    record TEXT NOT NULL,
                    created_at REAL NOT NULL);
                -- The reading twin of job_credit_routes: present
                -- only when the charge was routed to D-Planet at
                -- reserve time; settlement follows this row, not
                -- the owner's current link state. The reading id is
                -- the opaque reservation key on the D-Planet side
                -- (its si-credits ops are keyed by that string).
                CREATE TABLE IF NOT EXISTS app_reading_routes (
                    reading_id TEXT PRIMARY KEY REFERENCES app_readings(id),
                    player_id TEXT NOT NULL,
                    link_id TEXT NOT NULL);
                -- Scan exposure accounting (OSS plan v4 §9 R3-2): one
                -- reservation per accepted scan job, reserved in the
                -- submit transaction, consumed when the scan completes
                -- (its results are delivered), returned when the scan
                -- ends without delivered results.
                CREATE TABLE IF NOT EXISTS scan_reservations (
                    job TEXT PRIMARY KEY REFERENCES jobs(id),
                    owner TEXT NOT NULL REFERENCES accounts(id),
                    units INTEGER NOT NULL, day TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('reserved','consumed','returned')),
                    created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS exposure_budget (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    consumed_units INTEGER NOT NULL DEFAULT 0);
                -- Held grants (plan v4 §7): a grant blocked by the
                -- per-inviter cap or the total budget is recorded
                -- here instead of being applied or dropped.
                CREATE TABLE IF NOT EXISTS grant_holds (
                    id INTEGER PRIMARY KEY, owner TEXT NOT NULL,
                    kind TEXT NOT NULL, amount INTEGER NOT NULL,
                    inviter TEXT, reason TEXT NOT NULL,
                    created_at REAL NOT NULL);
                -- Replaced invitation codes (plan v4 §10 U7):
                -- history only; validation never consults this table.
                CREATE TABLE IF NOT EXISTS invitation_code_history (
                    id INTEGER PRIMARY KEY, owner TEXT NOT NULL,
                    code TEXT NOT NULL, replaced_at REAL NOT NULL);
                -- Referral system (referral-system-design-20261122):
                -- one redemption per invitee, ever. Registration-time
                -- referrals are recorded here too (source
                -- 'registration') alongside settings-time redemptions.
                CREATE TABLE IF NOT EXISTS referral_redemptions (
                    id TEXT PRIMARY KEY,
                    invitee_owner TEXT NOT NULL REFERENCES accounts(id),
                    inviter_kind TEXT NOT NULL CHECK(inviter_kind IN ('si','dplanet')),
                    inviter_si_owner TEXT, inviter_player_id TEXT,
                    code TEXT, source TEXT NOT NULL,
                    amount INTEGER NOT NULL, created_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'completed',
                    UNIQUE(invitee_owner));
                -- Failed referral-code resolutions (fix round
                -- F-SI-5): the durable per-account attempt limiter
                -- counts these over a rolling hour.
                CREATE TABLE IF NOT EXISTS referral_attempts (
                    id INTEGER PRIMARY KEY,
                    account TEXT NOT NULL REFERENCES accounts(id),
                    attempted_at REAL NOT NULL);
                -- Gift notifications: the celebration popup's rows.
                -- The grant itself is instant; seen_at only tracks
                -- that the notification was shown.
                CREATE TABLE IF NOT EXISTS referral_gifts (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL REFERENCES accounts(id),
                    amount INTEGER NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('inviter','invitee')),
                    redemption_id TEXT NOT NULL,
                    created_at REAL NOT NULL, seen_at REAL);
                -- D-Planet -> SI referral grants received through
                -- the internal inbox; op_key is the idempotency key.
                CREATE TABLE IF NOT EXISTS referral_grant_inbox (
                    op_key TEXT PRIMARY KEY,
                    si_owner TEXT NOT NULL REFERENCES accounts(id),
                    amount INTEGER NOT NULL, created_at REAL NOT NULL,
                    state TEXT NOT NULL DEFAULT 'applied');
                -- Per-side completion tracking for held redemptions
                -- (round 3, R3-SI-3): a resume that applies one side
                -- and is held on the other records the applied side
                -- here, so a later resume never grants it twice.
                CREATE TABLE IF NOT EXISTS referral_redemption_sides (
                    redemption_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('inviter','invitee')),
                    state TEXT NOT NULL CHECK(state IN ('applied','queued')),
                    amount INTEGER NOT NULL,
                    PRIMARY KEY(redemption_id, role));
            """)
            db.execute("INSERT OR IGNORE INTO exposure_budget VALUES (1, 0)")
            # First column migration in this codebase (existing
            # databases): worker_claims gains the claim binding
            # columns of plan v4 §8. Idempotent by construction.
            claim_columns = {row[1] for row in
                             db.execute("PRAGMA table_info(worker_claims)").fetchall()}
            if "generation" not in claim_columns:
                db.execute("ALTER TABLE worker_claims ADD COLUMN generation INTEGER NOT NULL DEFAULT 0")
            if "expires_at" not in claim_columns:
                db.execute("ALTER TABLE worker_claims ADD COLUMN expires_at REAL")
            if "purpose" not in claim_columns:
                db.execute("ALTER TABLE worker_claims ADD COLUMN purpose TEXT NOT NULL DEFAULT 'execute'")
            # Referral redemption window (design D2): accounts gain a
            # creation stamp. Existing rows keep NULL, which the
            # referral window treats as a pre-launch account (eligible
            # only while the launch grace is active).
            account_columns = {row[1] for row in
                               db.execute("PRAGMA table_info(accounts)").fetchall()}
            if "created_at" not in account_columns:
                db.execute("ALTER TABLE accounts ADD COLUMN created_at REAL")
            # Fix-round columns for databases created before them:
            # referral_redemptions.status ('completed'/'held',
            # F-SI-4) and referral_grant_inbox.state
            # ('applied'/'held', F-SI-4).
            redemption_columns = {row[1] for row in
                                  db.execute("PRAGMA table_info(referral_redemptions)").fetchall()}
            if redemption_columns and "status" not in redemption_columns:
                db.execute("ALTER TABLE referral_redemptions ADD COLUMN "
                           "status TEXT NOT NULL DEFAULT 'completed'")
            inbox_columns = {row[1] for row in
                             db.execute("PRAGMA table_info(referral_grant_inbox)").fetchall()}
            if inbox_columns and "state" not in inbox_columns:
                db.execute("ALTER TABLE referral_grant_inbox ADD COLUMN "
                           "state TEXT NOT NULL DEFAULT 'applied'")
            # Foundation item 2 fix rounds: app_submission_intents
            # gains intent_id / reference (round 4) and the consent
            # snapshot (round 5) — databases created by earlier
            # rounds have the table without them (Astra round-4
            # F12). Legacy rows keep NULL intent_id until their next
            # record call upgrades them; a NULL id can never match a
            # job stamp, so no provenance is manufactured.
            intent_columns = {row[1] for row in
                              db.execute("PRAGMA table_info(app_submission_intents)").fetchall()}
            if intent_columns:
                if "intent_id" not in intent_columns:
                    db.execute("ALTER TABLE app_submission_intents "
                               "ADD COLUMN intent_id TEXT")
                if "reference" not in intent_columns:
                    db.execute("ALTER TABLE app_submission_intents "
                               "ADD COLUMN reference TEXT")
                if "consent" not in intent_columns:
                    db.execute("ALTER TABLE app_submission_intents "
                               "ADD COLUMN consent TEXT")
            # The 1:1 rule binds active links only (design §3.1/§6.4): a plain
            # UNIQUE on the player column would also freeze unlinked rows and
            # permanently block a legitimate re-link under a new SI account.
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS account_links_active_player
                          ON account_links(dplanet_player_id) WHERE status='active'""")
            # Scan-claim backfill (Astra round-7 N18): databases
            # created before the claim table hold readings whose
            # financial operations may still be outstanding.
            # Claim every 'reserved' reading and every reading
            # with an undelivered settle/release op, earliest
            # first so a historical conflict resolves to the
            # oldest purchase. Idempotent (INSERT OR IGNORE).
            db.execute(
                "INSERT OR IGNORE INTO app_reading_scan_claims "
                "SELECT owner, scan_id, id, created_at FROM app_readings "
                "WHERE state='reserved' OR EXISTS "
                "(SELECT 1 FROM dplanet_pending_ops o WHERE o.job="
                "app_readings.id AND o.phase IN ('settle','release')) "
                "ORDER BY created_at")

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA secure_delete=ON")
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def create_test_account(self, initial_credits=100):
        if type(initial_credits) is not int or initial_credits < 0:
            raise ValueError("Invalid test-credit amount")
        owner = str(uuid.uuid4())
        with self.transaction() as db:
            db.execute("INSERT INTO accounts(id,balance,created_at) VALUES (?,?,?)", (owner, initial_credits, time.time()))
            db.execute("INSERT INTO ledger(owner,kind,amount) VALUES (?,?,?)",
                       (owner, "test_grant", initial_credits))
        return owner

    def balance(self, owner):
        with self.transaction() as db:
            row = db.execute("SELECT balance FROM accounts WHERE id=?", (owner,)).fetchone()
            if row is None:
                raise KeyError("Account not found")
            return row["balance"]

    def record_verified_purchase(self, owner, payment_id, yen, *, accepted_credits=None):
        """Internal settlement boundary, never exposed as an MCP/user tool.

        Future provider adapter must verify paid status, currency, amount and
        server-owned account binding before calling. No provider is connected.

        Settlement honors the ACCEPTED offer (batch 2b item 4): when
        the caller identifies the terms the buyer accepted
        (accepted_credits, persisted with the order at checkout
        time), those terms are granted even if the pack policy has
        since repriced or removed the denomination. A duplicate
        payment_id returns the STORED receipt before any current
        price is consulted, so a replay can neither fail nor regrant
        because the policy moved after payment.

        A NEW settlement REQUIRES the accepted terms (Astra
        round-2 F5): without them there is nothing in the record
        that establishes what the buyer accepted, and pricing the
        grant from the current pack policy — however soon after
        payment — can silently substitute a different amount, which
        the stored receipt would then preserve. Callers that cannot
        name the accepted terms must hold the payment for
        reconciliation (the Stripe path does exactly that; see
        stripe_billing.settlement_holds). The result reports
        terms_source ('accepted' / 'stored_receipt').
        """
        if not isinstance(payment_id, str) or not 1 <= len(payment_id) <= 200:
            raise ValueError("Verified payment identity required")
        if accepted_credits is not None and (type(accepted_credits) is not int
                                             or accepted_credits < 1):
            raise ValueError("Invalid accepted purchase terms")
        with self.transaction() as db:
            previous = db.execute("SELECT owner,yen,credits FROM purchase_receipts WHERE payment_id=?", (payment_id,)).fetchone()
            if previous:
                if (previous["owner"], previous["yen"]) != (owner, yen):
                    raise ValueError("Payment already assigned to different purchase")
                if accepted_credits is not None \
                        and accepted_credits != previous["credits"]:
                    raise ValueError("Payment already assigned to different purchase")
                return {"yen": previous["yen"], "credits": previous["credits"],
                        "currency": "JPY", "terms_source": "stored_receipt"}
            if accepted_credits is None:
                raise ValueError(
                    "Accepted purchase terms required for a new settlement")
            if type(yen) is not int or yen < 1:
                raise ValueError("Verified payment amount required")
            credits = accepted_credits
            terms_source = "accepted"
            updated = db.execute("UPDATE accounts SET balance=balance+? WHERE id=?", (credits, owner))
            if updated.rowcount != 1:
                raise KeyError("Account not found")
            db.execute("INSERT INTO purchase_receipts VALUES (?,?,?,?)", (payment_id, owner, yen, credits))
            db.execute("INSERT INTO ledger(owner,kind,amount) VALUES (?,?,?)", (owner, "purchase", credits))
            return {"yen": yen, "credits": credits, "currency": "JPY",
                    "terms_source": terms_source}

    # Catalog persistence lives in catalog_store.CatalogStore and the
    # credit-grant family in grants.GrantStore (Phase A split); both
    # are Store mixins, so the public API is unchanged.

    def get_by_request(self, owner, request_key):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE owner=? AND request_key=?",
                             (owner, request_key)).fetchone()
            return self._job(row) if row else None

    def register_identity(self, issuer, subject):
        """Internal only: call after signature/issuer/audience/scope verification.

        New accounts start at zero; repeats never reset their balance.
        Email is not used as an identity key and no bearer token is persisted.
        """
        if not issuer or not subject:
            raise ValueError("Verified issuer and subject required")
        with self.transaction() as db:
            row = db.execute("SELECT owner FROM identities WHERE issuer=? AND subject=?",
                             (issuer, subject)).fetchone()
            if row:
                return row["owner"]
            owner = str(uuid.uuid4())
            db.execute("INSERT INTO accounts(id,balance,created_at) VALUES (?,?,?)", (owner, SIGNUP_CAMPAIGN_CREDITS, time.time()))
            db.execute("INSERT INTO identities VALUES (?,?,?)", (issuer, subject, owner))
            return owner

    @staticmethod
    def _admission_identity(issuer, subject=None, digest=None):
        from urllib.parse import urlsplit
        parsed = urlsplit(issuer)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError('Fixed HTTPS upstream issuer required')
        if subject is not None and (not isinstance(subject, str) or str(uuid.UUID(subject)) != subject):
            raise ValueError('Canonical verified upstream UUID required')
        if digest is not None and (not isinstance(digest, str) or len(digest) != 64
                                  or any(c not in '0123456789abcdef' for c in digest)):
            raise ValueError('Verified evidence SHA256 required')

    def seal_identity_migration(self, issuer, manifest_digest):
        """Trusted operator only, after complete frozen-snapshot reconciliation.

        This records the operator's verified evidence; it cannot itself establish
        that a snapshot is complete. Never expose through MCP or a user endpoint.
        """
        if manifest_digest is None:
            raise ValueError('Verified migration evidence required')
        self._admission_identity(issuer, digest=manifest_digest)
        with self.transaction() as db:
            prior = db.execute('SELECT manifest_digest FROM identity_migrations WHERE issuer=?', (issuer,)).fetchone()
            if prior is not None and prior['manifest_digest'] != manifest_digest:
                raise ValueError('Migration evidence conflict')
            db.execute('INSERT OR IGNORE INTO identity_migrations VALUES (?,?)', (issuer, manifest_digest))

    def admit_upstream_identity(self, issuer, subject, disposition, evidence_digest):
        """Trusted onboarding only: missing SI identity is never evidence of newness."""
        if evidence_digest is None or subject is None:
            raise ValueError('Verified identity and admission evidence required')
        self._admission_identity(issuer, subject, evidence_digest)
        if disposition not in ('approved_new', 'quarantined'):
            raise ValueError('Explicit admission disposition required')
        with self.transaction() as db:
            prior = db.execute('SELECT disposition,evidence_digest FROM identity_admissions WHERE issuer=? AND subject=?', (issuer, subject)).fetchone()
            if prior is not None:
                if tuple(prior) != (disposition, evidence_digest):
                    raise ValueError('Admission evidence conflict')
                return
            if db.execute('SELECT 1 FROM identities WHERE issuer=? AND subject=?', (issuer, subject)).fetchone():
                raise ValueError('Existing identity requires reconciliation, not new admission')
            db.execute('INSERT OR IGNORE INTO identity_admissions VALUES (?,?,?,?)',
                       (issuer, subject, disposition, evidence_digest))

    def provision_admitted_identity(self, issuer, subject, referral_code=None):
        """Internal trusted onboarding. Broker token resolution remains bind-only.

        Readiness and explicit upstream-new evidence must both exist. Serialize
        optional referral awards with account creation; repeats cannot regrant.
        Referral codes cannot be added after registration.
        """
        if subject is None:
            raise ValueError('Verified identity required')
        self._admission_identity(issuer, subject)
        # Cross-system registration codes (fix round F-SI-2): resolve
        # against D-Planet BEFORE the provisioning transaction when
        # the code does not resolve locally.
        cross_player = None
        if referral_code is not None:
            with self.transaction() as db:
                local_owner = self._code_owner(db, referral_code)
            if local_owner is None:
                cross_player = self._resolve_cross_player(referral_code)
        redemption_id = None
        with self.transaction() as db:
            if not db.execute('SELECT 1 FROM identity_migrations WHERE issuer=?', (issuer,)).fetchone():
                raise ValueError('Identity migration not reconciled')
            admission = db.execute('SELECT disposition FROM identity_admissions WHERE issuer=? AND subject=?', (issuer, subject)).fetchone()
            if admission is not None and admission['disposition'] == 'quarantined':
                raise ValueError('Identity requires reconciliation')
            existing = db.execute('SELECT owner FROM identities WHERE issuer=? AND subject=?', (issuer, subject)).fetchone()
            if existing is not None:
                if referral_code is not None:
                    prior = db.execute('SELECT code FROM registration_referrals WHERE invitee=?', (existing['owner'],)).fetchone()
                    # registration_referrals stores the CANONICAL code
                    # (round 3, R3-SI-8a): a replay with the raw
                    # variant input matches through resolution, not
                    # by raw string equality.
                    canonical = self._canonical_code(db, referral_code)
                    matches = prior is not None and prior['code'] is not None \
                        and prior['code'] == (canonical or referral_code)
                    if not matches and prior is not None and prior['code'] is None:
                        # Cross registration (F-SI-2): the code lives on
                        # the dplanet redemption row instead.
                        cross = db.execute(
                            "SELECT code FROM referral_redemptions WHERE invitee_owner=? "
                            "AND inviter_kind='dplanet' AND source='registration'",
                            (existing['owner'],)).fetchone()
                        matches = cross is not None and cross['code'] == referral_code
                    if not matches:
                        raise ValueError('Referral must match original registration')
                return existing['owner']
            if admission is None or admission['disposition'] != 'approved_new':
                raise ValueError('Verified new-user admission required')
            owner = str(uuid.uuid4())
            db.execute('INSERT INTO accounts(id,balance,created_at) VALUES (?,?,?)', (owner, SIGNUP_CAMPAIGN_CREDITS, time.time()))
            db.execute('INSERT INTO identities VALUES (?,?,?)', (issuer, subject, owner))
            redemption_id = self._registration_referral(
                db, owner, referral_code, cross_player_id=cross_player)
            provisioned = owner
        # Best-effort post-commit drain of any queued inviter grant
        # op (F-SI-2); failures stay queued for reconciliation.
        if redemption_id is not None:
            try:
                self._deliver_dplanet_ops(redemption_id)
            except Exception:
                pass
        return provisioned

    def bind_verified_identity(self, issuer, subject, upstream_issuer, upstream_subject):
        """Internal only: bind an authenticated SI identity to a verified existing identity.

        Caller must verify the signed broker assertion and fixed upstream issuer.
        Never call with email, user-editable attributes or arbitrary MCP input.
        Missing canonical accounts require onboarding/import, not a fallback grant.
        """
        from urllib.parse import urlsplit
        for value in (issuer, upstream_issuer):
            parsed = urlsplit(value)
            if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
                    or parsed.password or parsed.query or parsed.fragment):
                raise ValueError('Verified HTTPS identity issuer required')
        if issuer == upstream_issuer:
            raise ValueError('Separate identity authorities required')
        for value in (subject, upstream_subject):
            if not isinstance(value, str) or str(uuid.UUID(value)) != value:
                raise ValueError('Verified canonical UUID required')
        with self.transaction() as db:
            canonical = db.execute('SELECT owner FROM identities WHERE issuer=? AND subject=?',
                                   (upstream_issuer, upstream_subject)).fetchone()
            if canonical is None:
                raise ValueError('Canonical identity not provisioned')
            prior = db.execute('SELECT upstream_issuer,upstream_subject FROM identity_links WHERE issuer=? AND subject=?',
                               (issuer, subject)).fetchone()
            if prior is not None and tuple(prior) != (upstream_issuer, upstream_subject):
                raise ValueError('Conflicting verified identity link')
            alias = db.execute('SELECT owner FROM identities WHERE issuer=? AND subject=?',
                               (issuer, subject)).fetchone()
            if alias is not None:
                if prior is None or alias['owner'] != canonical['owner']:
                    raise ValueError('Existing identity requires reconciliation')
                return canonical['owner']
            db.execute('INSERT INTO identities VALUES (?,?,?)', (issuer, subject, canonical['owner']))
            db.execute('INSERT INTO identity_links VALUES (?,?,?,?)',
                       (issuer, subject, upstream_issuer, upstream_subject))
            return canonical['owner']

    def import_legacy_identity(self, issuer, subject, account_id, opening_balance):
        """Operator-only cutover import from a verified, frozen legacy snapshot.

        Never called by a login, agent or public endpoint. Preserve the existing
        account ID/balance plus one migration award. Conflicts require review.
        """
        from urllib.parse import urlsplit
        parsed = urlsplit(issuer)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Exact verified issuer required')
        for value in (subject, account_id):
            if not isinstance(value, str) or str(uuid.UUID(value)) != value:
                raise ValueError('Canonical verified identity required')
        if type(opening_balance) is not int or opening_balance < 0:
            raise ValueError('Verified nonnegative balance required')
        with self.transaction() as db:
            prior = db.execute('SELECT account_id,opening_balance FROM legacy_identity_imports WHERE issuer=? AND subject=?', (issuer, subject)).fetchone()
            if prior:
                if tuple(prior) != (account_id, opening_balance):
                    raise ValueError('Conflicting migration snapshot')
                self._migration_award(db, account_id)
                return account_id
            if (db.execute('SELECT 1 FROM identity_migrations WHERE issuer=?', (issuer,)).fetchone()
                    or db.execute('SELECT 1 FROM identity_admissions WHERE issuer=? AND subject=?', (issuer, subject)).fetchone()):
                raise ValueError('Sealed migration or admitted identity requires reconciliation')
            if (db.execute('SELECT 1 FROM identities WHERE issuer=? AND subject=?', (issuer, subject)).fetchone()
                    or db.execute('SELECT 1 FROM accounts WHERE id=?', (account_id,)).fetchone()):
                raise ValueError('Existing identity/account requires reconciliation')
            db.execute('INSERT INTO accounts(id,balance,created_at) VALUES (?,?,?)', (account_id, opening_balance, time.time()))
            db.execute('INSERT INTO identities VALUES (?,?,?)', (issuer, subject, account_id))
            db.execute('INSERT INTO legacy_identity_imports VALUES (?,?,?,?)', (issuer, subject, account_id, opening_balance))
            db.execute('INSERT INTO ledger(owner,kind,amount) VALUES (?,?,?)', (account_id, 'legacy_opening_balance', opening_balance))
            self._migration_award(db, account_id)
            return account_id

    def register_target(self, owner, request_key, profile):
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 128:
            raise ValueError("Invalid target request key")
        encoded = json.dumps(profile, sort_keys=True, allow_nan=False)
        with self.transaction() as db:
            row = db.execute("SELECT id,profile FROM targets WHERE owner=? AND request_key=?", (owner, request_key)).fetchone()
            if row:
                if row["profile"] != encoded:
                    raise ValueError("Target request key already used")
                return row["id"]
            identity = str(uuid.uuid4())
            db.execute("INSERT INTO targets VALUES (?,?,?,?)", (identity, owner, request_key, encoded))
            return identity

    def get_target(self, owner, target):
        with self.transaction() as db:
            row = db.execute("SELECT profile FROM targets WHERE id=? AND owner=?", (target, owner)).fetchone()
        if row is None:
            raise KeyError("Target not found")
        return {**json.loads(row["profile"]), "id": target}

    def list_targets(self, owner):
        """The owner's registered targets for management screens.

        Returns display metadata only — never the stored photo bytes.
        Photo presence is reported so the app can show which targets
        are scan-ready without transferring image data."""
        with self.transaction() as db:
            rows = db.execute("SELECT id,profile FROM targets WHERE owner=? ORDER BY rowid",
                              (owner,)).fetchall()
        targets = []
        for row in rows:
            profile = json.loads(row["profile"])
            targets.append({"id": row["id"], "name": profile.get("name", ""),
                            "birthday": profile.get("birthday", ""),
                            "gender": profile.get("gender", ""),
                            "has_photo": bool(profile.get("photo_url")),
                            "has_photo2": bool(profile.get("photo_url2"))})
        return targets

    def rename_target(self, owner, target, name):
        """Rename one of the owner's targets; the rest of the stored
        profile (photos included) is preserved byte-for-byte."""
        if not isinstance(name, str) or not name.strip() or len(name) > 200 \
                or _display_text(name) is None:
            raise ValueError("Target name required, maximum 200 characters")
        with self.transaction() as db:
            row = db.execute("SELECT profile FROM targets WHERE id=? AND owner=?",
                             (target, owner)).fetchone()
            if row is None:
                raise KeyError("Target not found")
            profile = json.loads(row["profile"])
            profile["name"] = name
            db.execute("UPDATE targets SET profile=? WHERE id=? AND owner=?",
                       (json.dumps(profile, sort_keys=True, allow_nan=False), target, owner))

    def delete_target(self, owner, target):
        """Delete one of the owner's targets, photos included (§5.3:
        the S2b deletion is the app's photo-removal path). Jobs that
        already pinned this target keep their private context copy,
        so deletion never breaks a queued or running job."""
        with self.transaction() as db:
            deleted = db.execute("DELETE FROM targets WHERE id=? AND owner=?",
                                 (target, owner))
            if deleted.rowcount != 1:
                raise KeyError("Target not found")

    def get_context(self, job, *, worker=None, claim_generation=None):
        with self.transaction() as db:
            if worker is not None:
                self._verify_claim(db, job, worker, claim_generation)
            row = db.execute("SELECT content FROM job_context WHERE job=?", (job,)).fetchone()
        if row is None:
            raise ValueError("Pinned target context unavailable")
        return json.loads(row["content"])

    # Display resolution is a read-back convenience, never an
    # admission path: service validation already bounds a real
    # payload's target_ids to five, so this defensive cap only ever
    # binds malformed legacy payloads.
    _DISPLAY_TARGET_LIMIT = 25

    @staticmethod
    def _profile_name(db, owner, target_id):
        """The registered display name for one of the owner's
        targets, or None. A stored profile that does not decode to
        an object (legacy corruption) is unresolvable, never an
        error — display reads must not fail on stored data."""
        row = db.execute("SELECT profile FROM targets WHERE id=? AND owner=?",
                         (target_id, owner)).fetchone()
        if row is None:
            return None
        try:
            profile = json.loads(row["profile"])
        except (ValueError, TypeError, RecursionError):
            return None
        if not isinstance(profile, dict):
            return None
        return _display_text(profile.get("name"))

    def _target_names(self, db, owner, target_ids, cache=None):
        """Display names for registered target ids, in payload order.

        Ids that do not resolve to one of the owner's registered
        targets (request-image identities, since-deleted rows,
        malformed profiles) have no name and are skipped — the
        display summary records names, never ids or photo data.
        Lookups are deduplicated through the optional cache (one
        history request shares it across rows) and the number of
        ids examined is capped (_DISPLAY_TARGET_LIMIT), so a
        malformed legacy payload cannot turn a display read into
        unbounded profile parsing."""
        names = []
        if not isinstance(target_ids, list):
            return names
        if cache is None:
            cache = {}
        for target_id in target_ids[:self._DISPLAY_TARGET_LIMIT]:
            if _display_text(target_id) is None:
                # Not a string, or a string SQLite/JSON cannot
                # carry (unpaired surrogate): unresolvable, and it
                # must never reach SQL parameter binding.
                continue
            if target_id not in cache:
                cache[target_id] = self._profile_name(db, owner, target_id)
            name = cache[target_id]
            if name is not None:
                names.append(name)
        return names

    def _record_display_summary(self, db, job, owner, payload):
        """Snapshot the display summary in the job's own transaction.

        Written once, at submission (INSERT OR IGNORE: a request-key
        replay returns the existing job and keeps the first
        snapshot, exactly like the attribution records). Target
        names are resolved NOW, so a later rename or deletion never
        rewrites what a past session presented."""
        names = self._target_names(db, owner, payload.get("target_ids"))
        db.execute("INSERT OR IGNORE INTO job_display_summaries VALUES (?,?,?,?,?)",
                   (job, json.dumps(names, allow_nan=False),
                    _display_text(payload.get("intention")),
                    _display_text(payload.get("self_love_joy")), time.time()))

    @staticmethod
    def _summary_fields(row):
        """Parse one stored summary row defensively: a row whose
        targets do not decode to a name array reads as empty names
        rather than breaking the owner's reads."""
        try:
            targets = json.loads(row["targets"])
        except (ValueError, TypeError, RecursionError):
            targets = []
        if not isinstance(targets, list):
            targets = []
        return {"targets": [name for name in
                            (_display_text(entry) for entry in targets)
                            if name is not None],
                "intention": _display_text(row["intention"]),
                "self_love_joy": _display_text(row["self_love_joy"])}

    def _fallback_fields(self, db, owner, payload, cache=None):
        """Display fields read from the job payload itself — the
        path for jobs that predate summaries. The payload may be a
        parsed dict or the raw stored JSON text; anything
        undecodable or content-removed reads as empty fields,
        never an error."""
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (ValueError, RecursionError):
                payload = None
        if not isinstance(payload, dict) or payload.get("content_removed"):
            return {"targets": [], "intention": None, "self_love_joy": None}
        return {"targets": self._target_names(db, owner,
                                              payload.get("target_ids"),
                                              cache=cache),
                "intention": _display_text(payload.get("intention")),
                "self_love_joy": _display_text(payload.get("self_love_joy"))}

    def _display_fields(self, db, owner, job, payload, cache=None):
        """The display fields for one job: the durable submit-time
        summary when present (the payload is not even decoded in
        that case); otherwise the payload fallback."""
        row = db.execute("SELECT targets,intention,self_love_joy "
                         "FROM job_display_summaries WHERE job=?",
                         (job,)).fetchone()
        if row is not None:
            return self._summary_fields(row)
        return self._fallback_fields(db, owner, payload, cache=cache)

    def job_columns(self, owner, job):
        """Column-only read of one of the owner's jobs (id, state,
        cost): never decodes the payload, so the app surface stays
        able to answer for a job whose stored payload no longer
        decodes (legacy corruption). Raises KeyError like get()
        when the job is not the owner's."""
        with self.transaction() as db:
            row = db.execute("SELECT id,state,cost FROM jobs WHERE id=? AND owner=?",
                             (job, owner)).fetchone()
            if row is None:
                raise KeyError("Job not found")
            return {"id": row["id"], "state": row["state"], "cost": row["cost"]}

    def display_summary(self, owner, job):
        """Display fields for one of the owner's jobs: targets
        (display names), intention and self_love_joy, summary-first
        with the payload fallback (see _display_fields).

        The job row is re-read under the owner inside this call —
        only the supplied job id is trusted, never a caller-supplied
        payload — so a foreign or forged job reference raises
        KeyError exactly like get()."""
        with self.transaction() as db:
            row = db.execute("SELECT payload FROM jobs WHERE id=? AND owner=?",
                             (job["id"], owner)).fetchone()
            if row is None:
                raise KeyError("Job not found")
            return self._display_fields(db, owner, job["id"], row["payload"])
    def _assert_confirmation_claim(self, db, owner, request_key,
                                   confirmation_id):
        """Fail a job creation whose confirmation claim is no longer
        held for this request key (Astra round-7 F15): a failing
        twin may have released the shared claim marker before this
        attempt's creation transaction runs, and charging against a
        released claim would strand the job — its finalization
        could never bind the confirmation."""
        row = db.execute(
            "SELECT used_job FROM app_irradiation_confirmations "
            "WHERE id=? AND owner=?", (confirmation_id, owner)).fetchone()
        if row is None or row["used_job"] != (
                self._APP_CONFIRMATION_INTENT + request_key):
            raise ValueError("confirmation claim no longer held")

    @staticmethod
    def _assert_submission_intent(db, owner, request_key, intent_id):
        """Fail a job creation whose submission intent is gone or
        no longer the one the caller resolved (Astra round-6 F2):
        a concurrent attempt's rejection may discard the intent
        while this submission is in flight, and creating the job
        anyway would strand it — stamped with an intent that no
        longer exists, invisible to reconciliation."""
        row = db.execute(
            "SELECT intent_id FROM app_submission_intents "
            "WHERE owner=? AND request_key=?",
            (owner, request_key)).fetchone()
        if row is None or row["intent_id"] != intent_id:
            raise ValueError("submission intent no longer valid")

    def submit(self, owner, request_key, payload, *, cost, private_context=None,
               delegation=None, require_intent_id=None,
               require_confirmation_id=None):
        # Cost is set by service policy, never by an MCP tool caller.
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 128:
            raise ValueError("request_key must contain 1 to 128 characters")
        if type(cost) is not int or cost < 0:
            raise ValueError("Invalid cost")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        # Delegated attribution (batch 2b item 13): the internal jobs
        # API's player attribution and consent record are written in
        # the SAME transaction as the job, never as a follow-up write
        # that can fail after the job already exists — a job the
        # worker can execute must never exist unattributed. The
        # consent is serialized BEFORE any side effect (including the
        # routed path's remote reserve), so an unserializable consent
        # aborts the submission before anything is created.
        delegation_consent = None
        if delegation is not None:
            if not isinstance(delegation, dict) \
                    or not isinstance(delegation.get("player_id"), str) \
                    or not delegation.get("player_id") \
                    or not isinstance(delegation.get("link_id"), str) \
                    or not delegation.get("link_id") \
                    or delegation.get("operation") not in ("scan", "irradiation"):
                raise ValueError("Invalid delegation metadata")
            if delegation.get("consent") is not None:
                delegation_consent = json.dumps(
                    delegation["consent"], sort_keys=True, allow_nan=False)
        if cost > 0 and self._dplanet_routing_enabled:
            with self.transaction() as db:
                link = self._routing_link(db, owner)
            if link is not None:
                return self._submit_routed(owner, request_key, payload, encoded=encoded,
                                           digest=digest, cost=cost,
                                           private_context=private_context, link=link,
                                           delegation=delegation,
                                           delegation_consent=delegation_consent,
                                           require_intent_id=require_intent_id,
                                           require_confirmation_id=require_confirmation_id)
        with self.transaction() as db:
            existing = db.execute("SELECT * FROM jobs WHERE owner=? AND request_key=?",
                                  (owner, request_key)).fetchone()
            if existing:
                if existing["digest"] != digest:
                    raise ValueError("request_key already used for different input")
                return self._job(existing)
            if require_intent_id is not None:
                self._assert_submission_intent(db, owner, request_key,
                                               require_intent_id)
            if require_confirmation_id is not None:
                self._assert_confirmation_claim(db, owner, request_key,
                                                require_confirmation_id)
            self._check_scan_available(db, owner, payload)
            updated = db.execute("UPDATE accounts SET balance=balance-? WHERE id=? AND balance>=?",
                                 (cost, owner, cost))
            if updated.rowcount != 1:
                raise ValueError("Account unavailable or insufficient credits")
            job = str(uuid.uuid4())
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,NULL)",
                       (job, owner, request_key, encoded, digest, cost, "queued"))
            self._record_display_summary(db, job, owner, payload)
            if private_context is not None:
                db.execute("INSERT INTO job_context VALUES (?,?)", (job, json.dumps(private_context, sort_keys=True, allow_nan=False)))
            if delegation is not None:
                db.execute("INSERT OR IGNORE INTO delegated_jobs VALUES (?,?,?,?,?,?,?)",
                           (job, delegation["player_id"], owner, delegation["link_id"],
                            delegation["operation"], delegation_consent, time.time()))
            db.execute("INSERT INTO ledger(owner,job,kind,amount) VALUES (?,?,?,?)",
                       (owner, job, "reserve", -cost))
            if payload.get("operation") == "scan":
                self._reserve_scan_quota(db, owner, job)
            return self._job(db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone())

    @staticmethod
    def _check_scan_available(db, owner, payload):
        if payload.get("operation") == "irradiation":
            scan = db.execute("SELECT payload FROM jobs WHERE id=? AND owner=? AND state='completed'",
                              (payload.get("scan_id"), owner)).fetchone()
            if scan is None or json.loads(scan["payload"]).get("content_removed"):
                raise ValueError("Scan content unavailable; scan again")

    # --- Scan exposure accounting (OSS plan v4 section 9, R3-2) ---
    # Two layers share one ledger: a per-account daily quota and a
    # cross-account cumulative exposure budget. The reservation is
    # written inside the submit transaction, so concurrent submits
    # cannot overrun either limit, and a request_key replay returns
    # the existing job before any second reservation is made. A
    # reservation is consumed only when the scan completes (its
    # results are delivered); every other terminal path returns it.
    # Limits are dormant (None) until the operator sets approved
    # numbers in the limit policy; the ledger runs either way.
    #
    # The two layers count DIFFERENT things, deliberately: the daily
    # quota counts scans (reservation rows), so the configured value
    # is a number of scans per day; the exposure budget counts
    # exposure units (scan_units_per_job per scan), the operator's
    # aggregate-risk measure. Counting the daily quota in units made
    # a quota of 9 reject the very first scan at 10 units per scan
    # (the 2026-11-22 outage shape; Astra CRITICAL).

    def _reserve_scan_quota(self, db, owner, job):
        policy = self.limit_policy
        units = policy.scan_units_per_job
        day = datetime.now(timezone.utc).date().isoformat()
        if policy.scan_daily_quota_per_account is not None:
            used = db.execute(
                "SELECT COUNT(*) FROM scan_reservations "
                "WHERE owner=? AND day=? AND state IN ('reserved','consumed')",
                (owner, day)).fetchone()[0]
            if used + 1 > policy.scan_daily_quota_per_account:
                raise ValueError("Scan quota exhausted for today")
        if policy.scan_exposure_budget_total is not None:
            consumed = db.execute(
                "SELECT consumed_units FROM exposure_budget WHERE id=1").fetchone()[0]
            outstanding = db.execute(
                "SELECT COALESCE(SUM(units),0) FROM scan_reservations WHERE state='reserved'"
            ).fetchone()[0]
            if consumed + outstanding + units > policy.scan_exposure_budget_total:
                raise ValueError("Scan exposure budget exhausted")
        db.execute("INSERT INTO scan_reservations VALUES (?,?,?,?,?,?)",
                   (job, owner, units, day, "reserved", time.time()))

    def _settle_scan_reservation(self, db, job, *, consumed):
        row = db.execute("SELECT units,state FROM scan_reservations WHERE job=?",
                         (job,)).fetchone()
        if row is None or row["state"] != "reserved":
            return
        if consumed:
            db.execute("UPDATE scan_reservations SET state='consumed' WHERE job=?", (job,))
            db.execute("UPDATE exposure_budget SET consumed_units=consumed_units+? WHERE id=1",
                       (row["units"],))
        else:
            db.execute("UPDATE scan_reservations SET state='returned' WHERE job=?", (job,))

    def scan_reservation(self, job):
        # The exposure reservation recorded for one job, if any.
        with self.transaction() as db:
            row = db.execute("SELECT * FROM scan_reservations WHERE job=?", (job,)).fetchone()
            if row is None:
                return None
            return {"job": row["job"], "owner": row["owner"], "units": row["units"],
                    "day": row["day"], "state": row["state"],
                    "created_at": row["created_at"]}

    def exposure_consumed_units(self):
        # Cumulative scan exposure consumed across all accounts.
        with self.transaction() as db:
            return db.execute(
                "SELECT consumed_units FROM exposure_budget WHERE id=1").fetchone()[0]

    # --- D-Planet consumption routing (design §4.2/§4.3) ---
    # A job is routed when its owner had an active link at submit time
    # and routing was enabled; the route is recorded per job
    # (job_credit_routes) so settlement follows the job, not the owner's
    # current link state. Free jobs (scans, cost 0) never route.

    def _routing_link(self, db, owner):
        if not self._dplanet_routing_enabled:
            return None
        row = db.execute("SELECT * FROM account_links WHERE si_owner=? AND status='active'",
                         (owner,)).fetchone()
        return self._account_link(row) if row is not None else None

    @staticmethod
    def _route_player(db, job):
        row = db.execute("SELECT player_id FROM job_credit_routes WHERE job=?", (job,)).fetchone()
        return row["player_id"] if row is not None else None

    def _submit_routed(self, owner, request_key, payload, *, encoded, digest, cost,
                       private_context, link, delegation=None, delegation_consent=None,
                       require_intent_id=None, require_confirmation_id=None):
        """Reserve in D-Planet first, then create the job locally.

        The SI balance column is never touched for a routed job; the
        local ledger keeps mirror rows for audit and proof (§4.2). If
        the local insert fails after a successful reservation, the
        reservation is released as a compensating operation.

        The reservation intent is persisted BEFORE the remote call
        (batch 2b item 1): if the reserve response is ambiguous
        (D-Planet unreachable — the reservation may or may not
        exist), the intent stays as the durable recovery record in
        state 'uncertain', and a retry with the same request key
        adopts the recorded job id and re-reserves under it instead
        of minting a fresh id that could reserve twice.
        """
        client = self._dplanet_client
        if client is None:
            raise ValueError("D-Planet credit routing is enabled but not configured")
        player_id = link["dplanet_player_id"]
        with self.transaction() as db:
            existing = db.execute("SELECT * FROM jobs WHERE owner=? AND request_key=?",
                                  (owner, request_key)).fetchone()
            if existing:
                if existing["digest"] != digest:
                    raise ValueError("request_key already used for different input")
                return self._job(existing)
            if not db.execute("SELECT 1 FROM accounts WHERE id=?", (owner,)).fetchone():
                raise ValueError("Account unavailable or insufficient credits")
            if require_intent_id is not None:
                self._assert_submission_intent(db, owner, request_key,
                                               require_intent_id)
            if require_confirmation_id is not None:
                self._assert_confirmation_claim(db, owner, request_key,
                                                require_confirmation_id)
            self._check_scan_available(db, owner, payload)
        job = self._record_reservation_intent(
            str(uuid.uuid4()), owner=owner, request_key=request_key, kind="job",
            fingerprint=digest, player_id=player_id, amount=cost)
        try:
            client.reserve(player_id, job, cost)
        except DPlanetError as error:
            if error.status == 402:
                # The one proven rejection: D-Planet evaluated the
                # reserve against the wallet and refused it for lack
                # of funds — no hold exists under this operation id.
                self._set_reservation_intent_state(job, "rejected")
                raise ValueError("Account unavailable or insufficient credits") from None
            # Fail-closed (§4.3): while D-Planet cannot reserve, the job
            # is not accepted at all. Free scans never reach this path.
            # Every other failure — no response, a gateway 5xx, even a
            # malformed 200 — leaves the commit outcome UNKNOWN: the
            # hold may exist remotely under the recorded operation id
            # (Astra round-1 F1). The intent is marked 'uncertain'
            # (never deleted, never replaced): a same-key retry
            # adopts the recorded id, and reconciliation works from
            # the uncertain list.
            marked = self._set_reservation_intent_state(job, "uncertain")
            if not marked:
                # The intent record is gone (Astra round-6 F2): a
                # concurrent retry replaced it while this reserve
                # was in flight. The hold may exist remotely with
                # nothing recording it — compensate explicitly so
                # a durable release op exists.
                with self.transaction() as db:
                    exists = db.execute(
                        "SELECT 1 FROM dplanet_reservation_intents "
                        "WHERE op_id=?", (job,)).fetchone()
                if not exists:
                    self._compensate_reserve(player_id, job, cost)
            raise ValueError(f"D-Planet credit reservation failed: {error.message}") from None
        self._set_reservation_intent_state(job, "reserved")
        created = None
        existing = None
        try:
            with self.transaction() as db:
                existing = db.execute("SELECT * FROM jobs WHERE owner=? AND request_key=?",
                                      (owner, request_key)).fetchone()
                if existing is None:
                    self._assert_no_compensation_fence(
                        db, job, ValueError(
                            "Reservation is being released; retry later"))
                    if require_intent_id is not None:
                        self._assert_submission_intent(
                            db, owner, request_key, require_intent_id)
                    if require_confirmation_id is not None:
                        self._assert_confirmation_claim(
                            db, owner, request_key, require_confirmation_id)
                    self._check_scan_available(db, owner, payload)
                    db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,NULL)",
                               (job, owner, request_key, encoded, digest, cost, "queued"))
                    self._record_display_summary(db, job, owner, payload)
                    if private_context is not None:
                        db.execute("INSERT INTO job_context VALUES (?,?)",
                                   (job, json.dumps(private_context, sort_keys=True, allow_nan=False)))
                    db.execute("INSERT INTO ledger(owner,job,kind,amount) VALUES (?,?,?,?)",
                               (owner, job, "reserve", -cost))
                    db.execute("INSERT INTO job_credit_routes VALUES (?,?,?)",
                               (job, player_id, link["link_id"]))
                    if delegation is not None:
                        db.execute(
                            "INSERT OR IGNORE INTO delegated_jobs VALUES (?,?,?,?,?,?,?)",
                            (job, delegation["player_id"], owner, delegation["link_id"],
                             delegation["operation"], delegation_consent, time.time()))
                    created = self._job(db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone())
        except BaseException:
            self._compensate_reserve(player_id, job, cost)
            raise
        if created is None:
            # A concurrent submit with the same request_key won the
            # insert; this reservation has no job and is released
            # (unless the winner carries this very operation id —
            # _compensate_reserve never releases an owned id).
            self._compensate_reserve(player_id, job, cost)
            if existing["digest"] != digest:
                raise ValueError("request_key already used for different input")
            if existing["id"] == job:
                # The winner carries this attempt's adopted operation
                # id: the reservation belongs to that job now.
                self._set_reservation_intent_state(job, "consumed")
            return self._job(existing)
        self._set_reservation_intent_state(job, "consumed")
        return created

    # --- Durable D-Planet reservation intents (batch 2b item 1) ---
    # Written BEFORE the remote reserve call so the operation id,
    # amount and payload fingerprint survive an ambiguous response
    # or a process death. A retry with the same request key adopts
    # the recorded operation id (D-Planet's si-credits ops are
    # idempotent per operation id, so re-reserving under it can
    # never double-reserve); non-terminal rows are the operator's
    # reconciliation surface for outcomes that stay uncertain.

    def _record_reservation_intent(self, op_id, *, owner, request_key, kind,
                                   fingerprint, player_id, amount, conflict=None,
                                   pending=None):
        """Persist the intent for one remote reservation attempt.

        Returns the operation id to reserve under: the recorded one
        when an earlier attempt for this (owner, request_key, kind)
        left a live intent, else the freshly supplied op_id. A live
        intent whose fingerprint, player or amount differs is a
        different input under an already-used key and raises
        `conflict` (a ValueError by default): adoption must never
        re-point a recorded operation id at a different wallet or a
        different amount (Astra round-1 F1). An intent in
        'release_pending' is being compensated: it is never adopted
        and never replaced until the release lands, and a matching
        retry raises `pending` instead — adopting it would let the
        queued release refund a live job (F2). Terminal intents
        (released/rejected) are replaced: that reservation is known
        not to exist remotely, so the new attempt is genuinely new.
        """
        now = time.time()
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM dplanet_reservation_intents "
                "WHERE owner=? AND request_key=? AND kind=?",
                (owner, request_key, kind)).fetchone()
            if row is not None and row["state"] not in ("released", "rejected"):
                if row["fingerprint"] != fingerprint \
                        or row["player_id"] != player_id \
                        or row["amount"] != amount:
                    raise conflict if conflict is not None else ValueError(
                        "request_key already used for different input")
                if row["state"] == "release_pending":
                    raise pending if pending is not None else ValueError(
                        "Reservation is being released; retry later")
                return row["op_id"]
            if row is not None:
                db.execute("DELETE FROM dplanet_reservation_intents WHERE op_id=?",
                           (row["op_id"],))
            db.execute("INSERT INTO dplanet_reservation_intents VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (op_id, owner, request_key, kind, fingerprint, player_id,
                        amount, "intent", now, now))
            return op_id

    # Intent lifecycle (Astra round-3 F2): transitions are
    # CONDITIONAL and one-way. A state may only move forward along
    # these edges; in particular nothing may move OUT of
    # 'release_pending' except the delivery close to 'released',
    # and nothing may move into an active state once compensation
    # has claimed or completed — a late reserve response must never
    # reopen a compensated intent.
    _INTENT_TRANSITIONS = {
        # A 402 proves only that THIS attempt was refused; it must
        # not overwrite an 'uncertain' mark (a concurrent attempt's
        # hold may exist) — rejection is recorded only from the
        # pristine 'intent' state (Astra round-4 F2).
        "rejected": ("intent",),
        # An ambiguous outcome may also follow a concurrent
        # attempt's rejection mark (Astra round-5 F2): the hold
        # may exist, so the outcome belongs on the reconciliation
        # list, not behind a terminal 'rejected'.
        "uncertain": ("intent", "uncertain", "rejected"),
        # A successful reserve is fresh proof a hold exists under
        # this operation id, so it may also supersede a 'rejected'
        # mark left by a concurrent attempt whose 402 landed first
        # (Astra round-4 F2): without this edge, the successful
        # caller's mark returns zero, creation refuses 'rejected',
        # compensation refuses to claim it, and the hold strands.
        "reserved": ("intent", "uncertain", "rejected"),
        "consumed": ("intent", "reserved", "uncertain"),
    }

    def _set_reservation_intent_state(self, op_id, state):
        allowed = self._INTENT_TRANSITIONS[state]
        marks = ",".join("?" for _ in allowed)
        with self.transaction() as db:
            return db.execute(
                "UPDATE dplanet_reservation_intents SET state=?, updated_at=? "
                f"WHERE op_id=? AND state IN ({marks})",
                (state, time.time(), op_id, *allowed)).rowcount

    def uncertain_dplanet_reservations(self):
        """Reservation intents whose remote outcome is not durably
        settled (batch 2b item 1): the recovery record an operator
        reconciles against D-Planet when a reserve response was
        ambiguous or the process died mid-submission."""
        with self.transaction() as db:
            rows = db.execute(
                "SELECT * FROM dplanet_reservation_intents "
                "WHERE state IN ('intent','reserved','uncertain','release_pending') "
                "ORDER BY created_at").fetchall()
            return [{"op_id": row["op_id"], "owner": row["owner"],
                     "request_key": row["request_key"], "kind": row["kind"],
                     "fingerprint": row["fingerprint"],
                     "player_id": row["player_id"], "amount": row["amount"],
                     "state": row["state"], "created_at": row["created_at"],
                     "updated_at": row["updated_at"]} for row in rows]

    def _compensate_reserve(self, player_id, job, amount):
        """Release a reservation whose local job was never created.

        The compensation is FENCED (Astra round-1 F2, round-2 F2/N1):
        one transaction checks ownership, claims the intent into
        'release_pending', and writes the release op into the
        pending-ops outbox — all before any network I/O. While the
        fence is held, the intent can be neither adopted nor
        replaced, and every creation transaction re-validates the
        fence before inserting its row (see _assert_no_compensation
        _fence), so a concurrent adopter either commits first (and
        this claim sees the owner and aborts) or validates against
        the held fence (and aborts itself). Because the outbox row
        is written atomically with the claim, a process death at
        any later point still leaves a deliverable operation for
        maintenance — never a permanently pending intent with
        nothing to replay. Delivery goes through the same guarded
        path as every queued op (_release_warranted re-checks
        ownership at delivery time), and a landed release closes
        the intent to 'released'.
        """
        now = time.time()
        with self.transaction() as db:
            owned = db.execute("SELECT 1 FROM jobs WHERE id=?", (job,)).fetchone() \
                or db.execute("SELECT 1 FROM app_readings WHERE id=?", (job,)).fetchone()
            if owned:
                return
            claimed = db.execute(
                "UPDATE dplanet_reservation_intents SET state='release_pending', "
                "updated_at=? WHERE op_id=? AND state IN ('intent','reserved','uncertain','rejected')",
                (now, job)).rowcount
            if not claimed:
                row = db.execute(
                    "SELECT state FROM dplanet_reservation_intents WHERE op_id=?",
                    (job,)).fetchone()
                if row is not None:
                    # Already fenced by another compensator or
                    # already released: that compensation owns it.
                    return
                # No intent row exists for this id (Astra round-5
                # F2): a concurrent retry REPLACED the terminal
                # intent (deleting this attempt's record) while
                # this attempt's reserve was in flight, so there is
                # nothing to claim — but this caller's reserve DID
                # land (or may have), and its hold must not strand.
                # The durable release op is written anyway: the
                # pending-ops outbox is itself the reconciliation
                # record, and delivery re-validates ownership
                # before releasing anything.
                pass
            db.execute("INSERT OR IGNORE INTO dplanet_pending_ops VALUES (?,?,?,?,?,0,NULL,?,?)",
                       (f"si:{job}:release", job, player_id, "release", amount,
                        now, now))
        self._deliver_dplanet_ops(job)

    def _assert_no_compensation_fence(self, db, op_id, error):
        """Creation-side half of the compensation fence (Astra
        round-2 F2, round-3 F2): creation must POSITIVELY verify
        that this operation id's intent is still active — state
        'intent', 'reserved' or 'uncertain'. A missing intent, a
        claimed one ('release_pending'), and above all a COMPLETED
        compensation ('released') all refuse the insert: the
        round-3 interleaving was a submitter pausing before its
        creation transaction while compensation claimed, delivered
        and closed the intent, after which the old guard — which
        rejected only 'release_pending' — happily inserted a job
        against a refunded reservation. Called inside the creation
        transaction, so the check and the insert are atomic against
        the compensator's claim. Raises the caller-supplied
        exception instance, so each channel surfaces its own
        failure shape."""
        row = db.execute(
            "SELECT state FROM dplanet_reservation_intents WHERE op_id=?",
            (op_id,)).fetchone()
        if row is None or row["state"] not in ("intent", "reserved", "uncertain"):
            raise error

    def _release_warranted(self, db, job):
        """Whether a queued 'release' op for this id may still be
        delivered (Astra round-1 F2). A release is the refund of a
        reservation that backs NOTHING executable: it is warranted
        when no local row carries the id (a compensated reservation
        whose record was never created), when the job terminally
        failed or was cancelled (the refund _finish queued), or
        when the reading terminally failed (release_reading's
        refund). A queued/running/reserved/completed row means the
        reservation is spoken for — the op is stale and is dropped
        instead of refunding a live or settled charge."""
        row = db.execute("SELECT state FROM jobs WHERE id=?", (job,)).fetchone()
        if row is not None:
            return row["state"] in ("failed", "cancelled")
        row = db.execute("SELECT state FROM app_readings WHERE id=?",
                         (job,)).fetchone()
        if row is not None:
            return row["state"] == "failed"
        return True

    @staticmethod
    def _release_scan_claim(db, reading_id):
        """End one reading's scan claim, TRANSFERRING it when
        another reading for the same scan still has an
        outstanding financial operation (Astra round-8 N18): a
        claim must never simply vanish while a scan-mate's
        settle/release is still undelivered."""
        claim = db.execute(
            "SELECT owner, scan_id FROM app_reading_scan_claims "
            "WHERE reading_id=?", (reading_id,)).fetchone()
        if claim is None:
            return
        db.execute("DELETE FROM app_reading_scan_claims "
                   "WHERE reading_id=?", (reading_id,))
        successor = db.execute(
            "SELECT id, created_at FROM app_readings "
            "WHERE owner=? AND scan_id=? AND id<>? AND "
            "(state='reserved' OR EXISTS "
            "(SELECT 1 FROM dplanet_pending_ops o WHERE o.job="
            "app_readings.id AND o.phase IN ('settle','release'))) "
            "ORDER BY created_at LIMIT 1",
            (claim["owner"], claim["scan_id"], reading_id)).fetchone()
        if successor is not None:
            db.execute(
                "INSERT OR IGNORE INTO app_reading_scan_claims "
                "VALUES (?,?,?,?)",
                (claim["owner"], claim["scan_id"], successor["id"],
                 successor["created_at"]))

    @staticmethod
    def _queue_routed_compensation(db, route, reading_id,
                                   credits, now):
        """Fence this routed attempt's intent and queue its
        release, inside the caller's transaction (Astra round-6
        F3, round-7 F3): used when a routed creation answers with
        another reading — by alias or by the unresolved check —
        so the compensation commits atomically with the answer
        and no crash can strand the landed reserve."""
        db.execute(
            "UPDATE dplanet_reservation_intents "
            "SET state='release_pending', updated_at=? "
            "WHERE op_id=? AND state IN "
            "('intent','reserved','uncertain','rejected')",
            (now, reading_id))
        db.execute(
            "INSERT OR IGNORE INTO dplanet_pending_ops "
            "VALUES (?,?,?,?,?,0,NULL,?,?)",
            (f"si:{reading_id}:release", reading_id,
             route["player_id"], "release", credits, now, now))

    @staticmethod
    def _queue_dplanet_op(db, job, player_id, phase, amount):
        """Record a settle/release intent in the same transaction as the
        local terminal transition; delivery happens after commit and is
        retried until it lands (§4.3). The op_key is the idempotency key
        shared with D-Planet: si:<job_id>:<phase>."""
        now = time.time()
        db.execute("INSERT OR IGNORE INTO dplanet_pending_ops VALUES (?,?,?,?,?,0,NULL,?,?)",
                   (f"si:{job}:{phase}", job, player_id, phase, amount, now, now))

    def _deliver_dplanet_ops(self, job, *, client=None):
        """Best-effort delivery of queued ops for one job.

        Success deletes the queued row; failure keeps it with an attempt
        count and the remote error message. Nothing is dropped silently.
        Phase 'transfer' rows are the link-time balance moves (§4.4):
        their job column carries the link_id, and a successful delivery
        also completes the link_transfers record.
        """
        client = self._dplanet_client if client is None else client
        if client is None:
            return
        with self.transaction() as db:
            rows = db.execute("SELECT * FROM dplanet_pending_ops WHERE job=? ORDER BY created_at",
                              (job,)).fetchall()
        for row in rows:
            if row["phase"] == "release":
                # Delivery-time ownership re-check (Astra round-1
                # F2): a release queued as compensation must not land
                # if the id has since been adopted by a live job or
                # reading; a stale op is dropped, never delivered.
                with self.transaction() as db:
                    warranted = self._release_warranted(db, job)
                    if not warranted:
                        db.execute("DELETE FROM dplanet_pending_ops WHERE op_key=?",
                                   (row["op_key"],))
                if not warranted:
                    continue
            try:
                if row["phase"] == "settle":
                    client.settle(row["player_id"], job)
                elif row["phase"] == "release":
                    client.release(row["player_id"], job, row["amount"])
                elif row["phase"] == "transfer":
                    client.transfer_in(row["player_id"], job, row["amount"])
                elif row["phase"] == "grant":
                    # Referral grant (design §4.1): the op_key is
                    # sent verbatim as the D-Planet idempotency key;
                    # its role suffix selects the ledger reason.
                    client.grant(row["player_id"], op_key=row["op_key"],
                                 amount=row["amount"],
                                 ledger_reason=("referral_inviter"
                                                if row["op_key"].endswith(":inviter")
                                                else "referral_invitee"))
                else:
                    continue
            except DPlanetError as error:
                with self.transaction() as db:
                    db.execute("UPDATE dplanet_pending_ops SET attempts=attempts+1, "
                               "last_error=?, updated_at=? WHERE op_key=?",
                               (error.message, time.time(), row["op_key"]))
                continue
            with self.transaction() as db:
                db.execute("DELETE FROM dplanet_pending_ops WHERE op_key=?", (row["op_key"],))
                if row["phase"] == "transfer":
                    db.execute("UPDATE link_transfers SET state='done', updated_at=? "
                               "WHERE link_id=?", (time.time(), job))
                if row["phase"] == "release":
                    # A compensation release that finally landed
                    # closes its intent (Astra round-1 F2).
                    db.execute("UPDATE dplanet_reservation_intents SET state='released', "
                               "updated_at=? WHERE op_id=? AND state='release_pending'",
                               (time.time(), job))
                if row["phase"] in ("settle", "release"):
                    # A routed reading's financial operation is now
                    # fully delivered: its scan claim ends — or
                    # transfers to a scan-mate whose operation is
                    # still outstanding (Astra round-6 F3,
                    # round-8 N18). Job ids match no claim.
                    self._release_scan_claim(db, job)

    def pending_dplanet_ops(self):
        """Operator reconciliation list (§4.3): remote credit ops whose
        delivery has not been confirmed yet."""
        with self.transaction() as db:
            rows = db.execute("SELECT * FROM dplanet_pending_ops ORDER BY created_at").fetchall()
            return [{"op_key": row["op_key"], "job": row["job"],
                     "player_id": row["player_id"], "phase": row["phase"],
                     "amount": row["amount"], "attempts": row["attempts"],
                     "last_error": row["last_error"], "created_at": row["created_at"],
                     "updated_at": row["updated_at"]} for row in rows]

    def retry_pending_dplanet_ops(self):
        """Re-deliver every queued op; each op_key is idempotent on the
        D-Planet side, so re-sending can never double-apply (§4.3)."""
        with self.transaction() as db:
            jobs = [row["job"] for row in
                    db.execute("SELECT DISTINCT job FROM dplanet_pending_ops").fetchall()]
        before = len(self.pending_dplanet_ops())
        for job in jobs:
            self._deliver_dplanet_ops(job)
        remaining = len(self.pending_dplanet_ops())
        return {"delivered": before - remaining, "remaining": remaining}

    @staticmethod
    def _job(row):
        return {"id": row["id"], "state": row["state"], "cost": row["cost"],
                "payload": json.loads(row["payload"]),
                "result": json.loads(row["result"]) if row["result"] is not None else None}

    def get(self, owner, job):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=? AND owner=?", (job, owner)).fetchone()
            if row is None:
                raise KeyError("Job not found")
            result = self._job(row)
            progress = db.execute("SELECT completed,stop_requested FROM execution_progress WHERE job=?", (job,)).fetchone()
            if progress:
                result["execution"] = {"completed_count": progress["completed"],
                                       "stop_requested": bool(progress["stop_requested"])}
            return result

    def queue_status(self, owner, job):
        """Owner-scoped snapshot; expose counts, never another user's job details."""
        with self.transaction() as db:
            row = db.execute("SELECT rowid AS sequence,state FROM jobs WHERE id=? AND owner=?",
                             (job, owner)).fetchone()
            if row is None:
                raise KeyError("Job not found")
            waiting = row["state"] == "queued"
            ahead = db.execute("SELECT count(*) FROM jobs WHERE rowid < ? AND state IN ('queued','running','unknown')",
                               (row["sequence"],)).fetchone()[0] if waiting else None
            blocked = bool(db.execute("SELECT 1 FROM jobs WHERE state='unknown' LIMIT 1").fetchone())
            return {"state": row["state"], "jobs_ahead": ahead,
                    "position": ahead + 1 if waiting else None,
                    "blocked_by_unresolved_execution": blocked if waiting else row["state"] == "unknown",
                    "estimated_wait_seconds": None}

    @staticmethod
    def _diode_hash_valid(value):
        """The worker reports sha256(serial) truncated to 24 hex chars —
        the same derivation as its local log file names. The raw serial
        itself never leaves the worker host."""
        return isinstance(value, str) and len(value) == 24 \
            and all(c in "0123456789abcdef" for c in value)

    def _record_diode_hash(self, db, job, value):
        if self._diode_hash_valid(value):
            db.execute("INSERT OR IGNORE INTO job_devices VALUES (?,?)", (job, value))

    def _verify_claim(self, db, job, worker, claim_generation, *, allow_expired=False):
        """Verify a PRESENTED claim generation (plan v4 §8).

        A presented generation must match the live claim row: same
        worker, same generation, not expired. Callers that present no
        generation (the local worker, and remote workers not yet
        updated to send one) keep the historical worker-identity
        verification at each call site; generation enforcement then
        activates per-caller as they upgrade.

        allow_expired authenticates only the claim's identity (worker
        + generation): acknowledgement replays of an outcome that is
        already committed use it — stale receipts of jobs under an
        authoritative execution resolution (batch 2c) and outbox
        replays whose acknowledgement was lost (batch 2b item 2) —
        because lease freshness must not wedge recovery. It is never
        used to authorize new work: every NEW execution mutation
        re-verifies with the default, so a live lease is still
        required to change state.
        """
        if claim_generation is None:
            return
        if type(claim_generation) is not int:
            raise PermissionError("Claim generation mismatch")
        row = db.execute("SELECT worker,generation,expires_at FROM worker_claims WHERE job=?",
                         (job,)).fetchone()
        if row is None or row["worker"] != worker or row["generation"] != claim_generation:
            raise PermissionError("Claim generation mismatch")
        if (not allow_expired and row["expires_at"] is not None
                and row["expires_at"] <= time.time()):
            raise PermissionError("Claim expired")

    def verify_claim(self, job, worker, claim_generation=None):
        """Standalone claim check for the worker API's catalog/context
        gates, which re-verify (job, worker, state='running') in one
        transaction at access time."""
        with self.transaction() as db:
            if not db.execute("SELECT 1 FROM jobs WHERE id=? AND state='running'",
                              (job,)).fetchone():
                return False
            if not db.execute("SELECT 1 FROM worker_claims WHERE job=? AND worker=?",
                              (job, worker)).fetchone():
                return False
            try:
                self._verify_claim(db, job, worker, claim_generation)
            except PermissionError:
                return False
            return True

    def claim_next(self, *, worker=None, diode_serial_hash=None):
        # Claim is the binding moment for device evidence: a malformed
        # hash fails the claim loudly instead of letting work run
        # without its device identity attached.
        if diode_serial_hash is not None and not self._diode_hash_valid(diode_serial_hash):
            raise ValueError("Invalid diode serial hash")
        with self.transaction() as db:
            # One device lane; unknown execution holds the lane for reconciliation.
            if db.execute("SELECT 1 FROM jobs WHERE state IN ('running','unknown') LIMIT 1").fetchone():
                return None
            row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY rowid LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (row["id"],))
            db.execute('INSERT INTO execution_liveness(job,seen_at) VALUES (?,?)', (row['id'], time.time()))
            claim_expires_at = None
            if worker is not None:
                # Claim binding (plan v4 §8): every claim carries a
                # generation, an expiry (the limit policy's TTL,
                # extended by heartbeats), and a purpose. Follow-up
                # worker calls present the generation and are verified
                # against this row.
                claim_expires_at = time.time() + self.limit_policy.claim_ttl_seconds
                db.execute("INSERT INTO worker_claims(job,worker,generation,expires_at,purpose) "
                           "VALUES (?,?,?,?,?)",
                           (row["id"], worker, 1, claim_expires_at, "execute"))
            if diode_serial_hash is not None:
                db.execute("INSERT INTO job_devices VALUES (?,?)", (row["id"], diode_serial_hash))
            result = self._job(row)
            result["state"] = "running"
            if worker is not None:
                result["claim_generation"] = 1
                result["claim_expires_at"] = claim_expires_at
                result["claim_purpose"] = "execute"
            # Display-only identity for the worker host's local log: a short
            # id always, and the profile name only when one is on record.
            # Neither value enters the job payload or any receipt.
            result["owner_short_id"] = row["owner"][:8]
            display = db.execute("SELECT display_name FROM account_display WHERE owner=?",
                                 (row["owner"],)).fetchone()
            if display is not None:
                result["owner_display_name"] = display["display_name"]
            return result

    def set_account_display(self, owner, display_name):
        """Record the profile name shown on the worker host's local display."""
        if not isinstance(display_name, str) or not display_name.strip() \
                or len(display_name) > 64:
            raise ValueError("A bounded display name is required")
        with self.transaction() as db:
            if not db.execute("SELECT 1 FROM accounts WHERE id=?", (owner,)).fetchone():
                raise KeyError("Account not found")
            db.execute("INSERT INTO account_display(owner, display_name, updated_at) "
                       "VALUES (?,?,?) ON CONFLICT(owner) DO UPDATE SET "
                       "display_name=excluded.display_name, updated_at=excluded.updated_at",
                       (owner, display_name.strip(), time.time()))

    def account_display_name(self, owner):
        with self.transaction() as db:
            row = db.execute("SELECT display_name FROM account_display WHERE owner=?",
                             (owner,)).fetchone()
            return row["display_name"] if row else None

    # --- D-Planet account linking (account-linking-design-20261119 §3.1) ---
    # account_links is the source of truth for consumption routing (L3);
    # D-Planet's si_links is only a mirror for display and its own unlink.

    @staticmethod
    def _account_link(row):
        if row is None:
            return None
        return {"si_owner": row["si_owner"], "dplanet_player_id": row["dplanet_player_id"],
                "link_id": row["link_id"], "linked_at": row["linked_at"],
                "unlinked_at": row["unlinked_at"], "status": row["status"]}

    def account_link(self, owner):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM account_links WHERE si_owner=?", (owner,)).fetchone()
            return self._account_link(row)

    def active_account_link(self, owner):
        link = self.account_link(owner)
        return link if link is not None and link["status"] == "active" else None

    def active_link_for_player(self, player_id):
        """Resolve a D-Planet player to its active SI link, if any.

        The delegated jobs API (prayer-npc-delegation-spec-20261120)
        keys requests by player; an unlinked or since-unlinked player
        resolves to None, which the API answers 409 — unlinking is the
        immediate revocation for delegated execution.
        """
        if not isinstance(player_id, str) or not player_id:
            return None
        with self.transaction() as db:
            row = db.execute("SELECT * FROM account_links WHERE dplanet_player_id=? "
                             "AND status='active'", (player_id,)).fetchone()
            return self._account_link(row) if row is not None else None

    def record_delegated_job(self, job, *, player_id, owner, link_id, operation,
                             consent=None):
        """Attribute a job created through the internal jobs API to
        the player it was submitted for. Irradiation rows store the
        consent record the charge was accepted under, so every
        delegated state change stays auditable to the player. A
        request_key replay returns the already-created job and keeps
        the first record (INSERT OR IGNORE)."""
        encoded = (json.dumps(consent, sort_keys=True, allow_nan=False)
                   if consent is not None else None)
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO delegated_jobs VALUES (?,?,?,?,?,?,?)",
                       (job, player_id, owner, link_id, operation, encoded, time.time()))

    def delegated_job(self, job):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM delegated_jobs WHERE job=?", (job,)).fetchone()
            if row is None:
                return None
            return {"job_id": row["job"], "player_id": row["player_id"],
                    "si_owner": row["si_owner"], "link_id": row["link_id"],
                    "operation": row["operation"],
                    "consent": (json.loads(row["consent"])
                                if row["consent"] is not None else None),
                    "created_at": row["created_at"]}

    def record_app_job(self, job, *, owner, operation, consent=None,
                       notification=None,
                       require_intent_id=_PROVENANCE_NOT_REQUESTED):
        """Attribute a job created through the standalone app API to
        the account it was submitted for. Irradiation rows store the
        server-generated consent record (§5.4). A request_key replay
        returns the already-created job and keeps the first record
        (INSERT OR IGNORE), exactly like delegated jobs.

        The completion-notification choice (foundation item 2) is
        written in the SAME transaction when provided (Astra
        round-3 F2), and the submission intent that carried it is
        consumed: attribution, choice and intent can no longer
        commit apart."""
        encoded = (json.dumps(consent, sort_keys=True, allow_nan=False)
                   if consent is not None else None)
        if notification is not None:
            self._validate_notification_choice(
                operation, notification["mode"], notification.get("email"))
        with self.transaction() as db:
            # Attribution must never bless another account's job:
            # the app surface trusts app_jobs.si_owner, so the row
            # is only written when it matches the job's real owner.
            row = db.execute("SELECT owner FROM jobs WHERE id=?", (job,)).fetchone()
            if row is None or row["owner"] != owner:
                raise KeyError("Job not found")
            if require_intent_id is not _PROVENANCE_NOT_REQUESTED \
                    and db.execute(
                        "SELECT 1 FROM app_jobs WHERE job=?",
                        (job,)).fetchone() is None:
                # Provenance enforcement inside the attribution
                # transaction (Astra round-5 F11): a job this intent
                # did not create — e.g. a foreign-channel job that
                # won the race for the request key after the caller's
                # own lookup — carries no matching stamp and must
                # never gain app attribution from it. A required id
                # that is absent or empty fails closed (round-6
                # F11): missing provenance is never "equal" to a
                # missing stamp.
                if not require_intent_id:
                    raise ValueError(
                        "required submission provenance is missing")
                jrow = db.execute("SELECT payload FROM jobs WHERE id=?",
                                  (job,)).fetchone()
                stamp = (json.loads(jrow["payload"]).get("app_intent")
                         if jrow is not None else None)
                if stamp != require_intent_id:
                    raise ValueError("submission provenance mismatch")
            db.execute("INSERT OR IGNORE INTO app_jobs VALUES (?,?,?,?,?)",
                       (job, owner, operation, encoded, time.time()))
            if notification is not None:
                self._insert_notification_row(
                    db, job, owner=owner, operation=operation,
                    mode=notification["mode"],
                    email=notification.get("email"), now=time.time())
                row = db.execute("SELECT request_key FROM jobs WHERE id=?",
                                 (job,)).fetchone()
                if row is not None:
                    db.execute(
                        "DELETE FROM app_submission_intents "
                        "WHERE owner=? AND request_key=?",
                        (owner, row["request_key"]))

    def app_job(self, job):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM app_jobs WHERE job=?", (job,)).fetchone()
            if row is None:
                return None
            return {"job_id": row["job"], "si_owner": row["si_owner"],
                    "operation": row["operation"],
                    "consent": (json.loads(row["consent"])
                                if row["consent"] is not None else None),
                    "created_at": row["created_at"]}

    def recent_app_jobs(self, owner, *, limit=20):
        """The owner's most recent app sessions (design §2 S8:
        latest 20 only), newest first, joined with the job rows.
        Each row carries the job's display fields (targets,
        intention, self_love_joy) so the history stays fully
        labelled after the job content itself expires."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid history limit")
        with self.transaction() as db:
            rows = db.execute(
                "SELECT a.job AS job_id, a.operation AS operation, "
                "a.created_at AS created_at, j.state AS state, j.cost AS cost, "
                "j.payload AS payload "
                "FROM app_jobs a JOIN jobs j ON j.id = a.job AND j.owner = a.si_owner "
                "WHERE a.si_owner=? ORDER BY a.created_at DESC, a.job DESC LIMIT ?",
                (owner, limit)).fetchall()
            # Summaries for the whole page in one query; payloads
            # are decoded only for rows that predate summaries, and
            # target-name lookups share one cache across the page.
            summaries = {}
            if rows:
                ids = [row["job_id"] for row in rows]
                marks = ",".join("?" for _ in ids)
                for srow in db.execute(
                        "SELECT job,targets,intention,self_love_joy "
                        f"FROM job_display_summaries WHERE job IN ({marks})",
                        ids).fetchall():
                    summaries[srow["job"]] = self._summary_fields(srow)
            cache = {}
            sessions = []
            for row in rows:
                entry = {"job_id": row["job_id"], "operation": row["operation"],
                         "created_at": row["created_at"], "state": row["state"],
                         "cost": row["cost"]}
                fields = summaries.get(row["job_id"])
                if fields is None:
                    fields = self._fallback_fields(db, owner, row["payload"],
                                                   cache=cache)
                entry.update(fields)
                sessions.append(entry)
            return sessions

    # --- Completion notifications (foundation work item 2) ---
    # The choice is recorded once per app job at submission (first
    # record wins, exactly like app attribution, so a request-key
    # replay can never flip it). Terminal job transitions note the
    # finish inside their own transaction (_note_job_finished):
    # notify-mode queues immediately; wait-mode stays armed and the
    # maintenance sweep escalates it if the result is never viewed.
    # Only app jobs ever get a row here, so MCP and internal (NPC)
    # jobs — whose callers wait synchronously — never produce mail.

    @staticmethod
    def _validate_notification_choice(operation, mode, email):
        if operation not in ("scan", "irradiation"):
            raise ValueError("Invalid notification operation")
        if mode not in ("wait", "notify"):
            raise ValueError("Invalid completion mode")
        if email is not None and not plausible_email(email):
            raise ValueError("Invalid notification email")
        # "notify requires an address" is enforced at insert time in
        # _insert_notification_row, not here: a replay whose stored
        # record already exists must stay a no-op even when the
        # replaying token no longer carries the claim.

    def _insert_notification_row(self, db, job, *, owner, operation, mode,
                                 email, now):
        """INSERT OR IGNORE the choice inside the caller's
        transaction, then initialize it against the job's CURRENT
        state: a job that already finished before its choice was
        recorded (a fast worker, late attribution, crash repair)
        must not sit armed forever — notify queues immediately,
        wait arms with the finish stamped, and an owner-cancelled
        job is suppressed at once. The finish stamp for such late
        rows is the recording time: the jobs table keeps no finish
        timestamp, so the grace/TTL honestly run from discovery,
        never from an invented earlier moment."""
        cursor = db.execute(
            "INSERT OR IGNORE INTO job_notifications"
            "(job, owner, operation, mode, email, status, created_at) "
            "VALUES (?,?,?,?,?,'armed',?)",
            (job, owner, operation, mode, email, now))
        if cursor.rowcount != 1:
            return False
        if mode == "notify" and email is None:
            # A first record of notify without an address is a
            # promise the service cannot keep; raising here rolls
            # the caller's transaction (and this insert) back.
            raise ValueError("Notification email required for notify mode")
        viewed = db.execute("SELECT viewed_at FROM job_result_views "
                            "WHERE job=?", (job,)).fetchone()
        if viewed is not None:
            # The owner already saw the result before this record
            # existed (Astra round-3 F5): no mail is owed, whatever
            # the job's state.
            db.execute(
                "UPDATE job_notifications SET status='suppressed', "
                "viewed_at=?, email=NULL WHERE job=?",
                (viewed["viewed_at"], job))
            return True
        row = db.execute("SELECT state FROM jobs WHERE id=?",
                         (job,)).fetchone()
        state = row["state"] if row is not None else None
        if state in ("completed", "failed", "unknown"):
            if mode == "notify":
                db.execute(
                    "UPDATE job_notifications SET finished_at=?, "
                    "outcome=?, status='queued', queued_at=?, "
                    "next_attempt_at=? WHERE job=?",
                    (now, state, now, now, job))
            else:
                db.execute(
                    "UPDATE job_notifications SET finished_at=?, "
                    "outcome=? WHERE job=?", (now, state, job))
        elif state == "cancelled":
            db.execute(
                "UPDATE job_notifications SET status='suppressed', "
                "viewed_at=?, email=NULL WHERE job=?", (now, job))
        return True

    def set_job_notification(self, job, *, owner, operation, mode, email):
        self._validate_notification_choice(operation, mode, email)
        with self.transaction() as db:
            self._insert_notification_row(
                db, job, owner=owner, operation=operation, mode=mode,
                email=email, now=time.time())

    def job_notification(self, job):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM job_notifications WHERE job=?",
                             (job,)).fetchone()
            return dict(row) if row is not None else None

    def record_app_submission_intent(self, owner, request_key, *,
                                     operation, mode, email, reference=None,
                                     consent=None):
        """Persist the app submission intent BEFORE the job exists:
        the original choice, bound to owner + request key + a unique
        intent id, first record wins. Returns (winning_row, created)
        — callers must act on the WINNING row, never on what they
        presented, because a concurrent or earlier attempt may own
        the intent (Astra round-4 F2). The intent id is stamped into
        the job's payload at creation, which is what proves —
        atomically — that a later unattributed job really is this
        intent's job (Astra round-4 F11). Operation and reference
        are immutable parts of the binding (round-5 F13): callers
        reject a winner that names a different operation or
        confirmation. For irradiation, `consent` persists the
        verified consent snapshot before submission, so maintenance
        can finalize a crashed attempt without a replay (round-5
        F2)."""
        self._validate_notification_choice(operation, mode, email)
        intent_id = uuid.uuid4().hex
        encoded_consent = (json.dumps(consent, sort_keys=True,
                                      allow_nan=False)
                           if consent is not None else None)
        with self.transaction() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO app_submission_intents"
                "(owner, request_key, intent_id, operation, mode, email, "
                "reference, consent, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (owner, request_key, intent_id, operation, mode, email,
                 reference, encoded_consent, time.time()))
            created = cursor.rowcount == 1
            row = db.execute(
                "SELECT * FROM app_submission_intents "
                "WHERE owner=? AND request_key=?",
                (owner, request_key)).fetchone()
            if row["intent_id"] is None:
                # Legacy row from a pre-round-4 database (F12):
                # assign its id now. Jobs created earlier carry no
                # stamp and still cannot match it, so this
                # manufactures no provenance — only jobs created
                # after this call are stamped with it, by their own
                # submission.
                db.execute(
                    "UPDATE app_submission_intents SET intent_id=? "
                    "WHERE owner=? AND request_key=? "
                    "AND intent_id IS NULL",
                    (intent_id, owner, request_key))
                row = db.execute(
                    "SELECT * FROM app_submission_intents "
                    "WHERE owner=? AND request_key=?",
                    (owner, request_key)).fetchone()
            return dict(row), created

    def app_submission_intent(self, owner, request_key):
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM app_submission_intents "
                "WHERE owner=? AND request_key=?",
                (owner, request_key)).fetchone()
            return dict(row) if row is not None else None

    def discard_app_submission_intent(self, owner, request_key, intent_id):
        """Remove an intent whose submission was definitively
        rejected, so it cannot later lend false provenance to an
        unrelated job (Astra round-4 F11). Only the intent's own
        creator may discard it (intent id must match), and never
        once a job exists for the key."""
        with self.transaction() as db:
            db.execute(
                "DELETE FROM app_submission_intents WHERE owner=? "
                "AND request_key=? AND intent_id=? AND NOT EXISTS "
                "(SELECT 1 FROM jobs WHERE owner=? AND request_key=?)",
                (owner, request_key, intent_id, owner, request_key))

    def mark_result_viewed(self, owner, job):
        """Record, durably and independently of any notification
        row, that the owner saw this job's terminal result, and
        apply the view to the notification record — in ONE
        transaction (Astra round-4 F5): the marker and the record
        can no longer commit apart, and the delivery gate consults
        the marker itself as a second line of defence."""
        now = time.time()
        with self.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO job_result_views(job, owner, "
                "viewed_at) VALUES (?,?,?)", (job, owner, now))
            db.execute(
                "UPDATE job_notifications SET "
                "status=CASE WHEN status IN ('armed','queued') "
                "THEN 'suppressed' ELSE status END, "
                "viewed_at=?, "
                "email=CASE WHEN status IN ('armed','queued') "
                "THEN NULL ELSE email END, "
                "resume_token=CASE WHEN status IN ('armed','queued') "
                "THEN NULL ELSE resume_token END, "
                "mail_subject=CASE WHEN status IN ('armed','queued') "
                "THEN NULL ELSE mail_subject END, "
                "mail_body=CASE WHEN status IN ('armed','queued') "
                "THEN NULL ELSE mail_body END, "
                "mail_sender=CASE WHEN status IN ('armed','queued') "
                "THEN NULL ELSE mail_sender END "
                "WHERE job=? AND owner=? "
                "AND status IN ('armed','queued','sending')",
                (now, job, owner))

    def _note_job_finished(self, db, job, state):
        """Inside the terminal transition's own transaction: stamp
        the finish; a notify-mode record queues at once, a wait-mode
        record stays armed for the escalation sweep."""
        now = time.time()
        db.execute(
            "UPDATE job_notifications SET finished_at=?, outcome=?, "
            "status=CASE WHEN mode='notify' THEN 'queued' ELSE status END, "
            "queued_at=CASE WHEN mode='notify' THEN ? ELSE queued_at END, "
            "next_attempt_at=CASE WHEN mode='notify' THEN ? "
            "ELSE next_attempt_at END "
            "WHERE job=? AND status='armed'",
            (now, state, now, now, job))

    def _suppress_notification(self, db, job):
        """The owner acted in person (cancel): no mail is owed."""
        db.execute(
            "UPDATE job_notifications SET status='suppressed', "
            "viewed_at=?, email=NULL, resume_token=NULL, "
            "mail_subject=NULL, mail_body=NULL, mail_sender=NULL "
            "WHERE job=? AND status IN ('armed','queued')",
            (time.time(), job))

    def escalate_completion_notifications(self, now=None):
        """Maintenance sweep (pure bookkeeping, no sending):
        reclaim stale claims, backfill finishes the transition hook
        did not stamp, expire records whose job finished long ago,
        and escalate wait-mode records whose result was never
        viewed within the grace window."""
        now = time.time() if now is None else now
        counts = {"reclaimed": 0, "backfilled": 0, "expired": 0,
                  "escalated": 0, "viewed": 0, "intents_purged": 0}
        with self.transaction() as db:
            # Reclaim stale claims. A reclaimed claim counts as an
            # attempt (a sender that never reported back is not free
            # to loop forever), a viewed result is suppressed instead
            # of requeued, and the resume token is KEPT: the stalled
            # send may in fact have been accepted, and its link must
            # keep working for the retry's identical payload.
            stale = db.execute(
                "SELECT job, attempts, viewed_at FROM job_notifications "
                "WHERE status='sending' AND claimed_at<=?",
                (now - CLAIM_STALE_SECONDS,)).fetchall()
            for row in stale:
                attempts = row["attempts"] + 1
                if row["viewed_at"] is not None:
                    db.execute(
                        "UPDATE job_notifications SET status='suppressed', "
                        "email=NULL, resume_token=NULL, claimed_at=NULL, "
                        "claim_id=NULL, attempts=?, mail_subject=NULL, "
                        "mail_body=NULL, mail_sender=NULL WHERE job=?",
                        (attempts, row["job"]))
                elif attempts >= MAX_DELIVERY_ATTEMPTS:
                    db.execute(
                        "UPDATE job_notifications SET status='expired', "
                        "email=NULL, resume_token=NULL, "
                        "claimed_at=NULL, claim_id=NULL, attempts=?, "
                        "mail_subject=NULL, mail_body=NULL, "
                        "mail_sender=NULL WHERE job=?",
                        (attempts, row["job"]))
                else:
                    backoff = min(3600, 60 * (2 ** attempts))
                    db.execute(
                        "UPDATE job_notifications SET status='queued', "
                        "claimed_at=NULL, claim_id=NULL, attempts=?, "
                        "next_attempt_at=? WHERE job=?",
                        (attempts, now + backoff, row["job"]))
                counts["reclaimed"] += 1
            rows = db.execute(
                "SELECT n.job AS job, n.mode AS mode, j.state AS state "
                "FROM job_notifications n "
                "JOIN jobs j ON j.id=n.job WHERE n.status='armed' "
                "AND n.finished_at IS NULL "
                "AND j.state IN ('completed','failed','cancelled','unknown')"
                ).fetchall()
            for row in rows:
                if row["state"] == "cancelled":
                    db.execute(
                        "UPDATE job_notifications SET status='suppressed', "
                        "viewed_at=?, email=NULL WHERE job=? "
                        "AND status='armed' AND finished_at IS NULL",
                        (now, row["job"]))
                elif row["mode"] == "notify":
                    db.execute(
                        "UPDATE job_notifications SET finished_at=?, "
                        "outcome=?, status='queued', queued_at=?, "
                        "next_attempt_at=? WHERE job=? AND status='armed' "
                        "AND finished_at IS NULL",
                        (now, row["state"], now, now, row["job"]))
                else:
                    db.execute(
                        "UPDATE job_notifications SET finished_at=?, outcome=? "
                        "WHERE job=? AND status='armed' AND finished_at IS NULL",
                        (now, row["state"], row["job"]))
                counts["backfilled"] += 1
            # A durable view marker beats any pending mail, however
            # the record got here (Astra round-3 F5).
            cursor = db.execute(
                "UPDATE job_notifications SET status='suppressed', "
                "email=NULL, resume_token=NULL, mail_subject=NULL, "
                "mail_body=NULL, mail_sender=NULL "
                "WHERE status IN ('armed','queued') AND job IN "
                "(SELECT job FROM job_result_views)")
            counts["viewed"] = cursor.rowcount
            # Submission intents are recovery scaffolding, not a
            # record: purge week-old rows — except any whose job
            # still exists unattributed, which reconciliation (not
            # deletion) must resolve first (Astra round-4 F2).
            cursor = db.execute(
                "DELETE FROM app_submission_intents WHERE created_at<=? "
                "AND (intent_id IS NULL OR NOT EXISTS (SELECT 1 FROM jobs j "
                "LEFT JOIN app_jobs a ON a.job=j.id "
                "WHERE j.owner=app_submission_intents.owner "
                "AND j.request_key=app_submission_intents.request_key "
                "AND a.job IS NULL))",
                (now - 7 * 24 * 3600,))
            counts["intents_purged"] = cursor.rowcount
            cursor = db.execute(
                "UPDATE job_notifications SET status='expired', email=NULL, "
                "resume_token=NULL, claim_id=NULL, mail_subject=NULL, "
                "mail_body=NULL, mail_sender=NULL "
                "WHERE status IN ('armed','queued') AND finished_at IS NOT NULL "
                "AND finished_at<=?", (now - NOTIFICATION_TTL_SECONDS,))
            counts["expired"] = cursor.rowcount
            cursor = db.execute(
                "UPDATE job_notifications SET status='queued', queued_at=?, "
                "next_attempt_at=? WHERE mode='wait' AND status='armed' "
                "AND email IS NOT NULL AND finished_at IS NOT NULL "
                "AND finished_at<=?",
                (now, now, now - ESCALATION_GRACE_SECONDS))
            counts["escalated"] = cursor.rowcount
        return counts

    def claim_completion_notifications(self, now=None, *, limit=20):
        """Atomically claim due queued rows for one delivery cycle.
        A row whose claim update does not land (a racing cycle took
        it) is simply not returned."""
        now = time.time() if now is None else now
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid claim limit")
        claimed = []
        with self.transaction() as db:
            rows = db.execute(
                "SELECT * FROM job_notifications WHERE status='queued' "
                "AND email IS NOT NULL "
                "AND (next_attempt_at IS NULL OR next_attempt_at<=?) "
                "ORDER BY queued_at, job LIMIT ?", (now, limit)).fetchall()
            for row in rows:
                claim_id = uuid.uuid4().hex
                cursor = db.execute(
                    "UPDATE job_notifications SET status='sending', "
                    "claimed_at=?, claim_id=? WHERE job=? AND status='queued'",
                    (now, claim_id, row["job"]))
                if cursor.rowcount == 1:
                    entry = dict(row)
                    entry["status"] = "sending"
                    entry["claimed_at"] = now
                    entry["claim_id"] = claim_id
                    claimed.append(entry)
        return claimed

    def set_notification_resume_token(self, job, token, token_hash,
                                      expires_at, *, claim_id):
        """Bind the resume token of an in-flight send, fenced to the
        claim that minted it: a stale sender whose claim was
        reclaimed and re-issued can no longer overwrite the live
        claim's token. The plaintext is held only while a send is in
        flight or awaiting retry (so every retry mails the identical
        payload under the same provider idempotency key) and is
        erased at every terminal state; the hash alone survives a
        successful send for redemption."""
        with self.transaction() as db:
            cursor = db.execute(
                "UPDATE job_notifications SET resume_token=?, "
                "resume_token_hash=?, resume_expires_at=? "
                "WHERE job=? AND status='sending' AND claim_id=? "
                "AND resume_token_hash IS NULL",
                (token, token_hash, expires_at, job, claim_id))
            return cursor.rowcount == 1

    def mark_notification_sent(self, job, *, claim_id, now=None):
        now = time.time() if now is None else now
        with self.transaction() as db:
            cursor = db.execute(
                "UPDATE job_notifications SET status='sent', sent_at=?, "
                "email=NULL, resume_token=NULL, claimed_at=NULL, "
                "claim_id=NULL, mail_subject=NULL, mail_body=NULL, "
                "mail_sender=NULL WHERE job=? AND status='sending' "
                "AND claim_id=?", (now, job, claim_id))
            return cursor.rowcount == 1

    def mark_notification_failed(self, job, error, *, claim_id, now=None):
        """Bounded retries with exponential backoff, fenced to the
        reporting claim. The resume token is KEPT across retries:
        the failed send may in fact have been accepted, so its link
        must keep working and the retry must mail the identical
        payload. A result the owner has meanwhile viewed is
        suppressed instead of requeued. The final failure expires
        the record and erases the address and token, so a dead
        address is never retried forever and never mailed late."""
        now = time.time() if now is None else now
        error = str(error)[:200] if error else "unknown"
        with self.transaction() as db:
            row = db.execute(
                "SELECT attempts, viewed_at FROM job_notifications "
                "WHERE job=? AND status='sending' AND claim_id=?",
                (job, claim_id)).fetchone()
            if row is None:
                return False
            attempts = row["attempts"] + 1
            if row["viewed_at"] is not None:
                db.execute(
                    "UPDATE job_notifications SET status='suppressed', "
                    "attempts=?, last_error=?, email=NULL, "
                    "resume_token=NULL, claimed_at=NULL, claim_id=NULL, "
                    "mail_subject=NULL, mail_body=NULL, mail_sender=NULL "
                    "WHERE job=?", (attempts, error, job))
            elif attempts >= MAX_DELIVERY_ATTEMPTS:
                # Delivery ends here, but locator validity does not:
                # an earlier attempt may in fact have been accepted,
                # so the hash and its expiry survive until the
                # link's own seven days run out (Astra round-3 F3).
                db.execute(
                    "UPDATE job_notifications SET status='expired', "
                    "attempts=?, last_error=?, email=NULL, claimed_at=NULL, "
                    "claim_id=NULL, resume_token=NULL, "
                    "mail_subject=NULL, mail_body=NULL, mail_sender=NULL "
                    "WHERE job=?", (attempts, error, job))
            else:
                backoff = min(3600, 60 * (2 ** attempts))
                db.execute(
                    "UPDATE job_notifications SET status='queued', "
                    "attempts=?, last_error=?, claimed_at=NULL, "
                    "claim_id=NULL, next_attempt_at=? WHERE job=?",
                    (attempts, error, now + backoff, job))
            return True

    def mark_notification_viewed(self, owner, job):
        """The owner saw the terminal result in the app: an armed or
        queued mail is now noise, so suppress it and erase the
        address. A send already in flight cannot be recalled, but
        the view is still RECORDED on a sending row: if that attempt
        fails or goes stale, the failure/reclaim paths see the view
        and suppress instead of mailing a result already seen."""
        with self.transaction() as db:
            db.execute(
                "UPDATE job_notifications SET "
                "status=CASE WHEN status IN ('armed','queued') "
                "THEN 'suppressed' ELSE status END, "
                "viewed_at=?, "
                "email=CASE WHEN status IN ('armed','queued') "
                "THEN NULL ELSE email END, "
                "resume_token=CASE WHEN status IN ('armed','queued') "
                "THEN NULL ELSE resume_token END, "
                "mail_subject=CASE WHEN status IN ('armed','queued') "
                "THEN NULL ELSE mail_subject END, "
                "mail_body=CASE WHEN status IN ('armed','queued') "
                "THEN NULL ELSE mail_body END, "
                "mail_sender=CASE WHEN status IN ('armed','queued') "
                "THEN NULL ELSE mail_sender END "
                "WHERE job=? AND owner=? "
                "AND status IN ('armed','queued','sending')",
                (time.time(), job, owner))

    def set_notification_envelope(self, job, *, claim_id, subject, body,
                                  sender):
        """Persist the immutable delivery envelope (Astra round-3
        F10) before the first send attempt, fenced to the live
        claim: sender, subject and body exactly as they will be
        sent, so every retry reproduces the identical payload under
        the same idempotency key even if templates, origin or
        sender configuration change between attempts. The envelope
        is erased at every terminal state."""
        with self.transaction() as db:
            cursor = db.execute(
                "UPDATE job_notifications SET mail_subject=?, "
                "mail_body=?, mail_sender=? WHERE job=? "
                "AND status='sending' AND claim_id=? "
                "AND mail_subject IS NULL",
                (subject, body, sender, job, claim_id))
            return cursor.rowcount == 1

    def notification_delivery_gate(self, job, *, claim_id):
        """The last check before transport starts (Astra round-3
        F5): 'send' only while this claim is live and the result is
        unviewed. A viewed result is suppressed here — claim,
        address, plaintext token and envelope erased — and reported
        as 'suppressed'; a claim that is no longer ours is 'gone'."""
        with self.transaction() as db:
            row = db.execute(
                "SELECT status, claim_id, viewed_at FROM "
                "job_notifications WHERE job=?", (job,)).fetchone()
            if row is None or row["status"] != "sending" \
                    or row["claim_id"] != claim_id:
                return "gone"
            marker = db.execute(
                "SELECT 1 FROM job_result_views WHERE job=?",
                (job,)).fetchone()
            if row["viewed_at"] is not None or marker is not None:
                db.execute(
                    "UPDATE job_notifications SET status='suppressed', "
                    "email=NULL, resume_token=NULL, claimed_at=NULL, "
                    "claim_id=NULL, mail_subject=NULL, mail_body=NULL, "
                    "mail_sender=NULL WHERE job=?", (job,))
                return "suppressed"
            return "send"

    def redeem_resume_token(self, owner, token, now=None):
        """Resolve a resume token for its owner: the mail link is a
        locator, never a credential — redemption still requires the
        owner's authenticated session (checked by the caller) and
        answers only {job_id, operation}. Unknown tokens and other
        owners' tokens both answer None (no existence leak); an
        expired token of the owner's own raises ValueError so the
        app can say the link aged out. Tokens stay redeemable until
        expiry: mail clients prefetch and users re-click, and the
        session check is what guards the data."""
        now = time.time() if now is None else now
        if not isinstance(token, str) or not token:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM job_notifications WHERE resume_token_hash=?",
                (digest,)).fetchone()
            if row is None or row["owner"] != owner:
                return None
            if row["resume_expires_at"] is None \
                    or row["resume_expires_at"] <= now:
                raise ValueError("resume_expired")
            return {"job_id": row["job"], "operation": row["operation"]}

    def reconcile_app_submission_intents(self, now=None):
        """Autonomous recovery for app submissions that crashed
        between job creation and attribution (Astra round-4 F2,
        completed in round 5):
        - Scan: an intent whose job exists — proven by the intent
          id stamped into the job's payload at creation (F11) — but
          has no attribution is attributed now, with the intent's
          original choice, without waiting for a replay.
        - Irradiation: the intent carries the verified consent
          snapshot and the confirmation reference recorded before
          submission, so the sweep finalizes attribution with
          exactly the records the handler would have written; the
          store's own confirmation binding (the claim marker for
          this request key, or this job) is re-validated inside
          record_app_irradiation.
        A short grace keeps the sweep from racing a live request."""
        now = time.time() if now is None else now
        with self.transaction() as db:
            rows = db.execute(
                "SELECT * FROM app_submission_intents "
                "WHERE created_at<=?", (now - 60,)).fetchall()
        reconciled = 0
        for intent in rows:
            job = self.get_by_request(intent["owner"],
                                      intent["request_key"])
            if job is None:
                continue
            if self.app_job(job["id"]) is not None:
                continue
            if intent["operation"] == "scan":
                if not intent["intent_id"] \
                        or job["payload"].get("app_intent") \
                        != intent["intent_id"]:
                    # No provenance, no attribution (round-6 F11):
                    # an absent id never "matches" an absent stamp.
                    continue
                self.record_app_job(
                    job["id"], owner=intent["owner"], operation="scan",
                    notification={"mode": intent["mode"],
                                  "email": intent["email"]},
                    require_intent_id=intent["intent_id"])
                reconciled += 1
            elif intent["operation"] == "irradiation":
                if job["payload"].get("operation") != "irradiation":
                    continue
                if not intent["intent_id"] \
                        or job["payload"].get("app_intent") \
                        != intent["intent_id"]:
                    # The job must carry this intent's stamp
                    # (round-6 F14): a same-key foreign irradiation
                    # is never finalized against this confirmation;
                    # record_app_irradiation additionally validates
                    # scan, count and charged total.
                    continue
                if not intent["reference"] or not intent["consent"]:
                    continue  # Nothing durable to finalize with.
                confirmation = self.app_confirmation(
                    intent["owner"], intent["reference"])
                if confirmation is None:
                    continue
                if confirmation["used_job"] not in (
                        self._APP_CONFIRMATION_INTENT
                        + intent["request_key"], job["id"]):
                    continue  # The claim is not this attempt's.
                try:
                    self.record_app_irradiation(
                        job["id"], owner=intent["owner"],
                        consent=json.loads(intent["consent"]),
                        confirmation_id=intent["reference"],
                        request_key=intent["request_key"],
                        notification={"mode": intent["mode"],
                                      "email": intent["email"]})
                except ValueError:
                    continue
                reconciled += 1
        return reconciled

    def run_notification_cycle(self):
        """The maintenance entry point: reconciliation and
        bookkeeping always run; sending runs only while a delivery
        is attached (fail-soft while unconfigured)."""
        self.reconcile_app_submission_intents()
        delivery = getattr(self, "notification_delivery", None)
        if delivery is not None:
            delivery.run_once()
        else:
            self.escalate_completion_notifications()

    def create_app_confirmation(self, owner, scan_id, quote, *, ttl_seconds=600):
        """Park an irradiation confirmation for the app (§5.4): the
        quote the S5 screen showed, bound to this owner and scan.
        Returns the confirmation id the confirm tap presents back."""
        if not isinstance(scan_id, str) or not scan_id:
            raise ValueError("Scan identity required")
        # The credits_per_shot column holds the accepted rate in
        # tenths of a credit since the 2026-11-22 beta price (39 =
        # 3.9 credits/shot), so a fractional rate is stored exactly.
        # Confirmations live at most ttl_seconds, so no long-lived
        # rows predate the change.
        count, per_shot_tenths, total = (quote["count"],
                                         quote["credits_per_shot_tenths"],
                                         quote["credits"])
        now = time.time()
        confirmation_id = str(uuid.uuid4())
        with self.transaction() as db:
            db.execute("INSERT INTO app_irradiation_confirmations VALUES (?,?,?,?,?,?,?,?,NULL)",
                       (confirmation_id, owner, scan_id, count, per_shot_tenths, total,
                        now, now + ttl_seconds))
        return confirmation_id

    def app_confirmation(self, owner, confirmation_id):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM app_irradiation_confirmations "
                             "WHERE id=? AND owner=?", (confirmation_id, owner)).fetchone()
            if row is None:
                return None
            return {"id": row["id"], "scan_id": row["scan_id"], "count": row["count"],
                    "credits_per_shot": rate_display(row["credits_per_shot"]),
                    "credits_per_shot_tenths": row["credits_per_shot"],
                    "total_credits": row["total_credits"],
                    "created_at": row["created_at"], "expires_at": row["expires_at"],
                    "used_job": row["used_job"]}

    def consume_app_confirmation(self, owner, confirmation_id, job_id, *, now=None):
        """Mark a confirmation used by exactly one job. Raises
        ValueError when it is unknown, another owner's, expired, or
        already used — an irradiate call without a live confirmation
        is never accepted (§5.4)."""
        moment = time.time() if now is None else now
        with self.transaction() as db:
            row = db.execute("SELECT * FROM app_irradiation_confirmations "
                             "WHERE id=? AND owner=?", (confirmation_id, owner)).fetchone()
            if row is None:
                raise ValueError("Confirmation not found")
            if row["used_job"] is not None:
                raise ValueError("Confirmation already used")
            if moment > row["expires_at"]:
                raise ValueError("Confirmation expired")
            db.execute("UPDATE app_irradiation_confirmations SET used_job=? WHERE id=?",
                       (job_id, confirmation_id))

    # Confirmation claiming (Astra review 2026-11-22, batch 1):
    # consume-after-submit cannot be atomic with job creation, so a
    # confirm is claimed BEFORE anything is submitted. The claim is
    # the used_job column holding an intent marker bound to the
    # caller's request_key; only the holder may turn it into a job,
    # and record_app_irradiation() swaps the marker for the job id in
    # the same transaction as the app attribution. A failed
    # submission releases the claim so the user can retry, and a
    # retry carrying the same request_key resumes its own claim, so
    # a crash between submission and attribution heals on retry
    # instead of leaving a charged job invisible to the app.
    _APP_CONFIRMATION_INTENT = "intent:"

    def claim_app_confirmation(self, owner, confirmation_id, request_key, *, now=None):
        """Atomically claim a confirmation for one irradiate attempt.

        Returns {"state": "claimed"|"resumed"|"used", "job_id": ...}:
        "claimed"/"resumed" mean this request_key holds the claim and
        may submit; "used" means an earlier attempt already finalized
        the confirmation into job_id. Raises ValueError when the
        confirmation is unknown, expired before any claim, or claimed
        by a different request_key.
        """
        moment = time.time() if now is None else now
        marker = self._APP_CONFIRMATION_INTENT + request_key
        with self.transaction() as db:
            row = db.execute("SELECT * FROM app_irradiation_confirmations "
                             "WHERE id=? AND owner=?", (confirmation_id, owner)).fetchone()
            if row is None:
                raise ValueError("Confirmation not found")
            used = row["used_job"]
            if used is None:
                if moment > row["expires_at"]:
                    raise ValueError("Confirmation expired")
                db.execute("UPDATE app_irradiation_confirmations SET used_job=? WHERE id=?",
                           (marker, confirmation_id))
                return {"state": "claimed", "job_id": None}
            if used == marker:
                return {"state": "resumed", "job_id": None}
            if used.startswith(self._APP_CONFIRMATION_INTENT):
                raise ValueError("Confirmation already used")
            return {"state": "used", "job_id": used}

    def release_app_confirmation_claim(self, owner, confirmation_id, request_key):
        """Give back this attempt's claim after a failed submission.

        Only the holder's own intent marker is cleared; a finalized
        confirmation (real job id) is never reopened. The claim is
        ALSO kept once a job exists for the request key (Astra
        round-7 F15): same-key attempts share the marker, so a
        failing twin must not clear the claim out from under a
        concurrent attempt that already committed its job — and a
        submission that failed ambiguously may have committed its
        own job, which the claim then belongs to.
        """
        with self.transaction() as db:
            db.execute("UPDATE app_irradiation_confirmations SET used_job=NULL "
                       "WHERE id=? AND owner=? AND used_job=? "
                       "AND NOT EXISTS (SELECT 1 FROM jobs "
                       "WHERE owner=? AND request_key=?)",
                       (confirmation_id, owner,
                        self._APP_CONFIRMATION_INTENT + request_key,
                        owner, request_key))

    def record_app_irradiation(self, job_id, *, owner, consent, confirmation_id,
                               request_key, notification=None):
        """Attribute an app irradiation job and finalize its
        confirmation in one transaction. The confirmation must be
        held by this attempt (its intent marker) or already bound to
        this job; anything else raises ValueError, so attribution can
        never bless a job the confirmation did not authorize.

        The completion-notification choice (foundation item 2) is
        written in the SAME transaction when provided: attribution
        and the choice can no longer commit apart, so a crash
        between them cannot silently lose the user's preference.
        """
        encoded = (json.dumps(consent, sort_keys=True, allow_nan=False)
                   if consent is not None else None)
        if notification is not None:
            self._validate_notification_choice(
                "irradiation", notification["mode"],
                notification.get("email"))
        with self.transaction() as db:
            row = db.execute("SELECT * FROM app_irradiation_confirmations "
                             "WHERE id=? AND owner=?", (confirmation_id, owner)).fetchone()
            if row is None:
                raise ValueError("Confirmation not found")
            if row["used_job"] not in (self._APP_CONFIRMATION_INTENT + request_key, job_id):
                raise ValueError("Confirmation already used")
            # The job being finalized must be the job the
            # confirmation authorized — same scan, same count, same
            # charged total (Astra round-6 F14). A claim marker
            # proves a reservation, not that this job was created
            # from it; reconciliation and replay both rely on this
            # check living in the attribution transaction.
            jrow = db.execute("SELECT payload, cost, owner FROM jobs WHERE id=?",
                              (job_id,)).fetchone()
            if jrow is None or jrow["owner"] != owner:
                raise ValueError("Job not found")
            jpayload = json.loads(jrow["payload"])
            if jpayload.get("operation") != "irradiation" \
                    or jpayload.get("scan_id") != row["scan_id"] \
                    or jpayload.get("irradiation_count") != row["count"] \
                    or jrow["cost"] != row["total_credits"]:
                raise ValueError("Confirmation terms do not match the job")
            # First attribution must prove provenance (Astra round-7
            # F14): the durable intent for this key names this
            # operation and this confirmation, and the job carries
            # its stamp. Matching terms alone never suffice — an
            # unstamped foreign job with identical terms is refused.
            # An already-finalized binding (this confirmation bound
            # to this job, attribution present) was validated when
            # it was written and replays as-is.
            finalized = row["used_job"] == job_id and db.execute(
                "SELECT 1 FROM app_jobs WHERE job=?",
                (job_id,)).fetchone() is not None
            if not finalized:
                irow = db.execute(
                    "SELECT * FROM app_submission_intents "
                    "WHERE owner=? AND request_key=?",
                    (owner, request_key)).fetchone()
                if irow is None or not irow["intent_id"] \
                        or irow["operation"] != "irradiation" \
                        or irow["reference"] != confirmation_id \
                        or jpayload.get("app_intent") != irow["intent_id"]:
                    raise ValueError("submission provenance mismatch")
            db.execute("INSERT OR IGNORE INTO app_jobs VALUES (?,?,?,?,?)",
                       (job_id, owner, "irradiation", encoded, time.time()))
            db.execute("UPDATE app_irradiation_confirmations SET used_job=? WHERE id=?",
                       (job_id, confirmation_id))
            if notification is not None:
                self._insert_notification_row(
                    db, job_id, owner=owner, operation="irradiation",
                    mode=notification["mode"],
                    email=notification.get("email"), now=time.time())
                db.execute(
                    "DELETE FROM app_submission_intents "
                    "WHERE owner=? AND request_key=?",
                    (owner, request_key))

    # --- Native-app AI readings (native-reading-design-20261122) ---
    # The charge discipline mirrors jobs exactly: reserve in one
    # transaction, settle in the same transaction that stores the
    # result, release in full on any failure — the user never pays
    # for a reading that was not delivered. Routed accounts reserve
    # in D-Planet first (the reading id is the opaque reservation
    # key; D-Planet's si-credits ops are keyed by that string and
    # never reference SI jobs) and settle/release through the same
    # pending-ops outbox as jobs.

    def create_app_reading_confirmation(self, owner, scan_id, quote, *, ttl_seconds=600):
        """Park a reading confirmation (the S4c screen's quote):
        the price the user is shown, bound to this owner and scan."""
        if not isinstance(scan_id, str) or not scan_id:
            raise ValueError("Scan identity required")
        credits, version = quote["credits"], quote["price_version"]
        now = time.time()
        confirmation_id = str(uuid.uuid4())
        with self.transaction() as db:
            db.execute("INSERT INTO app_reading_confirmations VALUES (?,?,?,?,?,?,?,NULL)",
                       (confirmation_id, owner, scan_id, credits, version,
                        now, now + ttl_seconds))
        return confirmation_id

    def app_reading_confirmation(self, owner, confirmation_id):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM app_reading_confirmations "
                             "WHERE id=? AND owner=?", (confirmation_id, owner)).fetchone()
            if row is None:
                return None
            return {"id": row["id"], "scan_id": row["scan_id"],
                    "credits": row["credits"], "price_version": row["price_version"],
                    "created_at": row["created_at"], "expires_at": row["expires_at"],
                    "used_reading": row["used_reading"]}

    @staticmethod
    def _reading(row):
        return {"id": row["id"], "scan_id": row["scan_id"], "state": row["state"],
                "credits": row["credits"], "price_version": row["price_version"],
                "model": row["model"], "text": row["text"],
                "error_code": row["error_code"], "created_at": row["created_at"],
                "finished_at": row["finished_at"],
                "text_expired": row["state"] == "completed" and row["text"] is None}

    def get_reading(self, owner, reading_id):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM app_readings WHERE id=? AND owner=?",
                             (reading_id, owner)).fetchone()
            return self._reading(row) if row else None

    def reading_by_request(self, owner, request_key):
        """The reading stored under one request key, with the
        confirmation that authorized it (batch 2b item 9): a replay
        is only the same request when the presented confirmation
        matches the stored one, so the API layer can reject a key
        reused for a different reading instead of answering with
        the first reading's outcome."""
        with self.transaction() as db:
            row = db.execute("SELECT * FROM app_readings WHERE owner=? AND request_key=?",
                             (owner, request_key)).fetchone()
            if row is None:
                # Durable alias binding (Astra round-5 N11): a
                # request key that was answered with an earlier
                # unresolved reading for the same scan is bound to
                # that reading, so its replay — even after the
                # reading completed or failed — returns the SAME
                # outcome instead of creating a second purchase.
                alias = db.execute(
                    "SELECT reading_id, confirmation_id FROM "
                    "app_reading_request_aliases WHERE owner=? AND request_key=?",
                    (owner, request_key)).fetchone()
                if alias is None:
                    return None
                row = db.execute("SELECT * FROM app_readings WHERE id=?",
                                 (alias["reading_id"],)).fetchone()
                if row is None:
                    return None
                view = self._reading(row)
                view["confirmation_id"] = alias["confirmation_id"]
                return view
            view = self._reading(row)
            consent = db.execute(
                "SELECT confirmation_id FROM app_reading_consents WHERE reading_id=?",
                (row["id"],)).fetchone()
            view["confirmation_id"] = consent["confirmation_id"] if consent else None
            return view

    @staticmethod
    def _reading_route_player(db, reading_id):
        row = db.execute("SELECT player_id FROM app_reading_routes WHERE reading_id=?",
                         (reading_id,)).fetchone()
        return row["player_id"] if row is not None else None

    # The one-unresolved-purchase predicate (Astra round-4 F3,
    # round-5 F3): a 'reserved' row, or a 'failed' row whose
    # routed release has not landed yet.
    _UNRESOLVED_READING_SQL = (
        "SELECT * FROM app_readings WHERE owner=? AND scan_id=? "
        "AND id<>? AND (state='reserved' OR (state='failed' AND "
        "EXISTS (SELECT 1 FROM dplanet_reservation_intents "
        "WHERE op_id=app_readings.id AND state='release_pending'))) "
        "ORDER BY created_at LIMIT 1")

    @staticmethod
    def _compensate_outstanding_intent(db, owner, request_key,
                                       answered_reading_id, now):
        """Queue compensation for a landed reserve recorded
        under this request key whose reading was never created
        (Astra round-8 F3): the pre-check answers with another
        reading, and the caller's own earlier attempt — whose
        process died between reserving and creating — must not
        strand its hold just because the answer arrived through
        the pre-check rather than the creation funnel."""
        intent = db.execute(
            "SELECT * FROM dplanet_reservation_intents "
            "WHERE owner=? AND request_key=? AND kind='reading'",
            (owner, request_key)).fetchone()
        if intent is None or intent["op_id"] == answered_reading_id:
            return
        if intent["state"] not in ("intent", "reserved", "uncertain"):
            return
        db.execute(
            "UPDATE dplanet_reservation_intents "
            "SET state='release_pending', updated_at=? WHERE op_id=?",
            (now, intent["op_id"]))
        db.execute(
            "INSERT OR IGNORE INTO dplanet_pending_ops "
            "VALUES (?,?,?,?,?,0,NULL,?,?)",
            (f"si:{intent['op_id']}:release", intent["op_id"],
             intent["player_id"], "release", intent["amount"],
             now, now))

    def _preexisting_reading_answer(self, owner, confirmation_id, request_key):
        """The unresolved reading already owning this
        confirmation's scan, bound to this request key — or None.
        Used by the routed path BEFORE any remote reservation, so
        the common second-purchase case never touches D-Planet at
        all (Astra round-5 F3); the creation funnel repeats the
        same checks inside its transaction as the race backstop.
        Binding validation matches the funnel's (N12)."""
        with self.transaction() as db:
            confirmation = db.execute(
                "SELECT scan_id FROM app_reading_confirmations "
                "WHERE id=? AND owner=?", (confirmation_id, owner)).fetchone()
            if confirmation is None:
                return None
            # An existing PRIMARY binding resolves first (Astra
            # round-6 N14): if this request key already created a
            # reading, that reading is the answer — never another
            # scan-mate the unresolved check might pick.
            primary = db.execute(
                "SELECT * FROM app_readings WHERE owner=? AND request_key=?",
                (owner, request_key)).fetchone()
            if primary is not None:
                consent = db.execute(
                    "SELECT confirmation_id FROM app_reading_consents "
                    "WHERE reading_id=?", (primary["id"],)).fetchone()
                if consent is not None \
                        and consent["confirmation_id"] != confirmation_id:
                    raise ReadingChargeError("request_key_conflict")
                view = self._reading(primary)
                view["created"] = False
                view["preexisting"] = True
                return view
            bound = db.execute(
                "SELECT confirmation_id FROM app_reading_consents "
                "WHERE reading_id IN (SELECT id FROM app_readings "
                "WHERE owner=? AND request_key=?)",
                (owner, request_key)).fetchone()
            if bound is not None \
                    and bound["confirmation_id"] != confirmation_id:
                raise ReadingChargeError("request_key_conflict")
            alias = db.execute(
                "SELECT reading_id, confirmation_id FROM "
                "app_reading_request_aliases WHERE owner=? AND request_key=?",
                (owner, request_key)).fetchone()
            if alias is not None:
                if alias["confirmation_id"] != confirmation_id:
                    raise ReadingChargeError("request_key_conflict")
                row = db.execute("SELECT * FROM app_readings WHERE id=?",
                                 (alias["reading_id"],)).fetchone()
                if row is None:
                    return None
                self._compensate_outstanding_intent(
                    db, owner, request_key, row["id"], time.time())
                view = self._reading(row)
                view["created"] = False
                view["preexisting"] = True
                return view
            claim = db.execute(
                "SELECT reading_id FROM app_reading_scan_claims "
                "WHERE owner=? AND scan_id=?",
                (owner, confirmation["scan_id"])).fetchone()
            if claim is not None:
                row = db.execute("SELECT * FROM app_readings WHERE id=?",
                                 (claim["reading_id"],)).fetchone()
                if row is not None:
                    db.execute(
                        "INSERT OR IGNORE INTO app_reading_request_aliases "
                        "VALUES (?,?,?,?,?)",
                        (owner, request_key, confirmation_id, row["id"],
                         time.time()))
                    self._compensate_outstanding_intent(
                        db, owner, request_key, row["id"], time.time())
                    view = self._reading(row)
                    view["created"] = False
                    view["preexisting"] = True
                    return view
            row = db.execute(self._UNRESOLVED_READING_SQL,
                             (owner, confirmation["scan_id"],
                              "")).fetchone()
            if row is None:
                return None
            db.execute(
                "INSERT OR IGNORE INTO app_reading_request_aliases "
                "VALUES (?,?,?,?,?)",
                (owner, request_key, confirmation_id, row["id"],
                 time.time()))
            self._compensate_outstanding_intent(
                db, owner, request_key, row["id"], time.time())
            view = self._reading(row)
            view["created"] = False
            view["preexisting"] = True
            return view

    def compensate_orphan_intent_for_key(self, owner, request_key):
        """Recovery for a state the pre-fix implementation
        could leave behind (Astra round-9 F3): a request key
        that already resolves — as a reading or an alias — while
        an earlier attempt's intent under the same key still
        holds a landed reservation with no reading row and no
        queued release. API replay paths call this after identity
        validation; it fences the intent and queues its release
        in one transaction. Delivery is DEFERRED to the
        maintenance retry loop (Astra round-10 F3/F8(a)): the
        round-10 version delivered synchronously before the
        replay answered, which put a remote wait — however
        bounded — on a path that only ever needed the durable
        fence. The queued op is delivered by
        retry_pending_dplanet_ops like every other pending op,
        and delivery still re-validates ownership
        (_release_warranted) before releasing anything. A no-op
        when no such orphan exists."""
        now = time.time()
        with self.transaction() as db:
            intent = db.execute(
                "SELECT * FROM dplanet_reservation_intents "
                "WHERE owner=? AND request_key=? AND kind='reading'",
                (owner, request_key)).fetchone()
            if intent is None:
                return False
            if intent["state"] not in ("intent", "reserved", "uncertain"):
                return False
            if db.execute("SELECT 1 FROM app_readings WHERE id=?",
                          (intent["op_id"],)).fetchone():
                return False
            claimed = db.execute(
                "UPDATE dplanet_reservation_intents "
                "SET state='release_pending', updated_at=? "
                "WHERE op_id=? AND state IN ('intent','reserved','uncertain')",
                (now, intent["op_id"])).rowcount
            if not claimed:
                return False
            db.execute(
                "INSERT OR IGNORE INTO dplanet_pending_ops "
                "VALUES (?,?,?,?,?,0,NULL,?,?)",
                (f"si:{intent['op_id']}:release", intent["op_id"],
                 intent["player_id"], "release", intent["amount"],
                 now, now))
        return True

    def reserve_reading(self, owner, reading_id, confirmation_id, request_key, *,
                        credits, price_version, consent):
        """Reserve the reading charge (design §4.1 step 2).

        One transaction validates the parked confirmation (live,
        unused, quote unchanged), enforces the daily cap, takes the
        credits (guarded UPDATE; routed accounts reserve remotely
        first and never touch the SI balance column), writes the
        reserve ledger row, the reading row, the consent record,
        and consumes the confirmation. Any failure before the
        remote reservation writes nothing; a failure after it is
        compensated with a release, like _submit_routed.
        """
        if type(credits) is not int or credits < 1:
            raise ValueError("Invalid reading price")
        link = None
        if self._dplanet_routing_enabled:
            with self.transaction() as db:
                link = self._routing_link(db, owner)
        if link is not None:
            client = self._dplanet_client
            if client is None:
                raise ValueError("D-Planet credit routing is enabled but not configured")
            # An unresolved reading already owns this scan: answer
            # with it BEFORE recording an intent or reserving
            # remotely (Astra round-5 F3) — the second purchase
            # never becomes a second remote hold in the common
            # case. The creation funnel re-checks under its
            # transaction for the simultaneous-first-submission
            # race, compensating its own reserve there.
            preexisting = self._preexisting_reading_answer(
                owner, confirmation_id, request_key)
            if preexisting is not None:
                return preexisting
            player_id = link["dplanet_player_id"]
            # Durable intent before the remote call (batch 2b item 1,
            # the reading twin of _submit_routed): an ambiguous
            # reserve leaves an 'uncertain' record, and a retry with
            # the same request key adopts the recorded reading id —
            # the caller-generated id of the first attempt — instead
            # of reserving again under a fresh one. The fingerprint
            # binds the intent to this confirmation and price, so a
            # key reused for a different reading is a conflict.
            fingerprint = hashlib.sha256(
                f"{confirmation_id}:{credits}:{price_version}".encode()).hexdigest()
            reading_id = self._record_reservation_intent(
                reading_id, owner=owner, request_key=request_key, kind="reading",
                fingerprint=fingerprint, player_id=player_id, amount=credits,
                conflict=ReadingChargeError("request_key_conflict"),
                pending=ReadingChargeError("reading_unavailable"))
            existing = self.reading_by_request(owner, request_key)
            if existing is not None:
                # Identity on EVERY existing-row path (Astra round-1
                # F6): the stored row answers only the request that
                # created it. The fingerprint binds this call's
                # confirmation, but a row created by the LOCAL path
                # (no intent exists for it — e.g. the account linked
                # between two attempts with the same key) is checked
                # against its recorded consent here instead of being
                # handed out for another scan's confirmation.
                if existing["confirmation_id"] != confirmation_id:
                    raise ReadingChargeError("request_key_conflict")
                existing["created"] = False
                return existing
            try:
                client.reserve(player_id, reading_id, credits)
            except DPlanetError as error:
                if error.status == 402:
                    self._set_reservation_intent_state(reading_id, "rejected")
                    raise ReadingChargeError("reading_insufficient_balance") from None
                # Ambiguous outcome (Astra round-1 F1/F3): the hold
                # may exist remotely under this reading id. The local
                # row is created anyway, in 'reserved' state, so the
                # charge has a durable, recoverable identity: the
                # caller answers 'reading_pending' (the client polls
                # the row under the SAME request key), and orphan
                # reconciliation releases the row — and with it the
                # possible remote hold — if it never settles. Only
                # when even the local row cannot be created is the
                # possibly-held reservation compensated and the plain
                # unavailable error reported.
                marked = self._set_reservation_intent_state(reading_id, "uncertain")
                if not marked:
                    with self.transaction() as db:
                        exists = db.execute(
                            "SELECT 1 FROM dplanet_reservation_intents "
                            "WHERE op_id=?", (reading_id,)).fetchone()
                    if not exists:
                        self._compensate_reserve(player_id, reading_id, credits)
                        raise ReadingChargeError("reading_unavailable") from None
                try:
                    local = self._reserve_reading_local(
                        owner, reading_id, confirmation_id, request_key,
                        credits=credits, price_version=price_version,
                        consent=consent,
                        route={"player_id": player_id, "link_id": link["link_id"]})
                except BaseException:
                    self._compensate_reserve(player_id, reading_id, credits)
                    raise ReadingChargeError("reading_unavailable") from None
                if local.get("preexisting"):
                    # Another attempt's reading already owns this
                    # scan: our possibly-landed reserve is released
                    # and the existing purchase is the answer.
                    self._compensate_reserve(player_id, reading_id, credits)
                    return local
                raise ReadingChargeError("reading_pending") from None
            self._set_reservation_intent_state(reading_id, "reserved")
            try:
                reserved = self._reserve_reading_local(
                    owner, reading_id, confirmation_id, request_key,
                    credits=credits, price_version=price_version, consent=consent,
                    route={"player_id": player_id, "link_id": link["link_id"]})
            except BaseException:
                self._compensate_reserve(player_id, reading_id, credits)
                raise
            if reserved.get("preexisting"):
                self._compensate_reserve(player_id, reading_id, credits)
                return reserved
            self._set_reservation_intent_state(reading_id, "consumed")
            reserved["created"] = True
            return reserved
        reserved = self._reserve_reading_local(
            owner, reading_id, confirmation_id, request_key,
            credits=credits, price_version=price_version, consent=consent, route=None)
        if not reserved.get("preexisting"):
            reserved["created"] = True
        return reserved

    def _reserve_reading_local(self, owner, reading_id, confirmation_id, request_key,
                               *, credits, price_version, consent, route):
        now = time.time()
        day = datetime.now(timezone.utc).date().isoformat()
        with self.transaction() as db:
            if not db.execute("SELECT 1 FROM accounts WHERE id=?", (owner,)).fetchone():
                raise KeyError("Account not found")
            confirmation = db.execute(
                "SELECT * FROM app_reading_confirmations WHERE id=? AND owner=?",
                (confirmation_id, owner)).fetchone()
            if confirmation is None:
                raise ReadingChargeError("confirmation_not_found")
            if confirmation["used_reading"] is not None:
                raise ReadingChargeError("confirmation_already_used")
            if now > confirmation["expires_at"]:
                raise ReadingChargeError("confirmation_expired")
            if confirmation["credits"] != credits \
                    or confirmation["price_version"] != price_version:
                raise ReadingChargeError("quote_changed")
            # Request-key binding, validated INSIDE the creation
            # transaction (Astra round-5 N12): a key already bound
            # — as a reading's own key or as an alias — to a
            # DIFFERENT confirmation is a conflict, never an
            # answer; a key bound to THIS confirmation replays its
            # bound reading. Checking here, not only in the API's
            # pre-read, closes the race where a concurrent request
            # binds the key between the pre-read and creation.
            bound = db.execute(
                "SELECT confirmation_id FROM app_reading_consents "
                "WHERE reading_id IN (SELECT id FROM app_readings "
                "WHERE owner=? AND request_key=?)",
                (owner, request_key)).fetchone()
            if bound is not None and bound["confirmation_id"] != confirmation_id:
                raise ReadingChargeError("request_key_conflict")
            alias = db.execute(
                "SELECT reading_id, confirmation_id FROM "
                "app_reading_request_aliases WHERE owner=? AND request_key=?",
                (owner, request_key)).fetchone()
            if alias is not None:
                if alias["confirmation_id"] != confirmation_id:
                    raise ReadingChargeError("request_key_conflict")
                aliased = db.execute("SELECT * FROM app_readings WHERE id=?",
                                     (alias["reading_id"],)).fetchone()
                if aliased is not None:
                    if route is not None:
                        # An alias answer on a routed path means
                        # THIS caller's reserve landed for a scan
                        # another reading owns: queue its
                        # compensation in this transaction too
                        # (Astra round-7 F3 — the alias return
                        # previously bypassed the compensation
                        # block entirely, leaving a crash window
                        # before the caller's own compensation).
                        self._queue_routed_compensation(
                            db, route, reading_id, credits, now)
                    view = self._reading(aliased)
                    view["created"] = False
                    view["preexisting"] = True
                    return view
            # One unresolved purchase per (owner, scan) (Astra
            # round-4 F3/N6): two tabs — or a reloaded client that
            # lost its pending record — can hold DIFFERENT
            # confirmations and request keys for the same scan, and
            # request-key idempotency alone cannot stop a second
            # reservation. If a reading for this scan is still
            # unresolved, it IS the purchase: answer with it and
            # create nothing — no charge, and this confirmation
            # stays unused. "Unresolved" covers a 'reserved' row
            # and a 'failed' row whose routed release is still
            # 'release_pending' (Astra round-5 F3: the financial
            # operation is not over until the release lands). The
            # answer is bound durably to this request key (N11),
            # so a lost response replays THIS outcome even after
            # the reading settles. The check sits inside the
            # creation transaction, so concurrent creators
            # serialize on it.
            # The durable scan claim (Astra round-6 F3): while a
            # claim stands for another reading, that reading is
            # the scan's one unresolved purchase — however the
            # attempts raced, and even if its local row already
            # terminated while a financial op is outstanding.
            claim = db.execute(
                "SELECT reading_id FROM app_reading_scan_claims "
                "WHERE owner=? AND scan_id=?",
                (owner, confirmation["scan_id"])).fetchone()
            preexisting = None
            if claim is not None and claim["reading_id"] != reading_id:
                preexisting = db.execute(
                    "SELECT * FROM app_readings WHERE id=?",
                    (claim["reading_id"],)).fetchone()
            if preexisting is None:
                preexisting = db.execute(
                    self._UNRESOLVED_READING_SQL,
                    (owner, confirmation["scan_id"], reading_id)).fetchone()
            if preexisting is not None:
                db.execute(
                    "INSERT OR IGNORE INTO app_reading_request_aliases "
                    "VALUES (?,?,?,?,?)",
                    (owner, request_key, confirmation_id,
                     preexisting["id"], now))
                if route is not None:
                    # This caller's remote reserve already landed:
                    # queue its compensation IN THIS TRANSACTION
                    # (Astra round-6 F3 crash window) — the claim
                    # and the release op commit atomically with
                    # the alias decision, so no crash can leave
                    # the hold without a deliverable release.
                    self._queue_routed_compensation(
                        db, route, reading_id, credits, now)
                view = self._reading(preexisting)
                view["created"] = False
                view["preexisting"] = True
                return view
            cap = self.limit_policy.reading_daily_cap_per_account
            if cap is not None:
                used = db.execute(
                    "SELECT COUNT(*) FROM app_readings WHERE owner=? AND day=? "
                    "AND state IN ('reserved','completed')", (owner, day)).fetchone()[0]
                if used >= cap:
                    raise ReadingChargeError("reading_daily_cap")
            if route is None:
                updated = db.execute(
                    "UPDATE accounts SET balance=balance-? WHERE id=? AND balance>=?",
                    (credits, owner, credits))
                if updated.rowcount != 1:
                    raise ReadingChargeError("reading_insufficient_balance")
            else:
                # Routed creation validates the compensation fence
                # inside this transaction (Astra round-2 F2); the
                # local path has no reservation intent to fence.
                self._assert_no_compensation_fence(
                    db, reading_id, ReadingChargeError("reading_unavailable"))
            db.execute(
                "INSERT INTO app_readings(id,owner,scan_id,request_key,state,credits,"
                "price_version,route,day,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (reading_id, owner, confirmation["scan_id"], request_key, "reserved",
                 credits, price_version, "native-reading", day, now))
            # Acquire the scan claim atomically with the reading
            # row (Astra round-6 F3).
            db.execute(
                "INSERT OR IGNORE INTO app_reading_scan_claims "
                "VALUES (?,?,?,?)",
                (owner, confirmation["scan_id"], reading_id, now))
            db.execute("INSERT INTO ledger(owner,job,kind,amount) VALUES (?,?,?,?)",
                       (owner, None, "reading_reserve", -credits))
            db.execute("UPDATE app_reading_confirmations SET used_reading=? WHERE id=?",
                       (reading_id, confirmation_id))
            db.execute(
                "INSERT INTO app_reading_consents VALUES (?,?,?,?,?,?)",
                (reading_id, owner, confirmation_id, credits,
                 json.dumps(consent, sort_keys=True, allow_nan=False), now))
            if route is not None:
                db.execute("INSERT INTO app_reading_routes VALUES (?,?,?)",
                           (reading_id, route["player_id"], route["link_id"]))
            row = db.execute("SELECT * FROM app_readings WHERE id=?",
                             (reading_id,)).fetchone()
            return self._reading(row)

    def settle_reading(self, owner, reading_id, *, text, model, prompt_tokens,
                       completion_tokens, provider_cost_microusd, deliver=True):
        """Settle a reserved reading (design §4.1 step 4): the
        validated text and the usage counters are stored in the
        same transaction as the settle ledger row, so a charged
        reading always has its text. Routed readings queue the
        D-Planet settle in the same transaction and deliver it
        after commit, like job settlement."""
        with self.transaction() as db:
            row = db.execute("SELECT * FROM app_readings WHERE id=? AND owner=?",
                             (reading_id, owner)).fetchone()
            if row is None:
                raise KeyError("Reading not found")
            if row["state"] != "reserved":
                raise ValueError("Reading is not reserved")
            player_id = self._reading_route_player(db, reading_id)
            db.execute(
                "UPDATE app_readings SET state='completed', text=?, model=?, "
                "prompt_tokens=?, completion_tokens=?, provider_cost_microusd=?, "
                "finished_at=? WHERE id=?",
                (text, model, prompt_tokens, completion_tokens,
                 provider_cost_microusd, time.time(), reading_id))
            # A settled reading closes its reservation intent, so
            # the uncertain list only ever names outcomes that still
            # need reconciliation (batch 2b item 1 / Astra F3).
            db.execute(
                "UPDATE dplanet_reservation_intents SET state='consumed', "
                "updated_at=? WHERE op_id=? AND state IN ('intent','reserved','uncertain')",
                (time.time(), reading_id))
            db.execute("INSERT INTO ledger(owner,job,kind,amount) VALUES (?,?,?,0)",
                       (owner, None, "reading_settle"))
            if player_id is not None:
                self._queue_dplanet_op(db, reading_id, player_id, "settle", None)
            else:
                # Local readings have no outstanding financial op:
                # the scan claim ends with settlement (or
                # transfers). Routed claims end at delivery.
                self._release_scan_claim(db, reading_id)
        if deliver:
            self._deliver_dplanet_ops(reading_id)
        return self.get_reading(owner, reading_id)

    def release_reading(self, owner, reading_id, *, error_code, deliver=True):
        """Release a reserved reading in full (design §4.1 step 5):
        the user is charged nothing. Idempotent on a terminal row.
        Routed readings refund through the pending-ops outbox."""
        with self.transaction() as db:
            row = db.execute("SELECT * FROM app_readings WHERE id=? AND owner=?",
                             (reading_id, owner)).fetchone()
            if row is None:
                raise KeyError("Reading not found")
            if row["state"] != "reserved":
                return self._reading(row)
            player_id = self._reading_route_player(db, reading_id)
            db.execute(
                "UPDATE app_readings SET state='failed', error_code=?, finished_at=? "
                "WHERE id=?", (error_code, time.time(), reading_id))
            # The intent follows the remote release's delivery, not
            # the local transition (Astra round-2 F3): a routed
            # release stays 'release_pending' — visible on the
            # uncertain list — until the queued op actually lands
            # (the delivery path closes it to 'released'); a local
            # release has no remote leg and closes immediately.
            # The from-set includes 'consumed' (Astra round-3 F3):
            # a successful routed reserve marks its intent consumed
            # when the reading row is created, so a generation
            # failure's release starts from 'consumed' — without it
            # the intent stayed 'consumed' forever, invisible to
            # reconciliation even while its release was pending.
            db.execute(
                "UPDATE dplanet_reservation_intents SET state=?, "
                "updated_at=? WHERE op_id=? AND state IN ('intent','reserved','uncertain','consumed')",
                ("release_pending" if player_id is not None else "released",
                 time.time(), reading_id))
            db.execute("INSERT INTO ledger(owner,job,kind,amount) VALUES (?,?,?,?)",
                       (owner, None, "reading_release", row["credits"]))
            if player_id is None:
                db.execute("UPDATE accounts SET balance=balance+? WHERE id=?",
                           (row["credits"], owner))
                self._release_scan_claim(db, reading_id)
            else:
                self._queue_dplanet_op(db, reading_id, player_id, "release",
                                       row["credits"])
            settled = db.execute("SELECT * FROM app_readings WHERE id=?",
                                 (reading_id,)).fetchone()
            view = self._reading(settled)
        if deliver:
            # Delivery is best-effort and durably queued; callers
            # on a deadline (the app API's generation path, Astra
            # round-8 F8) pass deliver=False so a slow D-Planet
            # cannot hold the request past its budget — the
            # maintenance retry loop owns delivery then.
            self._deliver_dplanet_ops(reading_id)
        return view

    def reconcile_orphan_readings(self, *, now=None, max_age_seconds=600):
        """Release reservations whose process died mid-generation
        (design §4.2): a 'reserved' reading older than the bound
        with no result is failed as 'interrupted' and refunded in
        full — the safe side is always "not delivered, not charged".
        Picked up by the maintenance loop like expire_scans."""
        moment = time.time() if now is None else now
        with self.transaction() as db:
            rows = db.execute(
                "SELECT id, owner FROM app_readings WHERE state='reserved' "
                "AND created_at<=?", (moment - max_age_seconds,)).fetchall()
        released = 0
        for row in rows:
            self.release_reading(row["owner"], row["id"], error_code="interrupted")
            released += 1
        return released

    def recent_app_sessions(self, owner, *, limit=20):
        """S8 history with readings mixed in (design §2.4): the
        owner's latest app jobs and readings, newest first. A
        reading's shown cost is its price once completed and 0
        otherwise (a failed reading charged nothing)."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid history limit")
        sessions = [{"kind": "job", "job_id": row["job_id"],
                     "operation": row["operation"], "state": row["state"],
                     "cost": row["cost"], "created_at": row["created_at"],
                     "targets": row["targets"], "intention": row["intention"],
                     "self_love_joy": row["self_love_joy"]}
                    for row in self.recent_app_jobs(owner, limit=limit)]
        with self.transaction() as db:
            rows = db.execute(
                "SELECT id, scan_id, state, credits, created_at FROM app_readings "
                "WHERE owner=? ORDER BY created_at DESC, id DESC LIMIT ?",
                (owner, limit)).fetchall()
        sessions += [{"kind": "reading", "reading_id": row["id"],
                      "scan_id": row["scan_id"], "operation": "reading",
                      "state": row["state"],
                      "cost": row["credits"] if row["state"] == "completed" else 0,
                      "created_at": row["created_at"]} for row in rows]
        sessions.sort(key=lambda row: row["created_at"], reverse=True)
        return sessions[:limit]

    @staticmethod
    def _expire_reading_texts(db, scan_id):
        """Reading texts live and die with their parent scan's
        content (design §3.5): when the scan content is removed,
        the generated text goes with it; the row and its charge
        record stay for audit."""
        db.execute("UPDATE app_readings SET text=NULL WHERE scan_id=? "
                   "AND text IS NOT NULL", (scan_id,))

    def record_account_link(self, owner, player_id, link_id, *, defer_transfer=False):
        """Record a finalized D-Planet link; the binding is never rewritten.

        Call only after D-Planet finalize succeeded for this exact pair.
        Re-linking the same pair after an unlink refreshes the owner's row;
        either side actively bound to a different partner is a conflict,
        and changing partners always goes through unlink first (§6.3).

        With defer_transfer=True the link-time balance transfer is
        marked deferred IN THE SAME TRANSACTION (batch 2b item 3,
        Astra round-1 F4): the link and its deferral marker commit
        atomically, so an activation sweep can never miss a link
        whose confirmation crashed between the two writes.
        """
        if not isinstance(player_id, str) or not player_id or len(player_id) > 200:
            raise ValueError("Linked player identity required")
        if not isinstance(link_id, str) or not link_id or len(link_id) > 200:
            raise ValueError("Link identity required")
        with self.transaction() as db:
            if not db.execute("SELECT 1 FROM accounts WHERE id=?", (owner,)).fetchone():
                raise KeyError("Account not found")
            by_player = db.execute(
                "SELECT si_owner FROM account_links WHERE dplanet_player_id=? AND status='active'",
                (player_id,)).fetchone()
            if by_player is not None and by_player["si_owner"] != owner:
                raise ValueError("Player already linked to a different account")
            existing = db.execute("SELECT * FROM account_links WHERE si_owner=?", (owner,)).fetchone()
            now = time.time()
            if existing is not None and existing["status"] == "active":
                if existing["dplanet_player_id"] != player_id:
                    raise ValueError("Account already linked to a different player")
                if existing["link_id"] != link_id:
                    raise ValueError("Conflicting link identity")
                if defer_transfer:
                    db.execute("INSERT OR IGNORE INTO link_transfer_deferrals "
                               "VALUES (?,?,?)", (link_id, owner, now))
                return self._account_link(existing)
            if existing is not None:
                db.execute("UPDATE account_links SET dplanet_player_id=?, link_id=?, "
                           "linked_at=?, unlinked_at=NULL, status='active' WHERE si_owner=?",
                           (player_id, link_id, now, owner))
            else:
                db.execute("INSERT INTO account_links VALUES (?,?,?,?,NULL,'active')",
                           (owner, player_id, link_id, now))
            if defer_transfer:
                db.execute("INSERT OR IGNORE INTO link_transfer_deferrals "
                           "VALUES (?,?,?)", (link_id, owner, now))
            return self._account_link(
                db.execute("SELECT * FROM account_links WHERE si_owner=?", (owner,)).fetchone())

    def mark_account_unlinked(self, owner):
        """Flip the owner's active link to 'unlinked'; the row stays for audit."""
        with self.transaction() as db:
            row = db.execute("SELECT * FROM account_links WHERE si_owner=? AND status='active'",
                             (owner,)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE account_links SET status='unlinked', unlinked_at=? WHERE si_owner=?",
                       (time.time(), owner))
            db.execute("DELETE FROM link_transfer_deferrals WHERE link_id=?",
                       (row["link_id"],))
            return self._account_link(
                db.execute("SELECT * FROM account_links WHERE si_owner=?", (owner,)).fetchone())

    def has_unfinished_execution(self, owner):
        """Running/unknown jobs block unlinking (§6.4): their outcome is not
        settled, so the link must stay until they reach a terminal state."""
        with self.transaction() as db:
            return bool(db.execute(
                "SELECT 1 FROM jobs WHERE owner=? AND state IN ('running','unknown') LIMIT 1",
                (owner,)).fetchone())

    # --- Link-time balance transfer (design §4.4 step 2) ---

    def link_transfer(self, link_id):
        """The recorded balance transfer for one link, if any."""
        with self.transaction() as db:
            row = db.execute("SELECT * FROM link_transfers WHERE link_id=?",
                             (link_id,)).fetchone()
            if row is None:
                return None
            return {"link_id": row["link_id"], "si_owner": row["si_owner"],
                    "player_id": row["player_id"], "amount": row["amount"],
                    "state": row["state"]}

    def transfer_balance_to_dplanet(self, owner, client=None, *, expected_link_id=None):
        """Move the owner's whole SI balance into the linked D-Planet wallet.

        With expected_link_id, the transfer runs only if the owner's
        ACTIVE link is still that exact link (Astra round-1 F4): the
        deferred sweep validates the marked link and must never move
        the balance to a different link recorded in the meantime.

        Called once per link, right after the link is recorded (§4.4).
        Deduct-first ordering, so a failure can neither duplicate nor
        silently lose credits: one transaction zeroes the SI balance,
        writes the transfer_out_to_dplanet ledger row, records the
        transfer as pending and queues the remote op under the same
        op_key D-Planet records (si-transfer:<link_id>, idempotent
        there). Delivery is attempted immediately and retried through
        the pending-op mechanism (retry_pending_dplanet_ops /
        Maintenance); while delivery is outstanding the transfer is
        visible in pending_dplanet_ops for reconciliation. A repeat
        call for the same link reports the recorded transfer instead
        of moving money twice; a zero balance completes without any
        remote call. One transfer per link_id is final — credits that
        reach a linked account's SI side afterwards are an operations
        matter, not a second transfer.
        """
        client = self._dplanet_client if client is None else client
        if client is None:
            raise ValueError("D-Planet client is not configured")
        with self.transaction() as db:
            link = db.execute("SELECT * FROM account_links WHERE si_owner=? AND status='active'",
                              (owner,)).fetchone()
            if link is None:
                raise ValueError("Account is not linked to D-Planet")
            link = self._account_link(link)
            if expected_link_id is not None and link["link_id"] != expected_link_id:
                raise ValueError("Active link changed; transfer aborted")
            link_id, player_id = link["link_id"], link["dplanet_player_id"]
            record = db.execute("SELECT * FROM link_transfers WHERE link_id=?",
                                (link_id,)).fetchone()
            if record is not None and record["state"] == "done":
                return {"link_id": link_id, "player_id": player_id,
                        "amount": record["amount"],
                        "status": "no_balance" if record["amount"] == 0 else "done",
                        "already_recorded": True}
            if record is None:
                amount = db.execute("SELECT balance FROM accounts WHERE id=?",
                                    (owner,)).fetchone()["balance"]
                now = time.time()
                if amount == 0:
                    db.execute("INSERT INTO link_transfers VALUES (?,?,?,?,'done',?,?)",
                               (link_id, owner, player_id, 0, now, now))
                    return {"link_id": link_id, "player_id": player_id,
                            "amount": 0, "status": "no_balance"}
                db.execute("UPDATE accounts SET balance=0 WHERE id=?", (owner,))
                db.execute("INSERT INTO ledger(owner,kind,amount) VALUES (?,?,?)",
                           (owner, "transfer_out_to_dplanet", -amount))
                db.execute("INSERT INTO link_transfers VALUES (?,?,?,?,'pending',?,?)",
                           (link_id, owner, player_id, amount, now, now))
            else:
                amount = record["amount"]
            db.execute("INSERT OR IGNORE INTO dplanet_pending_ops VALUES (?,?,?,?,?,0,NULL,?,?)",
                       (f"si-transfer:{link_id}", link_id, player_id, "transfer",
                        amount, time.time(), time.time()))
        self._deliver_dplanet_ops(link_id, client=client)
        with self.transaction() as db:
            record = db.execute("SELECT state FROM link_transfers WHERE link_id=?",
                                (link_id,)).fetchone()
            queued = db.execute("SELECT last_error FROM dplanet_pending_ops WHERE op_key=?",
                                (f"si-transfer:{link_id}",)).fetchone()
        result = {"link_id": link_id, "player_id": player_id, "amount": amount,
                  "status": "done" if record["state"] == "done" else "pending"}
        if queued is not None and queued["last_error"]:
            result["error"] = queued["last_error"]
        return result

    def run_deferred_link_transfers(self):
        """Run balance transfers explicitly deferred while routing was
        off (batch 2b item 3), once routing is active. A no-op while
        routing is disabled or no client is configured, so the
        maintenance loop can call it unconditionally. Only links with
        a deferral marker are touched: a link recorded without one
        (any other path, any earlier era) keeps its SI balance — the
        absence of a transfer record alone never authorizes a move.
        The transfer is the same idempotent per-link operation
        confirm_link runs; once its record exists the marker is
        cleared and delivery/retry belongs to the pending-op
        mechanism. A failure before recording keeps the marker for
        the next pass, never silently dropped.
        """
        if not self._dplanet_routing_enabled or self._dplanet_client is None:
            return {"transferred": 0}
        with self.transaction() as db:
            rows = db.execute(
                "SELECT link_id, si_owner FROM link_transfer_deferrals").fetchall()
        transferred = 0
        for row in rows:
            link_id, owner = row["link_id"], row["si_owner"]
            with self.transaction() as db:
                link = db.execute(
                    "SELECT status FROM account_links WHERE link_id=?",
                    (link_id,)).fetchone()
                recorded = db.execute(
                    "SELECT 1 FROM link_transfers WHERE link_id=?",
                    (link_id,)).fetchone()
                if link is None or link["status"] != "active" or recorded:
                    db.execute("DELETE FROM link_transfer_deferrals "
                               "WHERE link_id=?", (link_id,))
                    continue
            try:
                # The transfer itself re-validates the exact link
                # inside its transaction (Astra round-1 F4): if the
                # owner re-linked since the marker was validated, it
                # aborts instead of paying the new link's player.
                self.transfer_balance_to_dplanet(owner, expected_link_id=link_id)
            except Exception:
                continue  # Marker stays; retried on the next pass.
            with self.transaction() as db:
                db.execute("DELETE FROM link_transfer_deferrals "
                           "WHERE link_id=?", (link_id,))
            transferred += 1
        return {"transferred": transferred}

    def reflect_remote_unlink(self, *, link_id, si_owner=None, player_id=None):
        """Reflect a D-Planet-initiated unlink onto account_links (§6.4).

        The row is kept for audit and re-delivery is a no-op success.
        An unknown link_id is an honest KeyError, and a notification
        naming a different owner or player than the recorded link is
        rejected instead of unlinking someone else's pairing.
        """
        if not isinstance(link_id, str) or not link_id or len(link_id) > 200:
            raise ValueError("Link identity required")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM account_links WHERE link_id=?",
                             (link_id,)).fetchone()
            if row is None:
                raise KeyError("Link not found")
            if si_owner is not None and row["si_owner"] != si_owner:
                raise ValueError("Link owner mismatch")
            if player_id is not None and row["dplanet_player_id"] != player_id:
                raise ValueError("Link player mismatch")
            if row["status"] == "unlinked":
                return self._account_link(row)
            db.execute("UPDATE account_links SET status='unlinked', unlinked_at=? "
                       "WHERE si_owner=?", (time.time(), row["si_owner"]))
            db.execute("DELETE FROM link_transfer_deferrals WHERE link_id=?",
                       (link_id,))
            return self._account_link(
                db.execute("SELECT * FROM account_links WHERE si_owner=?",
                           (row["si_owner"],)).fetchone())

    # --- Legacy-balance replacement measure (design §4.4 step 1, J3) ---
    # Operator-only: never exposed through MCP or any endpoint. The
    # production run additionally requires Asahi's explicit instruction
    # and the run procedure recorded in docs/implementation-notes.md.

    # --- Registration support (agent-carried up to the browser boundary) ---

    def invitation_code_status(self, owner, code):
        """Existence check for a registration invitation code.

        Validity only; the inviter's identity is never exposed. A code is
        not usable by its own owner (self-referral is rejected when the
        new account is provisioned), which a caller needs to know before
        the browser signup starts.
        """
        if not isinstance(code, str) or not 1 <= len(code) <= 128:
            return {"valid": False, "is_own_code": False}
        with self.transaction() as db:
            code_owner = self._code_owner(db, code)
            if code_owner is None:
                return {"valid": False, "is_own_code": False}
            return {"valid": True, "is_own_code": code_owner == owner}

    def registration_status(self, owner):
        """How this account came to exist and whether a referral attached.

        Read-only and owner-scoped. Referral codes cannot be added after
        registration, so 'referral_attached' is final once the account
        exists. onboarding_receipts is created by the onboarding
        coordinator, not by this store, so its absence is tolerated.
        """
        with self.transaction() as db:
            if not db.execute("SELECT 1 FROM accounts WHERE id=?", (owner,)).fetchone():
                raise KeyError("Account not found")
            has_receipts = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='onboarding_receipts'"
            ).fetchone()
            via_onboarding = bool(has_receipts and db.execute(
                "SELECT 1 FROM onboarding_receipts WHERE owner=?", (owner,)).fetchone())
            referral = db.execute("SELECT code FROM registration_referrals WHERE invitee=?",
                                  (owner,)).fetchone()
            return {
                "registered": True,
                "via_onboarding": via_onboarding,
                "migrated": bool(db.execute("SELECT 1 FROM migration_awards WHERE owner=?",
                                            (owner,)).fetchone()),
                "legacy_imported": bool(db.execute(
                    "SELECT 1 FROM legacy_identity_imports WHERE account_id=?", (owner,)).fetchone()),
                "referral_attached": bool(referral and referral["code"]),
                "invitation_code_issued": bool(db.execute(
                    "SELECT 1 FROM invitation_codes WHERE owner=?", (owner,)).fetchone()),
            }

    # --- Execution proof (owner-scoped evidence bundle) ---

    def public_device_id(self, owner, diode_serial_hash, *, at=None):
        # Scoped pseudonymous device identity for public proof
        # (plan v4 section 6): an HMAC over device x purpose
        # (execution-proof) x owner x month period, keyed by
        # AIQS_SI_DEVICE_ID_KEY (falling back to the profile salt).
        # The raw serial and its unsalted hash never leave the
        # server; the same device shows the same public id to the
        # same owner within a month and a different one across
        # owners, purposes, and months, so public ids cannot be
        # correlated across scopes. Without key material (dev
        # setups) the id is None rather than a weak derivation.
        if not self._diode_hash_valid(diode_serial_hash):
            return None
        key = os.environ.get(DEVICE_ID_KEY_ENV) or os.environ.get(PROFILE_SALT_ENV)
        if not isinstance(key, str) or not key:
            return None
        moment = datetime.fromtimestamp(time.time() if at is None else at,
                                        timezone.utc)
        scope = "aiqs-si:execution-proof:%s:%s:%s" % (
            owner, moment.strftime("%Y-%m"), diode_serial_hash)
        return hmac.new(key.encode(), scope.encode(),
                        hashlib.sha256).hexdigest()[:24]

    def execution_proof(self, owner, job):
        """Server-side execution evidence for one of the owner's jobs.

        Public proof schema (plan v4 section 6): the evidence chain —
        request digest, server-tracked progress, completion state,
        settlement ledger — plus a proof digest computed over THIS
        public bundle only. It deliberately omits the claiming
        worker's internal identity and any digest of the worker's
        full result, and the device is shown as a scoped pseudonymous
        public_device_id (see public_device_id), never as the raw
        serial or its unsalted hash. The server never verifies
        physical output. Another owner's job raises KeyError exactly
        like get().
        """
        with self.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=? AND owner=?", (job, owner)).fetchone()
            if row is None:
                raise KeyError("Job not found")
            payload = json.loads(row["payload"])
            result = json.loads(row["result"]) if row["result"] is not None else None
            if not isinstance(result, dict):
                result = None
            progress = db.execute("SELECT completed,stop_requested FROM execution_progress WHERE job=?",
                                  (job,)).fetchone()
            receipt = db.execute("SELECT state,digest FROM completion_receipts WHERE job=?",
                                 (job,)).fetchone()
            liveness = db.execute("SELECT seen_at,expired FROM execution_liveness WHERE job=?",
                                  (job,)).fetchone()
            resolution = db.execute(
                "SELECT completed,evidence_digest,resolved_at FROM execution_resolutions WHERE job=?",
                (job,)).fetchone()
            lifecycle = db.execute("SELECT finished_at FROM content_lifecycle WHERE job=?",
                                   (job,)).fetchone()
            ledger = db.execute("SELECT kind,amount FROM ledger WHERE job=? ORDER BY id",
                                (job,)).fetchall()
            device = db.execute("SELECT diode_serial_hash FROM job_devices WHERE job=?",
                                (job,)).fetchone()
            # Proof fields pass through the strict public result
            # schema (result_schema): a stored result carrying a
            # nested object or an out-of-vocabulary value publishes
            # None for that field, never the raw value.
            completed = progress["completed"] if progress else (
                public_count(result.get("completed_count")) if result else None)
            requested = public_count(result.get("requested_count")) if result else None
            evidence = {
                "job_id": row["id"],
                "operation": payload.get("operation"),
                "state": row["state"],
                "cost_credits": row["cost"],
                "request_digest": row["digest"],
                "execution": {
                    "mode": public_mode(result.get("mode")) if result else None,
                    "requested_count": requested if requested is not None
                                       else payload.get("irradiation_count"),
                    "completed_count": completed,
                    "stop_requested": bool(progress["stop_requested"]) if progress else None,
                    "stop_command_acknowledged": public_bool(
                        result.get("stop_command_acknowledged")) if result else None,
                    "last_worker_seen_at": liveness["seen_at"] if liveness else None,
                    "liveness_expired": bool(liveness["expired"]) if liveness else None,
                    "finished_at": lifecycle["finished_at"] if lifecycle else None,
                },
                "device": {
                    "public_device_id": self.public_device_id(
                        owner, device["diode_serial_hash"],
                        at=lifecycle["finished_at"] if lifecycle else None)
                    if device else None,
                    "diode_serial_recorded_server_side": False,
                    "physical_output_verified": False,
                },
                "completion_receipt": (
                    {"state": receipt["state"]} if receipt else None),
                "resolution": (
                    {"completed_count": resolution["completed"],
                     "evidence_digest": resolution["evidence_digest"],
                     "resolved_at": resolution["resolved_at"]} if resolution else None),
                "settlement": [{"kind": entry["kind"], "amount": entry["amount"]}
                               for entry in ledger],
            }
            encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":"), allow_nan=False)
            evidence["proof_digest"] = hashlib.sha256(encoded.encode()).hexdigest()
            return evidence

    def heartbeat(self, job, *, worker=None, claim_generation=None):
        with self.transaction() as db:
            if worker is not None and not db.execute(
                    'SELECT 1 FROM worker_claims WHERE job=? AND worker=?', (job, worker)).fetchone():
                raise PermissionError('Job not assigned to this worker')
            self._verify_claim(db, job, worker, claim_generation)
            if not db.execute("SELECT 1 FROM jobs WHERE id=? AND state='running'", (job,)).fetchone():
                raise ValueError('Job is not running')
            db.execute('UPDATE execution_liveness SET seen_at=? WHERE job=? AND expired=0', (time.time(), job))
            if worker is not None:
                # A live heartbeat extends the claim's expiry, so the
                # TTL only bites once the worker actually goes silent.
                db.execute('UPDATE worker_claims SET expires_at=? WHERE job=? AND worker=?',
                           (time.time() + self.limit_policy.claim_ttl_seconds, job, worker))

    def expire_executions(self, *, now=None):
        """Five minutes without worker liveness means unknown, never stopped or refunded."""
        now = time.time() if now is None else now
        with self.transaction() as db:
            # Existing pre-monitor jobs get a grace period, not a guessed execution time.
            db.execute("INSERT OR IGNORE INTO execution_liveness(job,seen_at) SELECT id,? FROM jobs WHERE state='running'", (now,))
            rows = db.execute("SELECT jobs.id FROM jobs JOIN execution_liveness ON jobs.id=job WHERE state='running' AND seen_at<=?", (now - 300,)).fetchall()
            for row in rows:
                db.execute("UPDATE jobs SET state='unknown',result=? WHERE id=?", (json.dumps({'reason': 'worker_contact_lost', 'physical_output_verified': False}), row['id']))
                db.execute('UPDATE execution_liveness SET expired=1 WHERE job=?', (row['id'],))
                self._settle_scan_reservation(db, row['id'], consumed=False)
            return len(rows)

    @staticmethod
    def _shot_terms(row):
        """(total shots, per-shot rate in tenths) fixed at reservation.

        Irradiation jobs carry their count and the accepted quote in
        the payload, so settlement stays proportional to the rate
        that was accepted even after a later rate revision: the
        settled amount for k completed shots is always
        pricing.session_charge_credits(k, rate_tenths), the same
        function the quote used. Quotes accepted before rates were
        recorded in tenths carry a whole-credit credits_per_shot,
        scaled here exactly (×10). Other jobs keep the historical
        one-to-one reading of cost (rate 10 tenths = 1 credit).
        """
        payload = json.loads(row["payload"])
        if payload.get("operation") == "irradiation":
            count = payload.get("irradiation_count")
            quote = payload.get("quote") or {}
            if type(count) is int and count >= 1:
                tenths = quote.get("credits_per_shot_tenths")
                if type(tenths) is int and tenths >= 1:
                    return count, tenths
                rate = quote.get("credits_per_shot")
                if type(rate) is not int or rate < 1:
                    credits = quote.get("credits")
                    rate = credits // count if type(credits) is int and credits >= count else 1
                return count, rate * 10
        return row["cost"], 10

    def finish(self, job, state, result, *, worker=None, diode_serial_hash=None,
               claim_generation=None):
        """Worker-only completion; afterwards, deliver any queued
        D-Planet settlement op for this job (design §4.3)."""
        self._finish(job, state, result, worker=worker, diode_serial_hash=diode_serial_hash,
                     claim_generation=claim_generation)
        self._deliver_dplanet_ops(job)

    def _finish(self, job, state, result, *, worker=None, diode_serial_hash=None,
                claim_generation=None):
        """Worker-only completion. 'failed' means confirmed no execution occurred."""
        if state not in {"completed", "failed", "unknown", "cancelled"}:
            raise ValueError("Invalid final state")
        with self.transaction() as db:
            if worker is not None and not db.execute(
                "SELECT 1 FROM worker_claims WHERE job=? AND worker=?", (job, worker)
            ).fetchone():
                raise PermissionError("Job not assigned to this worker")
            # Authenticate the claim's identity first (batch 2b item
            # 2): the early returns below acknowledge an outcome that
            # is ALREADY committed — an exact terminal receipt, an
            # authoritative resolution, or the identical state+result.
            # Those replays must succeed even after the lease expired,
            # because the worker drains its durable outbox with the
            # original generation and a rejection here wedged the
            # drain (and with it every later job) forever. This also
            # covers the batch 2c case (a resolved job's stale receipt
            # whose generation merely expired); a mismatched
            # worker/generation still raises here.
            self._verify_claim(db, job, worker, claim_generation, allow_expired=True)
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
            if row is not None:
                # Device evidence is metadata only: recording is lenient
                # here (claim validated strictly) so a bad hash can never
                # hold a settlement hostage.
                self._record_diode_hash(db, job, diode_serial_hash)
            if db.execute('SELECT 1 FROM execution_resolutions WHERE job=?', (job,)).fetchone():
                return  # Late notifications cannot undo authoritative reviewed settlement.
            encoded = json.dumps(result, sort_keys=True, allow_nan=False)
            if state == 'unknown' and row is not None and row['state'] == 'unknown' and db.execute(
                    'SELECT 1 FROM execution_liveness WHERE job=? AND expired=1', (job,)).fetchone():
                return
            receipt = db.execute("SELECT state,digest FROM completion_receipts WHERE job=?", (job,)).fetchone()
            if receipt and tuple(receipt) == (state, hashlib.sha256(encoded.encode()).hexdigest()):
                return
            if row is not None and row["state"] == state and row["result"] == encoded:
                return  # Queue committed, but the worker may have lost the acknowledgement.
            # Anything past this point is a NEW terminal transition:
            # it requires a live lease, never just an authenticated one.
            self._verify_claim(db, job, worker, claim_generation)
            # Fail-closed at the persistence boundary: a worker result
            # whose public-schema fields are malformed (nested objects,
            # free-form reason text, non-integer counts) is rejected
            # here and never stored, so it can never reach the public
            # serializer or the execution proof. The check runs after
            # the replay acknowledgements above (authoritative
            # resolution, exact receipt, identical state+result) and
            # after the live-lease re-verification: a stale outbox
            # result for an already-committed outcome is acknowledged
            # as the no-op it is instead of wedging the worker's outbox
            # on a body that no longer matters, and an expired claim
            # on an unresolved job is refused as a claim failure, not
            # leaked as a schema error (merge of batch 2b item 2 with
            # batch 2c item 2).
            validate_worker_result(result)
            if row is None or row["state"] != "running":
                raise ValueError("Job is not running")
            progress = db.execute("SELECT completed FROM execution_progress WHERE job=?", (job,)).fetchone()
            if progress and state == "completed" and progress["completed"] != self._shot_terms(row)[0]:
                raise ValueError("Completion does not match reserved shot count")
            if progress and state == "failed" and progress["completed"]:
                raise ValueError("Observed shots cannot be reported as unexecuted")
            if state == "cancelled":
                if (not progress or result.get("stop_command_acknowledged") is not True
                        or type(result.get("completed_count")) is not int
                        or result["completed_count"] != progress["completed"]):
                    raise ValueError("Confirmed stop and matching progress required")
            player_id = self._route_player(db, job)
            db.execute("UPDATE jobs SET state=?,result=? WHERE id=?", (state, encoded, job))
            if json.loads(row["payload"]).get("operation") == "scan":
                self._settle_scan_reservation(db, job, consumed=(state == "completed"))
            if state == "completed" and json.loads(row["payload"]).get("operation") == "scan":
                db.execute("INSERT OR IGNORE INTO content_lifecycle(job,finished_at) VALUES (?,?)", (job, time.time()))
            if state == "failed":
                self._release(db, row, credit_local=player_id is None)
                if player_id is not None:
                    self._queue_dplanet_op(db, job, player_id, "release", row["cost"])
            elif state == "completed":
                db.execute("INSERT INTO ledger(owner,job,kind,amount) VALUES (?,?,?,0)",
                           (row["owner"], job, "settle"))
                if player_id is not None:
                    self._queue_dplanet_op(db, job, player_id, "settle", None)
            elif state == "cancelled":
                refund = row["cost"] - session_charge_credits(
                    progress["completed"], self._shot_terms(row)[1])
                db.execute("INSERT INTO ledger(owner,job,kind,amount) VALUES (?,?,?,?)",
                           (row["owner"], job, "release", refund))
                if player_id is None:
                    db.execute("UPDATE accounts SET balance=balance+? WHERE id=?", (refund, row["owner"]))
                db.execute("INSERT INTO ledger(owner,job,kind,amount) VALUES (?,?,?,0)",
                           (row["owner"], job, "settle"))
                if player_id is not None:
                    if refund > 0:
                        self._queue_dplanet_op(db, job, player_id, "release", refund)
                    else:
                        self._queue_dplanet_op(db, job, player_id, "settle", None)
            if state in {'completed', 'failed', 'cancelled'}:
                db.execute("INSERT INTO completion_receipts VALUES (?,?,?)",
                           (job, state, hashlib.sha256(encoded.encode()).hexdigest()))
                # Same transaction as the terminal transition: a
                # notify-mode app job queues its completion mail
                # atomically with the finish itself, so no crash can
                # lose the notification (or invent one).
                self._note_job_finished(db, job, state)
                self._terminal_cleanup(db, job)

    @staticmethod
    def _remove_content(db, job):
        row = db.execute("SELECT payload,result FROM jobs WHERE id=?", (job,)).fetchone()
        payload = json.loads(row['payload'])
        minimal = {key: payload[key] for key in ('operation', 'scan_id', 'irradiation_count', 'app_intent') if key in payload}
        minimal['content_removed'] = True
        result = json.loads(row['result']) if row['result'] else None
        if result is not None:
            result = {key: result[key] for key in ('mode', 'completed_count', 'requested_count',
                'simulated_count', 'stopped', 'stop_command_acknowledged', 'physical_output_verified',
                'executed_physically', 'reason') if key in result}
        db.execute("UPDATE jobs SET payload=?,result=? WHERE id=?",
                   (json.dumps(minimal), json.dumps(result) if result is not None else None, job))
        db.execute("DELETE FROM job_context WHERE job=?", (job,))
        db.execute("UPDATE content_lifecycle SET removed=1 WHERE job=?", (job,))
        Store._expire_reading_texts(db, job)

    def _terminal_cleanup(self, db, job):
        # A terminal job's own content is minimized on every
        # channel (Astra review 2026-11-22, batch 1): the payload
        # copies a registered-target irradiation carries (entries,
        # intention, affirmation, private context) no longer persist
        # indefinitely just because the scan came from the registry.
        # A completed scan is the exception — it stays available
        # until expire_scans. The cascade into the referenced scan
        # remains request-image semantics: that intake is single-use
        # (its content was copied into the irradiation), while a
        # registered-target scan is a session artifact the app
        # legitimately reuses (further irradiations, readings) until
        # the 24-hour expiry removes it.
        row = db.execute("SELECT payload FROM jobs WHERE id=?", (job,)).fetchone()
        payload = json.loads(row['payload'])
        if payload.get('operation') == 'scan':
            state = db.execute("SELECT state FROM jobs WHERE id=?", (job,)).fetchone()[0]
            if state == 'completed':
                return  # Available for irradiation until expiry.
        self._remove_content(db, job)
        scan_id = payload.get('scan_id')
        if scan_id and payload.get('input_mode') == 'request-images-v1':
            references = db.execute("SELECT payload FROM jobs WHERE state IN ('queued','running','unknown')").fetchall()
            if not any(json.loads(r['payload']).get('scan_id') == scan_id for r in references):
                self._remove_content(db, scan_id)

    def execution_control(self, job, completed=None, *, worker=None, claim_generation=None):
        """Assigned worker reports monotonic completed shots and reads stop requests."""
        with self.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=? AND state='running'", (job,)).fetchone()
            if row is None:
                raise ValueError("Job is not running")
            if worker is not None and not db.execute(
                    "SELECT 1 FROM worker_claims WHERE job=? AND worker=?", (job, worker)).fetchone():
                raise PermissionError("Job not assigned")
            self._verify_claim(db, job, worker, claim_generation)
            payload = json.loads(row["payload"])
            if payload.get("operation") != "irradiation":
                raise ValueError("Irradiation required")
            db.execute("INSERT OR IGNORE INTO execution_progress(job) VALUES (?)", (job,))
            previous = db.execute("SELECT * FROM execution_progress WHERE job=?", (job,)).fetchone()
            if completed is not None:
                if (type(completed) is not int or completed < previous["completed"]
                        or completed > previous["completed"] + 1 or completed > self._shot_terms(row)[0]):
                    raise ValueError("Invalid completed shot count")
                db.execute("UPDATE execution_progress SET completed=? WHERE job=?", (completed, job))
            return {"stop_requested": bool(previous["stop_requested"]),
                    "completed_count": previous["completed"] if completed is None else completed}

    def reconcile_scan_receipt(self, job, digest, *, worker=None, claim_generation=None):
        """Recover a lost scan ACK without retaining scan content on the PC."""
        with self.transaction() as db:
            if worker is not None and not db.execute(
                    "SELECT 1 FROM worker_claims WHERE job=? AND worker=?", (job, worker)).fetchone():
                raise PermissionError("Job not assigned")
            # Same acknowledgement discipline as _finish (batch 2b
            # item 2): a stored receipt or an authoritative resolution
            # is acknowledged on claim identity alone, even after the
            # lease expired; the running->unknown transition below is
            # a new mutation and re-verifies the live lease.
            self._verify_claim(db, job, worker, claim_generation, allow_expired=True)
            receipt = db.execute("SELECT state,digest FROM completion_receipts WHERE job=?", (job,)).fetchone()
            if receipt and tuple(receipt) == ('completed', digest):
                return
            row = db.execute("SELECT payload,state,result FROM jobs WHERE id=?", (job,)).fetchone()
            if row is None or json.loads(row['payload']).get('operation') != 'scan':
                raise ValueError("Scan required")
            if db.execute('SELECT 1 FROM execution_resolutions WHERE job=?', (job,)).fetchone():
                return  # Reviewed terminal scan; acknowledge its stale receipt.
            reason = json.dumps({'reason': 'scan_result_delivery_unconfirmed'})
            # Replay of THIS reconciliation (Astra round-1 F7): the
            # first call transitioned the job to 'unknown' but its
            # acknowledgement was lost, and the lease has since
            # expired. The transition is recognised — by the receipt
            # recorded with it, or (ONLY for transitions recorded
            # before receipts were kept) by the exact stored result
            # only this path writes — BEFORE the live-lease check,
            # so the worker's outbox replay is acked instead of
            # wedging. When a receipt EXISTS, it alone governs
            # (Astra round-2 F7): a conflicting digest must never be
            # acknowledged through the legacy fallback. Anything
            # else remains a new transition and re-verifies strictly
            # below.
            if receipt and tuple(receipt) == ('unknown', digest):
                return
            if receipt is None and row['state'] == 'unknown' \
                    and row['result'] == reason:
                return
            if receipt is not None:
                # A receipt EXISTS and matches neither this digest
                # nor (checked above) a completed state: it is a
                # DIFFERENT outcome than the one recorded, and no
                # lease — live or expired — may ack it (Astra
                # round-3 F7: the live-lease path used to fall
                # through to the state=='unknown' return below and
                # silently accept any digest). The authoritative-
                # resolution return above is the only exception.
                raise ValueError("Scan receipt mismatch")
            self._verify_claim(db, job, worker, claim_generation)
            if row['state'] == 'unknown':
                return
            if row['state'] != 'running':
                raise ValueError("Scan receipt mismatch")
            db.execute("UPDATE jobs SET state='unknown',result=? WHERE id=?", (reason, job))
            db.execute("INSERT OR IGNORE INTO completion_receipts VALUES (?,?,?)",
                       (job, 'unknown', digest))
            self._settle_scan_reservation(db, job, consumed=False)

    def resolve_unknown(self, job, *, completed_count, stop_evidence_digest):
        """Operator-only reconciliation; afterwards, deliver any queued
        D-Planet settlement op for this job (design §4.2/§4.3)."""
        self._resolve_unknown(job, completed_count=completed_count,
                              stop_evidence_digest=stop_evidence_digest)
        self._deliver_dplanet_ops(job)

    def _resolve_unknown(self, job, *, completed_count, stop_evidence_digest):
        """Operator-only reconciliation after independent stop/evidence review.

        The digest references reviewed evidence; it does not prove physical stop.
        No public/worker route invokes this method. Never automatically resume.
        """
        if (type(completed_count) is not int or completed_count < 0
                or not isinstance(stop_evidence_digest, str) or len(stop_evidence_digest) != 64
                or any(c not in '0123456789abcdef' for c in stop_evidence_digest)):
            raise ValueError('Reviewed count and stop evidence reference required')
        with self.transaction() as db:
            previous = db.execute('SELECT completed,evidence_digest FROM execution_resolutions WHERE job=?', (job,)).fetchone()
            if previous:
                if tuple(previous) != (completed_count, stop_evidence_digest):
                    raise ValueError('Conflicting execution resolution')
                return
            row = db.execute("SELECT * FROM jobs WHERE id=? AND state='unknown'", (job,)).fetchone()
            if row is None:
                raise ValueError('Only unknown execution can be reconciled')
            payload = json.loads(row['payload'])
            if payload.get('operation') not in ('scan', 'irradiation') or completed_count > self._shot_terms(row)[0]:
                raise ValueError('Invalid execution count')
            progress = db.execute('SELECT completed FROM execution_progress WHERE job=?', (job,)).fetchone()
            if progress and completed_count < progress['completed']:
                raise ValueError('Cannot discard recorded completed shots')
            # A reviewed local receipt may prove shots missing from the remote
            # progress table after a lost acknowledgement, but never exceed cost.
            result = {'reason': 'operator_reconciled', 'completed_count': completed_count,
                      'physical_output_verified': False}
            state = 'cancelled'
            encoded = json.dumps(result, sort_keys=True, allow_nan=False)
            refund = row['cost'] - session_charge_credits(
                completed_count, self._shot_terms(row)[1])
            db.execute('UPDATE jobs SET state=?,result=? WHERE id=?', (state, encoded, job))
            db.execute('INSERT INTO execution_resolutions VALUES (?,?,?,?)', (job, completed_count, stop_evidence_digest, time.time()))
            # REPLACE, not INSERT: a scan-receipt reconciliation may
            # already have recorded its 'unknown' receipt for this
            # job (batch 2b item 2 / Astra F7); the reviewed
            # resolution is authoritative and supersedes it.
            db.execute('INSERT OR REPLACE INTO completion_receipts VALUES (?,?,?)', (job, state, hashlib.sha256(encoded.encode()).hexdigest()))
            db.execute('INSERT INTO ledger(owner,job,kind,amount) VALUES (?,?,?,?)', (row['owner'], job, 'release', refund))
            db.execute('INSERT INTO ledger(owner,job,kind,amount) VALUES (?,?,?,0)', (row['owner'], job, 'settle'))
            player_id = self._route_player(db, job)
            if player_id is None:
                db.execute('UPDATE accounts SET balance=balance+? WHERE id=?', (refund, row['owner']))
            elif refund > 0:
                self._queue_dplanet_op(db, job, player_id, "release", refund)
            else:
                self._queue_dplanet_op(db, job, player_id, "settle", None)
            if payload['operation'] == 'irradiation':
                db.execute('INSERT OR IGNORE INTO execution_progress(job) VALUES (?)', (job,))
                db.execute('UPDATE execution_progress SET completed=?,stop_requested=1 WHERE job=?', (completed_count, job))
            self._note_job_finished(db, job, state)
            self._terminal_cleanup(db, job)

    def expire_scans(self, *, now=None):
        """Remove unused scan content after 24 hours; keep only a replay tombstone.

        Running/queued/unknown irradiation references protect their scan. The
        same write transaction as submit prevents expiry/intake races.
        """
        cutoff = (time.time() if now is None else now) - 86400
        with self.transaction() as db:
            rows = db.execute("SELECT jobs.id,jobs.payload FROM jobs JOIN content_lifecycle ON jobs.id=content_lifecycle.job WHERE jobs.state='completed' AND content_lifecycle.removed=0 AND content_lifecycle.finished_at<=?", (cutoff,)).fetchall()
            removed = 0
            for row in rows:
                payload = json.loads(row['payload'])
                if payload.get('operation') != 'scan':
                    continue
                references = db.execute("SELECT payload FROM jobs WHERE state IN ('queued','running','unknown')").fetchall()
                if any(json.loads(r['payload']).get('scan_id') == row['id'] for r in references):
                    continue
                db.execute("UPDATE jobs SET payload=?,result=NULL WHERE id=?",
                           (json.dumps({'operation': 'scan', 'content_removed': True}), row['id']))
                db.execute("DELETE FROM job_context WHERE job=?", (row['id'],))
                db.execute("UPDATE content_lifecycle SET removed=1 WHERE job=?", (row['id'],))
                Store._expire_reading_texts(db, row['id'])
                removed += 1
            return removed

    def cancel(self, owner, job):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=? AND owner=?", (job, owner)).fetchone()
            if row is None:
                raise KeyError("Job not found")
            if row["state"] == "cancelled":
                return
            if row["state"] == "running" and json.loads(row["payload"]).get("operation") == "irradiation":
                db.execute("INSERT OR IGNORE INTO execution_progress(job) VALUES (?)", (job,))
                db.execute("UPDATE execution_progress SET stop_requested=1 WHERE job=?", (job,))
                return
            if row["state"] != "queued":
                raise ValueError("Only unstarted jobs may be cancelled here")
            db.execute("UPDATE jobs SET state='cancelled' WHERE id=?", (job,))
            # The owner cancelled in person; no completion mail is owed.
            self._suppress_notification(db, job)
            self._settle_scan_reservation(db, job, consumed=False)
            player_id = self._route_player(db, job)
            self._release(db, row, credit_local=player_id is None)
            if player_id is not None:
                self._queue_dplanet_op(db, job, player_id, "release", row["cost"])
            self._terminal_cleanup(db, job)
        self._deliver_dplanet_ops(job)

    @staticmethod
    def _release(db, row, *, credit_local=True):
        """Full refund of a reservation. The ledger mirror row is always
        written; credit_local=False (routed job) leaves the SI balance
        column untouched because the refund is delivered in D-Planet."""
        db.execute("INSERT INTO ledger(owner,job,kind,amount) VALUES (?,?,?,?)",
                   (row["owner"], row["id"], "release", row["cost"]))
        if credit_local:
            db.execute("UPDATE accounts SET balance=balance+? WHERE id=?", (row["cost"], row["owner"]))
