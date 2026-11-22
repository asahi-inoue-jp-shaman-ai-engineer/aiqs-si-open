"""Referral redemption family of the Store.

Split alongside grants.py: registration-time referral lives in
grants.GrantStore._registration_referral and records its redemption
here; this module owns everything after registration — the 6-char
code format, settings-time redemption (instant grant to BOTH sides,
the gift popup is a celebration notification only), cross-system
codes (SI accepts D-Planet codes and vice versa through the shared
internal contract), the D-Planet -> SI grant inbox receiver, and
the daily-monitoring summary.

Shared cross-system contract (both systems build to it):
- Code format: 6 chars from the 31-char alphabet below (excludes
  0/O/1/I/L). Input is normalized: uppercase, hyphens/spaces stripped.
  Legacy token codes issued before this format remain valid wherever
  codes are looked up.
- SI -> D-Planet grant: delivered through the linking client's
  grant operation; idempotent by an operation key on the
  D-Planet side.
- D-Planet -> SI grant delivery calls
  resolve_referral_code_owner / apply_referral_grant here.
"""
import re
import secrets
import time
import uuid
from datetime import datetime, timezone

from .dplanet import DPlanetError

REFERRAL_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
# Letter subset of the alphabet (fix round F-SI-6): SI-issued codes
# always start with a letter; D-Planet issues digit-first codes, so
# the two namespaces can never collide.
REFERRAL_CODE_LETTERS = "ABCDEFGHJKMNPQRSTUVWXYZ"
REFERRAL_CODE_LENGTH = 6
# Durable attempt limit (fix round F-SI-5): failed code resolutions
# per account over a rolling hour.
REFERRAL_ATTEMPT_LIMIT = 30
REFERRAL_ATTEMPT_WINDOW_SECONDS = 3600


class ReferralError(ValueError):
    """A referral operation failed under a fixed error code.

    The code is one of invalid_code / own_code / already_redeemed /
    window_closed / cross_unconfigured / amount_mismatch / conflict /
    too_many_attempts; the API layers map it to an HTTP status and
    answer with it verbatim. Carries no code values or account detail.
    """

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def normalize_referral_code(code):
    """Uppercase and strip hyphens/spaces; None for non-strings."""
    if not isinstance(code, str):
        return None
    return "".join(ch for ch in code.upper() if ch not in ("-", " ", "\t"))


def is_referral_code_format(code):
    return (isinstance(code, str) and len(code) == REFERRAL_CODE_LENGTH
            and all(ch in REFERRAL_CODE_ALPHABET for ch in code))


