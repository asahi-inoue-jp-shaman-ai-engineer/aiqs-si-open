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
from .grants import GrantStore
from .policies import load_grant_policy, load_limit_policy
from .referral import ReferralStore
from .pricing import purchase_quote, rate_display, session_charge_credits

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
            # The 1:1 rule binds active links only (design §3.1/§6.4): a plain
            # UNIQUE on the player column would also freeze unlinked rows and
            # permanently block a legitimate re-link under a new SI account.
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS account_links_active_player
                          ON account_links(dplanet_player_id) WHERE status='active'""")

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

    def record_verified_purchase(self, owner, payment_id, yen):
        """Internal settlement boundary, never exposed as an MCP/user tool.

        Future provider adapter must verify paid status, currency, amount and
        server-owned account binding before calling. No provider is connected.
        """
        quote = purchase_quote(yen)
        if not isinstance(payment_id, str) or not 1 <= len(payment_id) <= 200:
            raise ValueError("Verified payment identity required")
        with self.transaction() as db:
            previous = db.execute("SELECT owner,yen,credits FROM purchase_receipts WHERE payment_id=?", (payment_id,)).fetchone()
            if previous:
                if tuple(previous) != (owner, yen, quote["credits"]):
                    raise ValueError("Payment already assigned to different purchase")
                return quote
            updated = db.execute("UPDATE accounts SET balance=balance+? WHERE id=?", (quote["credits"], owner))
            if updated.rowcount != 1:
                raise KeyError("Account not found")
            db.execute("INSERT INTO purchase_receipts VALUES (?,?,?,?)", (payment_id, owner, yen, quote["credits"]))
            db.execute("INSERT INTO ledger(owner,kind,amount) VALUES (?,?,?)", (owner, "purchase", quote["credits"]))
            return quote

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
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
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

    def submit(self, owner, request_key, payload, *, cost, private_context=None):
        # Cost is set by service policy, never by an MCP tool caller.
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 128:
            raise ValueError("request_key must contain 1 to 128 characters")
        if type(cost) is not int or cost < 0:
            raise ValueError("Invalid cost")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        if cost > 0 and self._dplanet_routing_enabled:
            with self.transaction() as db:
                link = self._routing_link(db, owner)
            if link is not None:
                return self._submit_routed(owner, request_key, payload, encoded=encoded,
                                           digest=digest, cost=cost,
                                           private_context=private_context, link=link)
        with self.transaction() as db:
            existing = db.execute("SELECT * FROM jobs WHERE owner=? AND request_key=?",
                                  (owner, request_key)).fetchone()
            if existing:
                if existing["digest"] != digest:
                    raise ValueError("request_key already used for different input")
                return self._job(existing)
            self._check_scan_available(db, owner, payload)
            updated = db.execute("UPDATE accounts SET balance=balance-? WHERE id=? AND balance>=?",
                                 (cost, owner, cost))
            if updated.rowcount != 1:
                raise ValueError("Account unavailable or insufficient credits")
            job = str(uuid.uuid4())
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,NULL)",
                       (job, owner, request_key, encoded, digest, cost, "queued"))
            if private_context is not None:
                db.execute("INSERT INTO job_context VALUES (?,?)", (job, json.dumps(private_context, sort_keys=True, allow_nan=False)))
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

    def _reserve_scan_quota(self, db, owner, job):
        policy = self.limit_policy
        units = policy.scan_units_per_job
        day = datetime.now(timezone.utc).date().isoformat()
        if policy.scan_daily_quota_per_account is not None:
            used = db.execute(
                "SELECT COALESCE(SUM(units),0) FROM scan_reservations "
                "WHERE owner=? AND day=? AND state IN ('reserved','consumed')",
                (owner, day)).fetchone()[0]
            if used + units > policy.scan_daily_quota_per_account:
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
                       private_context, link):
        """Reserve in D-Planet first, then create the job locally.

        The SI balance column is never touched for a routed job; the
        local ledger keeps mirror rows for audit and proof (§4.2). If
        the local insert fails after a successful reservation, the
        reservation is released as a compensating operation.
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
            self._check_scan_available(db, owner, payload)
        job = str(uuid.uuid4())
        try:
            client.reserve(player_id, job, cost)
        except DPlanetError as error:
            if error.status == 402:
                raise ValueError("Account unavailable or insufficient credits") from None
            # Fail-closed (§4.3): while D-Planet cannot reserve, the job
            # is not accepted at all. Free scans never reach this path.
            raise ValueError(f"D-Planet credit reservation failed: {error.message}") from None
        created = None
        existing = None
        try:
            with self.transaction() as db:
                existing = db.execute("SELECT * FROM jobs WHERE owner=? AND request_key=?",
                                      (owner, request_key)).fetchone()
                if existing is None:
                    self._check_scan_available(db, owner, payload)
                    db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,NULL)",
                               (job, owner, request_key, encoded, digest, cost, "queued"))
                    if private_context is not None:
                        db.execute("INSERT INTO job_context VALUES (?,?)",
                                   (job, json.dumps(private_context, sort_keys=True, allow_nan=False)))
                    db.execute("INSERT INTO ledger(owner,job,kind,amount) VALUES (?,?,?,?)",
                               (owner, job, "reserve", -cost))
                    db.execute("INSERT INTO job_credit_routes VALUES (?,?,?)",
                               (job, player_id, link["link_id"]))
                    created = self._job(db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone())
        except BaseException:
            self._compensate_reserve(player_id, job, cost)
            raise
        if created is None:
            # A concurrent submit with the same request_key won the
            # insert; this reservation has no job and is released.
            self._compensate_reserve(player_id, job, cost)
            if existing["digest"] != digest:
                raise ValueError("request_key already used for different input")
            return self._job(existing)
        return created

    def _compensate_reserve(self, player_id, job, amount):
        """Release a reservation whose local job was never created.

        A failed release is queued as a pending op (attempts=1) so the
        reservation surfaces in the reconciliation list instead of
        leaking silently (§4.2/§4.3).
        """
        try:
            self._dplanet_client.release(player_id, job, amount)
        except DPlanetError as error:
            now = time.time()
            with self.transaction() as db:
                db.execute("INSERT OR IGNORE INTO dplanet_pending_ops VALUES (?,?,?,?,?,1,?,?,?)",
                           (f"si:{job}:release", job, player_id, "release", amount,
                            error.message, now, now))

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

    def _verify_claim(self, db, job, worker, claim_generation):
        """Verify a PRESENTED claim generation (plan v4 §8).

        A presented generation must match the live claim row: same
        worker, same generation, not expired. Callers that present no
        generation (the local worker, and remote workers not yet
        updated to send one) keep the historical worker-identity
        verification at each call site; generation enforcement then
        activates per-caller as they upgrade.
        """
        if claim_generation is None:
            return
        if type(claim_generation) is not int:
            raise PermissionError("Claim generation mismatch")
        row = db.execute("SELECT worker,generation,expires_at FROM worker_claims WHERE job=?",
                         (job,)).fetchone()
        if row is None or row["worker"] != worker or row["generation"] != claim_generation:
            raise PermissionError("Claim generation mismatch")
        if row["expires_at"] is not None and row["expires_at"] <= time.time():
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

    def record_app_job(self, job, *, owner, operation, consent=None):
        """Attribute a job created through the standalone app API to
        the account it was submitted for. Irradiation rows store the
        server-generated consent record (§5.4). A request_key replay
        returns the already-created job and keeps the first record
        (INSERT OR IGNORE), exactly like delegated jobs."""
        encoded = (json.dumps(consent, sort_keys=True, allow_nan=False)
                   if consent is not None else None)
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO app_jobs VALUES (?,?,?,?,?)",
                       (job, owner, operation, encoded, time.time()))

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
        latest 20 only), newest first, joined with the job rows."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid history limit")
        with self.transaction() as db:
            rows = db.execute(
                "SELECT a.job AS job_id, a.operation AS operation, "
                "a.created_at AS created_at, j.state AS state, j.cost AS cost "
                "FROM app_jobs a JOIN jobs j ON j.id = a.job "
                "WHERE a.si_owner=? ORDER BY a.created_at DESC, a.job DESC LIMIT ?",
                (owner, limit)).fetchall()
            return [dict(row) for row in rows]

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
        confirmation (real job id) is never reopened.
        """
        with self.transaction() as db:
            db.execute("UPDATE app_irradiation_confirmations SET used_job=NULL "
                       "WHERE id=? AND owner=? AND used_job=?",
                       (confirmation_id, owner,
                        self._APP_CONFIRMATION_INTENT + request_key))

    def record_app_irradiation(self, job_id, *, owner, consent, confirmation_id,
                               request_key):
        """Attribute an app irradiation job and finalize its
        confirmation in one transaction. The confirmation must be
        held by this attempt (its intent marker) or already bound to
        this job; anything else raises ValueError, so attribution can
        never bless a job the confirmation did not authorize.
        """
        encoded = (json.dumps(consent, sort_keys=True, allow_nan=False)
                   if consent is not None else None)
        with self.transaction() as db:
            row = db.execute("SELECT used_job FROM app_irradiation_confirmations "
                             "WHERE id=? AND owner=?", (confirmation_id, owner)).fetchone()
            if row is None:
                raise ValueError("Confirmation not found")
            if row["used_job"] not in (self._APP_CONFIRMATION_INTENT + request_key, job_id):
                raise ValueError("Confirmation already used")
            db.execute("INSERT OR IGNORE INTO app_jobs VALUES (?,?,?,?,?)",
                       (job_id, owner, "irradiation", encoded, time.time()))
            db.execute("UPDATE app_irradiation_confirmations SET used_job=? WHERE id=?",
                       (job_id, confirmation_id))

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
        with self.transaction() as db:
            row = db.execute("SELECT * FROM app_readings WHERE owner=? AND request_key=?",
                             (owner, request_key)).fetchone()
            return self._reading(row) if row else None

    @staticmethod
    def _reading_route_player(db, reading_id):
        row = db.execute("SELECT player_id FROM app_reading_routes WHERE reading_id=?",
                         (reading_id,)).fetchone()
        return row["player_id"] if row is not None else None

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
            player_id = link["dplanet_player_id"]
            try:
                client.reserve(player_id, reading_id, credits)
            except DPlanetError as error:
                if error.status == 402:
                    raise ReadingChargeError("reading_insufficient_balance") from None
                raise ReadingChargeError("reading_unavailable") from None
            try:
                return self._reserve_reading_local(
                    owner, reading_id, confirmation_id, request_key,
                    credits=credits, price_version=price_version, consent=consent,
                    route={"player_id": player_id, "link_id": link["link_id"]})
            except BaseException:
                self._compensate_reserve(player_id, reading_id, credits)
                raise
        return self._reserve_reading_local(
            owner, reading_id, confirmation_id, request_key,
            credits=credits, price_version=price_version, consent=consent, route=None)

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
            db.execute(
                "INSERT INTO app_readings(id,owner,scan_id,request_key,state,credits,"
                "price_version,route,day,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (reading_id, owner, confirmation["scan_id"], request_key, "reserved",
                 credits, price_version, "native-reading", day, now))
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
                       completion_tokens, provider_cost_microusd):
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
            db.execute("INSERT INTO ledger(owner,job,kind,amount) VALUES (?,?,?,0)",
                       (owner, None, "reading_settle"))
            if player_id is not None:
                self._queue_dplanet_op(db, reading_id, player_id, "settle", None)
        self._deliver_dplanet_ops(reading_id)
        return self.get_reading(owner, reading_id)

    def release_reading(self, owner, reading_id, *, error_code):
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
            db.execute("INSERT INTO ledger(owner,job,kind,amount) VALUES (?,?,?,?)",
                       (owner, None, "reading_release", row["credits"]))
            if player_id is None:
                db.execute("UPDATE accounts SET balance=balance+? WHERE id=?",
                           (row["credits"], owner))
            else:
                self._queue_dplanet_op(db, reading_id, player_id, "release",
                                       row["credits"])
            settled = db.execute("SELECT * FROM app_readings WHERE id=?",
                                 (reading_id,)).fetchone()
            view = self._reading(settled)
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
                     "cost": row["cost"], "created_at": row["created_at"]}
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

    def record_account_link(self, owner, player_id, link_id):
        """Record a finalized D-Planet link; the binding is never rewritten.

        Call only after D-Planet finalize succeeded for this exact pair.
        Re-linking the same pair after an unlink refreshes the owner's row;
        either side actively bound to a different partner is a conflict,
        and changing partners always goes through unlink first (§6.3).
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
                return self._account_link(existing)
            if existing is not None:
                db.execute("UPDATE account_links SET dplanet_player_id=?, link_id=?, "
                           "linked_at=?, unlinked_at=NULL, status='active' WHERE si_owner=?",
                           (player_id, link_id, now, owner))
            else:
                db.execute("INSERT INTO account_links VALUES (?,?,?,?,NULL,'active')",
                           (owner, player_id, link_id, now))
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

    def transfer_balance_to_dplanet(self, owner, client=None):
        """Move the owner's whole SI balance into the linked D-Planet wallet.

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
            completed = progress["completed"] if progress else (
                result.get("completed_count") if result else None)
            evidence = {
                "job_id": row["id"],
                "operation": payload.get("operation"),
                "state": row["state"],
                "cost_credits": row["cost"],
                "request_digest": row["digest"],
                "execution": {
                    "mode": result.get("mode") if result else None,
                    "requested_count": (result.get("requested_count") if result else None)
                                       or payload.get("irradiation_count"),
                    "completed_count": completed,
                    "stop_requested": bool(progress["stop_requested"]) if progress else None,
                    "stop_command_acknowledged": result.get("stop_command_acknowledged") if result else None,
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
        encoded = json.dumps(result, sort_keys=True, allow_nan=False)
        with self.transaction() as db:
            if worker is not None and not db.execute(
                "SELECT 1 FROM worker_claims WHERE job=? AND worker=?", (job, worker)
            ).fetchone():
                raise PermissionError("Job not assigned to this worker")
            self._verify_claim(db, job, worker, claim_generation)
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
            if row is not None:
                # Device evidence is metadata only: recording is lenient
                # here (claim validated strictly) so a bad hash can never
                # hold a settlement hostage.
                self._record_diode_hash(db, job, diode_serial_hash)
            if state == 'unknown' and row is not None and row['state'] == 'unknown' and db.execute(
                    'SELECT 1 FROM execution_liveness WHERE job=? AND expired=1', (job,)).fetchone():
                return
            if db.execute('SELECT 1 FROM execution_resolutions WHERE job=?', (job,)).fetchone():
                return  # Late notifications cannot undo authoritative reviewed settlement.
            receipt = db.execute("SELECT state,digest FROM completion_receipts WHERE job=?", (job,)).fetchone()
            if receipt and tuple(receipt) == (state, hashlib.sha256(encoded.encode()).hexdigest()):
                return
            if row is not None and row["state"] == state and row["result"] == encoded:
                return  # Queue committed, but the worker may have lost the acknowledgement.
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
                self._terminal_cleanup(db, job)

    @staticmethod
    def _remove_content(db, job):
        row = db.execute("SELECT payload,result FROM jobs WHERE id=?", (job,)).fetchone()
        payload = json.loads(row['payload'])
        minimal = {key: payload[key] for key in ('operation', 'scan_id', 'irradiation_count') if key in payload}
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
            self._verify_claim(db, job, worker, claim_generation)
            receipt = db.execute("SELECT state,digest FROM completion_receipts WHERE job=?", (job,)).fetchone()
            if receipt and tuple(receipt) == ('completed', digest):
                return
            row = db.execute("SELECT payload,state FROM jobs WHERE id=?", (job,)).fetchone()
            if row is None or json.loads(row['payload']).get('operation') != 'scan':
                raise ValueError("Scan required")
            if db.execute('SELECT 1 FROM execution_resolutions WHERE job=?', (job,)).fetchone():
                return  # Reviewed terminal scan; acknowledge its stale receipt.
            reason = json.dumps({'reason': 'scan_result_delivery_unconfirmed'})
            if row['state'] == 'unknown':
                return
            if row['state'] != 'running':
                raise ValueError("Scan receipt mismatch")
            db.execute("UPDATE jobs SET state='unknown',result=? WHERE id=?", (reason, job))
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
            db.execute('INSERT INTO completion_receipts VALUES (?,?,?)', (job, state, hashlib.sha256(encoded.encode()).hexdigest()))
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