class ReferralStore:
    # --- code format -------------------------------------------------

    def _generate_referral_code(self, db):
        """A fresh 6-char code, retried on UNIQUE collision.

        The first character is always a letter (F-SI-6 namespace
        partition); the remaining five use the full alphabet.
        """
        for _ in range(50):
            code = secrets.choice(REFERRAL_CODE_LETTERS) + "".join(
                secrets.choice(REFERRAL_CODE_ALPHABET)
                for _ in range(REFERRAL_CODE_LENGTH - 1))
            if not db.execute("SELECT 1 FROM invitation_codes WHERE code=?",
                              (code,)).fetchone():
                return code
        raise RuntimeError("Could not allocate a referral code")

    @staticmethod
    def _code_match(db, code):
        """(owner, canonical stored code) for a code as entered.

        Lookup tries the code verbatim first (legacy token codes are
        case-sensitive and stay valid), then its normalized form (the
        6-char codes are entered in any case, with or without
        hyphens/spaces). The canonical form is the exact string
        stored in invitation_codes (round 3, R3-SI-8a): callers that
        persist a code must persist this one, because
        registration_referrals.code has a foreign key to it — storing
        the registrant's raw variant (abc-234 for stored ABC234)
        resolves here but then fails that FK and rolls provisioning
        back.
        """
        if not isinstance(code, str) or not code:
            return None, None
        row = db.execute("SELECT owner, code FROM invitation_codes WHERE code=?",
                         (code,)).fetchone()
        if row is not None:
            return row["owner"], row["code"]
        normalized = normalize_referral_code(code)
        if normalized and normalized != code:
            row = db.execute("SELECT owner, code FROM invitation_codes WHERE code=?",
                             (normalized,)).fetchone()
            if row is not None:
                return row["owner"], row["code"]
        return None, None

    @staticmethod
    def _code_owner(db, code):
        """Owner of a code as entered, or None (see _code_match)."""
        return ReferralStore._code_match(db, code)[0]

    @staticmethod
    def _canonical_code(db, code):
        """The exact stored code a code as entered resolves to."""
        return ReferralStore._code_match(db, code)[1]

    def _issue_code(self, db, owner):
        """The owner's stored code, issuing a new-format one if none."""
        prior = db.execute("SELECT code FROM invitation_codes WHERE owner=?",
                           (owner,)).fetchone()
        if prior:
            return prior["code"]
        code = self._generate_referral_code(db)
        db.execute("INSERT INTO invitation_codes VALUES (?,?)", (owner, code))
        return code

    def resolve_referral_code_owner(self, code):
        """SI owner for a code (internal resolve-code receiver core)."""
        with self.transaction() as db:
            return self._code_owner(db, code)

    def _resolve_cross_player(self, code):
        """D-Planet player_id for a code, or None (fix round F-SI-2).

        None covers every non-resolution: no client configured, not
        a D-Planet code, or D-Planet unreachable — registration
        then fails the code exactly as an unknown local code.
        Called OUTSIDE any write transaction.
        """
        client = self._dplanet_client
        if client is None:
            return None
        try:
            resolved = client.resolve_referral_code(
                normalize_referral_code(code) or code)
        except DPlanetError:
            return None
        if not resolved or not resolved.get("player_id"):
            return None
        return resolved["player_id"]

    # --- grant execution ----------------------------------------------

    def _insert_gift(self, db, owner, amount, kind, redemption_id, now):
        db.execute("INSERT INTO referral_gifts VALUES (?,?,?,?,?,?,NULL)",
                   (str(uuid.uuid4()), owner, amount, kind, redemption_id, now))

    def _grant_referral_side(self, db, *, si_owner=None, player_id=None,
                             role, redemption_id, gift=True, amount=None):
        """Credit one side of a referral; returns the side's outcome.

        The amount defaults to the current policy amount; callers
        completing a stored entitlement (held-resume, inbox retry)
        pass the STORED amount, which is immutable (round 3,
        R3-SI-3/R3-SI-4). Outcomes: "applied" (credited on the SI
        ledger), "queued" (a phase-'grant' pending op was queued for
        D-Planet delivery), or "held" (_apply_grant refused the
        local credit under its ledger invariants and recorded a
        grant_holds row).

        An SI owner whose balance lives in D-Planet (routing enabled
        and an active link exists) is NOT credited on the SI ledger;
        the grant op is queued instead and delivered through
        _deliver_dplanet_ops. A bare player_id (a D-Planet-only
        inviter) always queues the grant op. Gift rows are inserted
        ONLY for a locally applied SI-owner side (round 3,
        R3-SI-5c): a routed/queued side gets NO SI gift row — the
        receiving system creates the gift when the grant lands
        (D-Planet's siReferralGrant does; the SI inbox does for the
        opposite direction). With gift=False the caller additionally
        suppresses the local gift row.
        """
        if amount is None:
            amount = self.grant_policy.referral_credits
        now = time.time()
        if si_owner is not None:
            link = self._routing_link(db, si_owner)
            if link is not None:
                op_key = f"si:referral:{redemption_id}:{role}"
                db.execute(
                    "INSERT OR IGNORE INTO dplanet_pending_ops VALUES (?,?,?,?,?,0,NULL,?,?)",
                    (op_key, redemption_id, link["dplanet_player_id"],
                     "grant", amount, now, now))
                return "queued"
            applied = self._apply_grant(
                db, si_owner, f"referral_{role}_v1", amount,
                inviter=si_owner if role == "inviter" else None)
            if not applied:
                return "held"
            if gift:
                self._insert_gift(db, si_owner, amount, role, redemption_id, now)
            return "applied"
        if player_id is not None:
            op_key = f"si:referral:{redemption_id}:{role}"
            db.execute(
                "INSERT OR IGNORE INTO dplanet_pending_ops VALUES (?,?,?,?,?,0,NULL,?,?)",
                (op_key, redemption_id, player_id, "grant", amount, now, now))
            return "queued"
        raise ValueError("A referral side needs an SI owner or a player_id")

    def _record_registration_referral(self, db, invitee, inviter, code, amount,
                                      *, redemption_id=None, inviter_kind="si",
                                      inviter_player_id=None,
                                      inviter_gift=True, invitee_gift=True,
                                      status="completed"):
        """Redemption + gift rows for a registration-time referral.

        Called from grants._registration_referral. INSERT OR IGNORE
        on the redemption: the invitee can only ever have one.
        Returns the redemption id (the caller's, when supplied).
        With status='held' (round 3, R3-SI-5b) the caller passes both
        gift flags False: a held registration redemption carries no
        gift rows and no grants until resume completes it.
        """
        now = time.time()
        redemption_id = redemption_id or str(uuid.uuid4())
        cursor = db.execute(
            "INSERT OR IGNORE INTO referral_redemptions"
            "(id,invitee_owner,inviter_kind,inviter_si_owner,inviter_player_id,"
            "code,source,amount,created_at,status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (redemption_id, invitee, inviter_kind,
             inviter if inviter_kind == "si" else None, inviter_player_id,
             code, "registration", amount, now, status))
        if cursor.rowcount != 1:
            return redemption_id
        if inviter_kind == "si" and inviter_gift:
            self._insert_gift(db, inviter, amount, "inviter", redemption_id, now)
        if invitee_gift:
            self._insert_gift(db, invitee, amount, "invitee", redemption_id, now)
        return redemption_id

    # --- grant-policy pre-check (fix round F-SI-4) ---------------------

    def _referral_hold_reason(self, db, *, inviter_si_owner, total_amount,
                              side_amount, exclude_redemption_id=None,
                              exclude_op_key=None):
        """Why the grant policy would hold a referral, or None.

        Round 3 (R3-SI-5a): the accounting source of truth is
        referral_redemptions, NOT the SI ledger — a routed grant
        writes no ledger row, so ledger accounting let routed
        grants evade both the per-inviter cap and the total budget.
        Inviter earnings are the amount sum over that inviter's
        redemptions with status completed/held, plus single-side
        inviter grants received through the inbox (state
        applied/held). Total budget consumption is amount*2 (both
        sides) over SI-origin redemptions (sources registration /
        settings) with status completed/held, plus single-side
        inbox grants, plus the non-referral grant ledger kinds
        (migration / legacy loyalty — the part of _apply_grant's
        counters that has no redemption row). Registration grants
        create redemption rows, so they are counted exactly once,
        through the redemption sum; referral ledger kinds are never
        summed here, which is what prevents double counting.

        Evaluated for the WHOLE operation (both sides,
        total_amount) before anything is granted, regardless of
        whether each side lands locally or is routed to D-Planet.
        exclude_redemption_id / exclude_op_key drop the caller's own
        already-recorded held entitlement (resume / inbox retry) so
        it is not counted against itself.
        """
        policy = self.grant_policy
        if inviter_si_owner is not None \
                and policy.referral_cap_per_inviter is not None:
            earned = db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM referral_redemptions "
                "WHERE inviter_si_owner=? AND status IN ('completed','held') "
                "AND (? IS NULL OR id<>?)",
                (inviter_si_owner, exclude_redemption_id,
                 exclude_redemption_id)).fetchone()[0]
            earned += db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM referral_grant_inbox "
                "WHERE si_owner=? AND state IN ('applied','held') "
                "AND substr(op_key,-8)=':inviter' "
                "AND (? IS NULL OR op_key<>?)",
                (inviter_si_owner, exclude_op_key, exclude_op_key)).fetchone()[0]
            if earned + side_amount > policy.referral_cap_per_inviter:
                return "inviter_cap"
        if policy.total_budget is not None:
            spent = db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM referral_redemptions "
                "WHERE status IN ('completed','held') "
                "AND source IN ('registration','settings') "
                "AND (? IS NULL OR id<>?)",
                (exclude_redemption_id, exclude_redemption_id)).fetchone()[0] * 2
            spent += db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM referral_grant_inbox "
                "WHERE state IN ('applied','held') "
                "AND (? IS NULL OR op_key<>?)",
                (exclude_op_key, exclude_op_key)).fetchone()[0]
            spent += db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM ledger "
                "WHERE kind IN ('migration_award_v1','legacy_loyalty_gift')"
            ).fetchone()[0]
            if spent + total_amount > policy.total_budget:
                return "total_budget"
        return None

    # --- cross-system claim rows (fix round F-SI-3) --------------------

    def _insert_cross_claim(self, db, invitee_owner, *, source="dplanet_cross",
                            code=None):
        """Record that this SI account already redeemed via D-Planet.

        INSERT OR IGNORE under the one-redemption-per-invitee rule:
        an existing redemption (any source) is left untouched, so
        the claim is idempotent and never creates a second row.
        """
        db.execute(
            "INSERT OR IGNORE INTO referral_redemptions"
            "(id,invitee_owner,inviter_kind,inviter_si_owner,inviter_player_id,"
            "code,source,amount,created_at,status) VALUES (?,?,?,NULL,NULL,?,?,?,?,'completed')",
            (str(uuid.uuid4()), invitee_owner, "dplanet", code, source,
             self.grant_policy.referral_credits, time.time()))

    def record_dplanet_cross_claim(self, owner, *, source="dplanet_cross"):
        """Public seam for link-time reconciliation (F-SI-3)."""
        with self.transaction() as db:
            if not db.execute("SELECT 1 FROM accounts WHERE id=?",
                              (owner,)).fetchone():
                raise KeyError("Account not found")
            self._insert_cross_claim(db, owner, source=source)

    def mark_referral_redeemed(self, owner, ref=None):
        """Push receiver core (round 3, R3-SI-1): D-Planet reports
        that this SI account's linked player already redeemed there.

        Idempotently inserts the invitee claim row with
        source='dplanet_push' (the sender's ref, when supplied, is
        kept on the row's code column for provenance); an existing
        redemption of any source is left untouched. Returns True
        when a new claim row was inserted.
        """
        if ref is not None and (not isinstance(ref, str) or len(ref) > 300):
            raise ValueError("Invalid referral ref")
        with self.transaction() as db:
            if not db.execute("SELECT 1 FROM accounts WHERE id=?",
                              (owner,)).fetchone():
                raise KeyError("Account not found")
            before = db.execute(
                "SELECT COUNT(*) FROM referral_redemptions WHERE invitee_owner=?",
                (owner,)).fetchone()[0]
            self._insert_cross_claim(db, owner, source="dplanet_push", code=ref)
            after = db.execute(
                "SELECT COUNT(*) FROM referral_redemptions WHERE invitee_owner=?",
                (owner,)).fetchone()[0]
            return after > before

    # --- attempt limiting (fix round F-SI-5, round 3 R3-SI-7) -------

    def _recent_failed_attempts(self, db, owner, now):
        return db.execute(
            "SELECT COUNT(*) FROM referral_attempts WHERE account=? AND attempted_at>?",
            (owner, now - REFERRAL_ATTEMPT_WINDOW_SECONDS)).fetchone()[0]

    def _begin_referral_attempt(self, owner):
        """Reserve an attempt slot BEFORE code resolution (R3-SI-7).

        Counting after resolution let concurrent bad attempts all
        pass the <30 check while none had been recorded yet. The
        count and the insert now happen inside one short write
        transaction (BEGIN IMMEDIATE serializes writers), so
        in-flight attempts count too. Returns the attempt row id;
        the caller deletes that row when the resolution succeeds —
        only failed resolutions persist and consume quota. Raises
        ReferralError('too_many_attempts') without inserting when
        the quota is already exhausted.
        """
        now = time.time()
        with self.transaction() as db:
            if self._recent_failed_attempts(db, owner, now) >= REFERRAL_ATTEMPT_LIMIT:
                raise ReferralError("too_many_attempts")
            cursor = db.execute(
                "INSERT INTO referral_attempts(account,attempted_at) VALUES (?,?)",
                (owner, now))
            return cursor.lastrowid

    def _discard_referral_attempt(self, attempt_id):
        """Drop a reserved attempt row after a successful resolution."""
        if attempt_id is None:
            return
        with self.transaction() as db:
            db.execute("DELETE FROM referral_attempts WHERE id=?", (attempt_id,))

    # --- redemption window ---------------------------------------------

    def _grace_unconfigured(self):
        """Grace deadline set without its cohort start (R3-SI-6)."""
        policy = self.grant_policy
        return policy.referral_grace_until is not None \
            and policy.referral_grace_start is None

    def _window_state(self, created_at, now):
        """(open, closes_at, grace_active) for one account.

        Grace cohort rule (fix round F-SI-1): when the policy sets
        referral_grace_start, the launch grace covers only accounts
        that existed before launch — created_at NULL (legacy) or
        created_at < referral_grace_start. A post-launch account
        gets only its own 48h window even while the grace deadline
        is still in the future.

        Fail-closed (round 3, R3-SI-6): grace is active only when
        BOTH timestamps are set. A deadline-only configuration
        (referral_grace_until set, referral_grace_start missing)
        used to reopen the window for every account; it now leaves
        grace INACTIVE — only the plain 48h window applies — and
        referral_status surfaces the misconfiguration as
        grace_unconfigured.
        """
        policy = self.grant_policy
        grace_until = policy.referral_grace_until
        grace_start = policy.referral_grace_start
        in_cohort = grace_start is not None and (
            created_at is None or created_at < grace_start)
        grace_active = grace_until is not None and grace_start is not None \
            and now <= grace_until and in_cohort
        closes_at = (created_at + policy.referral_window_seconds
                     if created_at is not None else None)
        if created_at is None:
            # Pre-launch account (no creation stamp): eligible only
            # while the launch grace is active.
            return grace_active, closes_at, grace_active
        return (now - created_at <= policy.referral_window_seconds) or grace_active, \
            closes_at, grace_active

    # --- settings-time redemption ---------------------------------------

    def _redeem_precheck(self, owner):
        """Read-side snapshot for redeem_referral (fix round F-SI-5).

        Returns (created_at, caller_player_id). Raises KeyError for
        an unknown account and ReferralError('already_redeemed') /
        ReferralError('too_many_attempts') for the durable states.
        Runs in its own short transaction so no write lock is held
        across the remote code resolution that follows.
        """
        now = time.time()
        with self.transaction() as db:
            account = db.execute("SELECT created_at FROM accounts WHERE id=?",
                                 (owner,)).fetchone()
            if account is None:
                raise KeyError("Account not found")
            if db.execute("SELECT 1 FROM referral_redemptions WHERE invitee_owner=?",
                          (owner,)).fetchone():
                raise ReferralError("already_redeemed")
            prior = db.execute("SELECT code FROM registration_referrals WHERE invitee=?",
                               (owner,)).fetchone()
            if prior is not None and prior["code"]:
                raise ReferralError("already_redeemed")
            if self._recent_failed_attempts(db, owner, now) >= REFERRAL_ATTEMPT_LIMIT:
                raise ReferralError("too_many_attempts")
            active = db.execute(
                "SELECT dplanet_player_id FROM account_links "
                "WHERE si_owner=? AND status='active'", (owner,)).fetchone()
            return account["created_at"], \
                (active["dplanet_player_id"] if active else None)

    def redeem_referral(self, owner, code, source="settings"):
        """Redeem a referral code after registration (decision D2/D10).

        Instant grant to both sides at the policy amount; no claim
        step. Raises ReferralError with a fixed code on every
        rejection. After commit, queued D-Planet grant ops are
        delivered best-effort (they stay queued on failure and are
        retried by the operator reconciliation path).

        Fix round: eligibility/window checks and code resolution
        (local read + the remote D-Planet call) happen OUTSIDE the
        write transaction, which re-validates before applying
        (F-SI-5); failed resolutions are durably attempt-limited;
        the grant policy is evaluated for both sides before
        granting, and a would-be hold persists the redemption as
        'held' with no gifts and no ops (F-SI-4).
        """
        if source not in ("registration", "settings"):
            raise ValueError("Invalid referral source")
        normalized = normalize_referral_code(code)
        if not normalized or len(normalized) > 128:
            raise ReferralError("invalid_code")
        # --- Phase 1 (no write lock): eligibility + resolution ---
        created_at, caller_player = self._redeem_precheck(owner)
        # Reverse-order double-claim check (round 3, R3-SI-1): the
        # link-time reconciliation (F-SI-3) only sees redemptions
        # that happened BEFORE linking. A player who linked first
        # and redeemed in D-Planet afterwards is caught here, at
        # redeem time: a linked invitee whose player already
        # redeemed on the D-Planet side is already_redeemed.
        # Best-effort on transport failure — proceed; the D-Planet
        # side's own claim check is the other half of the guard.
        if caller_player is not None and self._dplanet_client is not None:
            status_fn = getattr(self._dplanet_client,
                                "referral_redemption_status", None)
            if status_fn is not None:
                try:
                    status = status_fn(caller_player)
                except Exception:
                    status = None
                if isinstance(status, dict) and status.get("redeemed"):
                    raise ReferralError("already_redeemed")
        # Reserve the attempt slot BEFORE resolution (R3-SI-7); the
        # row is deleted again when the resolution succeeds, so only
        # failed resolutions consume quota.
        attempt_id = self._begin_referral_attempt(owner)
        with self.transaction() as db:
            local_owner = self._code_owner(db, code)
        inviter_kind = None
        inviter_si_owner = None
        inviter_player_id = None
        if local_owner is not None:
            self._discard_referral_attempt(attempt_id)
            if local_owner == owner:
                raise ReferralError("own_code")
            inviter_kind = "si"
            inviter_si_owner = local_owner
        else:
            client = self._dplanet_client
            if client is None:
                raise ReferralError("invalid_code")
            try:
                resolved = client.resolve_referral_code(normalized)
            except DPlanetError:
                # Not a failed resolution (the code may be valid);
                # historically uncounted, so release the slot.
                self._discard_referral_attempt(attempt_id)
                raise ReferralError("cross_unconfigured") from None
            if not resolved or not resolved.get("player_id"):
                raise ReferralError("invalid_code")
            self._discard_referral_attempt(attempt_id)
            inviter_player_id = resolved["player_id"]
            if caller_player is not None and caller_player == inviter_player_id:
                raise ReferralError("own_code")
            inviter_kind = "dplanet"
        now = time.time()
        open_, _closes, _grace = self._window_state(created_at, now)
        if not open_:
            raise ReferralError("window_closed")
        amount = self.grant_policy.referral_credits
        # --- Phase 2 (write transaction): re-validate, then apply ---
        with self.transaction() as db:
            account = db.execute("SELECT created_at FROM accounts WHERE id=?",
                                 (owner,)).fetchone()
            if account is None:
                raise KeyError("Account not found")
            if db.execute("SELECT 1 FROM referral_redemptions WHERE invitee_owner=?",
                          (owner,)).fetchone():
                raise ReferralError("already_redeemed")
            prior = db.execute("SELECT code FROM registration_referrals WHERE invitee=?",
                               (owner,)).fetchone()
            if prior is not None and prior["code"]:
                raise ReferralError("already_redeemed")
            if inviter_kind == "si":
                current_owner = self._code_owner(db, code)
                if current_owner is None:
                    raise ReferralError("invalid_code")
                if current_owner == owner:
                    raise ReferralError("own_code")
                inviter_si_owner = current_owner
            else:
                active = db.execute(
                    "SELECT dplanet_player_id FROM account_links "
                    "WHERE si_owner=? AND status='active'", (owner,)).fetchone()
                caller_now = active["dplanet_player_id"] if active else None
                if caller_now is not None and caller_now == inviter_player_id:
                    raise ReferralError("own_code")
            open_now, _c, _g = self._window_state(account["created_at"], time.time())
            if not open_now:
                raise ReferralError("window_closed")
            redemption_id = str(uuid.uuid4())
            held_reason = self._referral_hold_reason(
                db, inviter_si_owner=inviter_si_owner,
                total_amount=2 * amount, side_amount=amount)
            if held_reason is not None:
                # Policy honesty (F-SI-4): the redemption is recorded
                # as held — no grants, no gift rows, no queued ops —
                # and the caller is told so, never a false success.
                db.execute(
                    "INSERT INTO referral_redemptions"
                    "(id,invitee_owner,inviter_kind,inviter_si_owner,inviter_player_id,"
                    "code,source,amount,created_at,status) "
                    "VALUES (?,?,?,?,?,?,?,?,?,'held')",
                    (redemption_id, owner, inviter_kind, inviter_si_owner,
                     inviter_player_id, normalized, source, amount, time.time()))
                balance = db.execute("SELECT balance FROM accounts WHERE id=?",
                                     (owner,)).fetchone()["balance"]
                return {"redemption_id": redemption_id, "amount": amount,
                        "inviter_kind": inviter_kind, "source": source,
                        "balance": balance, "status": "held",
                        "held_reason": held_reason}
            db.execute(
                "INSERT INTO referral_redemptions"
                "(id,invitee_owner,inviter_kind,inviter_si_owner,inviter_player_id,"
                "code,source,amount,created_at,status) "
                "VALUES (?,?,?,?,?,?,?,?,?,'completed')",
                (redemption_id, owner, inviter_kind, inviter_si_owner,
                 inviter_player_id, normalized, source, amount, time.time()))
            self._grant_referral_side(db, si_owner=owner, role="invitee",
                                      redemption_id=redemption_id)
            if inviter_kind == "si":
                self._grant_referral_side(db, si_owner=inviter_si_owner,
                                          role="inviter",
                                          redemption_id=redemption_id)
            else:
                self._grant_referral_side(db, player_id=inviter_player_id,
                                          role="inviter",
                                          redemption_id=redemption_id)
            balance = db.execute("SELECT balance FROM accounts WHERE id=?",
                                 (owner,)).fetchone()["balance"]
        self._deliver_dplanet_ops(redemption_id)
        return {"redemption_id": redemption_id, "amount": amount,
                "inviter_kind": inviter_kind, "source": source,
                "balance": balance, "status": "completed"}

    def resume_held_redemptions(self):
        """Complete held redemptions the policy now allows (F-SI-4).

        Each held redemption is re-checked against the grant policy
        inside its own transaction; when it passes, both sides are
        granted (locally or via queued ops) at the redemption's
        STORED amount, the gift rows are written for locally applied
        sides only (R3-SI-5c), and the redemption flips to
        'completed' only when both sides landed (R3-SI-3). Returns
        {"resumed": [...ids], "still_held": [...ids]}.
        """
        with self.transaction() as db:
            held = db.execute(
                "SELECT * FROM referral_redemptions WHERE status='held' "
                "ORDER BY created_at").fetchall()
        resumed, still_held, deliver = [], [], []
        for row in held:
            # The STORED amount is the immutable entitlement
            # (R3-SI-3): eligibility, grants, ops and gifts all use
            # it, never the current policy amount.
            amount = row["amount"]
            with self.transaction() as db:
                current = db.execute(
                    "SELECT status FROM referral_redemptions WHERE id=?",
                    (row["id"],)).fetchone()
                if current is None or current["status"] != "held":
                    continue
                if self._referral_hold_reason(
                        db, inviter_si_owner=row["inviter_si_owner"],
                        total_amount=2 * amount, side_amount=amount,
                        exclude_redemption_id=row["id"]) is not None:
                    still_held.append(row["id"])
                    continue
                # Per-side outcomes are propagated (R3-SI-3): the
                # inviter side goes first, and the redemption flips
                # to 'completed' only when BOTH sides are applied or
                # queued. A side held again (its _apply_grant refusal
                # is recorded as a grant_holds row carrying the fresh
                # reason) keeps the redemption 'held'; sides already
                # completed by an earlier partial resume are recorded
                # in referral_redemption_sides and never re-granted.
                sides = [
                    ("inviter", {"si_owner": row["inviter_si_owner"]}
                     if row["inviter_kind"] == "si"
                     else {"player_id": row["inviter_player_id"]}),
                    ("invitee", {"si_owner": row["invitee_owner"]}),
                ]
                complete = True
                for role, target in sides:
                    done = db.execute(
                        "SELECT state FROM referral_redemption_sides "
                        "WHERE redemption_id=? AND role=?",
                        (row["id"], role)).fetchone()
                    if done is not None:
                        continue
                    outcome = self._grant_referral_side(
                        db, role=role, redemption_id=row["id"],
                        amount=amount, **target)
                    if outcome in ("applied", "queued"):
                        db.execute(
                            "INSERT OR IGNORE INTO referral_redemption_sides "
                            "VALUES (?,?,?,?)",
                            (row["id"], role, outcome, amount))
                    else:
                        complete = False
                if not complete:
                    still_held.append(row["id"])
                    continue
                db.execute("UPDATE referral_redemptions SET status='completed' "
                           "WHERE id=?", (row["id"],))
                resumed.append(row["id"])
                deliver.append(row["id"])
        for redemption_id in deliver:
            self._deliver_dplanet_ops(redemption_id)
        return {"resumed": resumed, "still_held": still_held}

    # --- D-Planet -> SI grant inbox --------------------------------------

    @staticmethod
    def _op_key_redemption_ref(op_key):
        """The redemption id embedded in a referral op_key, or None.

        Op keys are si:referral:<redemption_id>:<role> /
        dp:referral:<redemption_id>:<role>; the id itself never
        contains a colon in either system, so a strict 4-part split
        is unambiguous. Non-referral keys return None.
        """
        if not isinstance(op_key, str):
            return None
        parts = op_key.split(":")
        if len(parts) == 4 and parts[0] in ("si", "dp") \
                and parts[1] == "referral" and parts[3] in ("inviter", "invitee"):
            return parts[2]
        return None

    def apply_referral_grant(self, si_owner, op_key, amount):
        """Inbox receiver core for a D-Planet-side referral grant.

        Idempotent by op_key: a replay with identical content is a
        no-op duplicate; the same key with different owner/amount is
        a conflict.

        Round 3 (R3-SI-4, receiver-side stranding): the inbox/op
        record is checked FIRST — an existing op returns its
        recorded state with no policy revalidation, so a replay (or
        a held retry) at the amount the entitlement was created
        with still lands after the policy amount has moved. For
        fresh ops the amount is accepted when it equals the current
        policy amount OR the amount stored on the redemption the
        op_key references; anything else is amount_mismatch.

        Round 3 (R3-SI-2, invitee double-credit): an 'invitee' grant
        for an account that already has a COMPLETED redemption
        (any source) is a duplicate WITHOUT crediting or gifting.
        When the account's existing redemption is HELD, the inbox
        grant is that redemption's completion: it credits, writes
        the gift, and flips held -> completed.

        Fix round: the role comes from the op_key suffix
        (':inviter' / ':invitee', default 'inviter'). An applied
        'invitee' grant also records the cross-system claim row for
        si_owner (F-SI-3), so a later SI-side redeem answers
        already_redeemed. When the grant policy would hold the
        grant, the inbox row is recorded as held and the result is
        {'state': 'held'} with NO gift row (F-SI-4), so the
        D-Planet sender keeps the op pending instead of marking it
        delivered; a later replay re-checks the policy and completes
        the grant once it allows.
        """
        if not isinstance(op_key, str) or not op_key or len(op_key) > 300:
            raise ReferralError("invalid_code")
        role = "invitee" if op_key.endswith(":invitee") else "inviter"
        redemption_id = "inbox:" + re.sub(r"[^A-Za-z0-9_-]", "-", op_key)
        with self.transaction() as db:
            if not db.execute("SELECT 1 FROM accounts WHERE id=?",
                              (si_owner,)).fetchone():
                raise KeyError("Account not found")
            existing = db.execute(
                "SELECT * FROM referral_grant_inbox WHERE op_key=?",
                (op_key,)).fetchone()
            if existing is not None:
                if existing["si_owner"] != si_owner or existing["amount"] != amount:
                    raise ReferralError("conflict")
                if existing["state"] != "held":
                    return {"status": "duplicate", "state": "duplicate",
                            "si_owner": si_owner, "op_key": op_key,
                            "amount": amount}
                # A held row is retried at its RECORDED amount, not
                # revalidated against the current policy amount:
                # fall through and re-check the caps below.
            else:
                # Fresh op: pin the amount to the policy OR to the
                # stored entitlement the op_key references.
                accepted = amount == self.grant_policy.referral_credits
                if not accepted:
                    ref = self._op_key_redemption_ref(op_key)
                    if ref is not None:
                        stored = db.execute(
                            "SELECT amount FROM referral_redemptions WHERE id=?",
                            (ref,)).fetchone()
                        accepted = stored is not None and stored["amount"] == amount
                if not accepted and role == "invitee":
                    # Completing this account's held redemption: its
                    # stored amount is an accepted entitlement too.
                    held_row = db.execute(
                        "SELECT amount FROM referral_redemptions "
                        "WHERE invitee_owner=? AND status='held'",
                        (si_owner,)).fetchone()
                    accepted = held_row is not None and held_row["amount"] == amount
                if not accepted:
                    raise ReferralError("amount_mismatch")
            # R3-SI-2: the invitee side must never double-credit.
            completes_held = None
            if role == "invitee":
                claim = db.execute(
                    "SELECT * FROM referral_redemptions WHERE invitee_owner=?",
                    (si_owner,)).fetchone()
                if claim is not None and claim["status"] == "completed":
                    return {"status": "duplicate", "state": "duplicate",
                            "si_owner": si_owner, "op_key": op_key,
                            "amount": amount}
                if claim is not None and claim["status"] == "held":
                    completes_held = claim
            if completes_held is None:
                held_reason = self._referral_hold_reason(
                    db, inviter_si_owner=si_owner if role == "inviter" else None,
                    total_amount=amount, side_amount=amount,
                    exclude_op_key=op_key)
                if held_reason is not None:
                    if existing is None:
                        db.execute(
                            "INSERT INTO referral_grant_inbox"
                            "(op_key,si_owner,amount,created_at,state) VALUES (?,?,?,?, 'held')",
                            (op_key, si_owner, amount, time.time()))
                    return {"status": "held", "state": "held",
                            "held_reason": held_reason, "si_owner": si_owner,
                            "op_key": op_key, "amount": amount}
            if existing is None:
                db.execute(
                    "INSERT INTO referral_grant_inbox"
                    "(op_key,si_owner,amount,created_at,state) VALUES (?,?,?,?, 'applied')",
                    (op_key, si_owner, amount, time.time()))
            else:
                db.execute("UPDATE referral_grant_inbox SET state='applied' "
                           "WHERE op_key=?", (op_key,))
            outcome = self._grant_referral_side(
                db, si_owner=si_owner, role=role,
                redemption_id=redemption_id, amount=amount)
            if outcome == "held":
                # The ledger-level invariants refused a grant the
                # precheck allowed: keep the op held for retry
                # instead of claiming it applied.
                db.execute("UPDATE referral_grant_inbox SET state='held' "
                           "WHERE op_key=?", (op_key,))
                return {"status": "held", "state": "held",
                        "held_reason": "grant_held", "si_owner": si_owner,
                        "op_key": op_key, "amount": amount}
            if completes_held is not None:
                db.execute(
                    "UPDATE referral_redemptions SET status='completed' "
                    "WHERE id=?", (completes_held["id"],))
                db.execute(
                    "INSERT OR IGNORE INTO referral_redemption_sides "
                    "VALUES (?,?,?,?)",
                    (completes_held["id"], "invitee", outcome, amount))
            elif role == "invitee":
                self._insert_cross_claim(db, si_owner)
        self._deliver_dplanet_ops(redemption_id)
        return {"status": "applied", "state": "applied", "si_owner": si_owner,
                "op_key": op_key, "amount": amount}

    # --- status / gifts ---------------------------------------------------

    def referral_status(self, owner):
        """The referral card's data: own code, redemption state,
        window state, and unseen gift notifications."""
        now = time.time()
        with self.transaction() as db:
            account = db.execute("SELECT created_at FROM accounts WHERE id=?",
                                 (owner,)).fetchone()
            if account is None:
                raise KeyError("Account not found")
            code = self._issue_code(db, owner)
            redemption = db.execute(
                "SELECT * FROM referral_redemptions WHERE invitee_owner=?",
                (owner,)).fetchone()
            redeemed = redemption is not None
            source = redemption["source"] if redemption is not None else None
            redemption_status = redemption["status"] if redemption is not None else None
            if not redeemed:
                prior = db.execute(
                    "SELECT code FROM registration_referrals WHERE invitee=?",
                    (owner,)).fetchone()
                if prior is not None and prior["code"]:
                    redeemed = True
                    source = "registration"
            open_, closes_at, grace_active = self._window_state(
                account["created_at"], now)
            gifts = db.execute(
                "SELECT * FROM referral_gifts WHERE owner=? AND seen_at IS NULL "
                "ORDER BY created_at", (owner,)).fetchall()
            return {
                "code": code,
                "amount": self.grant_policy.referral_credits,
                "redeemed": redeemed,
                "redemption_source": source,
                "redemption_status": redemption_status,
                "window": {"open": open_, "closes_at": closes_at,
                           "grace_active": grace_active,
                           "grace_unconfigured": self._grace_unconfigured(),
                           "window_seconds": self.grant_policy.referral_window_seconds},
                "unseen_gifts": [
                    {"id": row["id"], "amount": row["amount"],
                     "kind": row["kind"], "created_at": row["created_at"]}
                    for row in gifts],
            }

    def mark_referral_gifts_seen(self, owner, ids):
        """Mark gift notifications seen; returns how many were new."""
        if not isinstance(ids, (list, tuple)) or not ids \
                or any(not isinstance(value, str) or not value for value in ids):
            raise ValueError("Gift ids required")
        now = time.time()
        with self.transaction() as db:
            updated = 0
            for gift_id in ids:
                cursor = db.execute(
                    "UPDATE referral_gifts SET seen_at=? "
                    "WHERE id=? AND owner=? AND seen_at IS NULL",
                    (now, gift_id, owner))
                updated += cursor.rowcount
            return updated

    # --- daily monitoring ---------------------------------------------------

    def referral_daily_summary(self, day_iso):
        """Daily-watch shape (decision D5: no cap, monitoring instead).

        Per-inviter redemption counts and totals for one UTC day from
        referral_redemptions, plus ledger totals for the referral
        grant kinds. The ledger carries no timestamps, so its totals
        are all-time and labeled as such.
        """
        try:
            day = datetime.strptime(day_iso, "%Y-%m-%d").replace(
                tzinfo=timezone.utc)
        except (ValueError, TypeError):
            raise ValueError("day must be YYYY-MM-DD") from None
        start = day.timestamp()
        end = start + 86400
        with self.transaction() as db:
            rows = db.execute(
                "SELECT inviter_kind, inviter_si_owner, inviter_player_id, "
                "COUNT(*) AS n, COALESCE(SUM(amount),0) AS total "
                "FROM referral_redemptions WHERE created_at>=? AND created_at<? "
                "GROUP BY inviter_kind, inviter_si_owner, inviter_player_id "
                "ORDER BY n DESC", (start, end)).fetchall()
            per_inviter = [
                {"inviter_kind": row["inviter_kind"],
                 "inviter": row["inviter_si_owner"] or row["inviter_player_id"],
                 "count": row["n"], "amount_total": row["total"]}
                for row in rows]
            ledger = db.execute(
                "SELECT kind, COUNT(*) AS n, COALESCE(SUM(amount),0) AS total "
                "FROM ledger WHERE kind IN ('referral_inviter_v1','referral_invitee_v1') "
                "GROUP BY kind").fetchall()
            return {
                "day": day_iso,
                "redemptions": sum(entry["count"] for entry in per_inviter),
                "amount_total": sum(entry["amount_total"] for entry in per_inviter),
                "per_inviter": per_inviter,
                "ledger_totals_all_time": {
                    row["kind"]: {"count": row["n"], "amount_total": row["total"]}
                    for row in ledger},
            }
