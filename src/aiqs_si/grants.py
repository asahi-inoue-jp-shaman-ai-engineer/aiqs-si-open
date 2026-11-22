"""Credit-grant family of the Store (OSS plan v4, Phase A §7).

Split out of store.py along the plan's lines. Every automatic grant
(migration award, referral pair) flows through _apply_grant, which
enforces the grant policy's invariants INSIDE the caller's
transaction: a per-inviter referral cap and a total grant budget.
When an invariant would be exceeded the grant is NOT applied and a
grant_holds row is recorded instead, so the held amount stays
visible for operator review instead of vanishing or over-granting.
With the policy caps unset (the default), behavior is exactly the
historical one. Amounts come from the grant policy
(policies.load_grant_policy), never from caller input.

The legacy loyalty replacement is an operator-invoked, receipt-
idempotent measure rather than an automatic grant, so it is kept
here structurally but is not budget-gated.
"""
import time
import uuid

# Ledger kinds that count against the total grant budget. Test
# fixtures ('test_grant') and purchases/settlements never count.
GRANT_LEDGER_KINDS = ("migration_award_v1", "referral_inviter_v1",
                      "referral_invitee_v1", "legacy_loyalty_gift")


class GrantStore:
    def _apply_grant(self, db, owner, kind, amount, *, inviter=None):
        """Credit one grant under the policy invariants.

        Returns True when credited. Returns False (and records a
        grant_holds row) when the per-inviter referral cap or the
        total grant budget would be exceeded; the caller's own
        operation (import, registration) still completes.
        """
        if type(amount) is not int or amount < 0:
            raise ValueError("Invalid grant amount")
        if amount == 0:
            return True
        policy = self.grant_policy
        if inviter is not None and policy.referral_cap_per_inviter is not None:
            earned = db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM ledger WHERE owner=? AND kind=?",
                (inviter, "referral_inviter_v1")).fetchone()[0]
            if earned + amount > policy.referral_cap_per_inviter:
                db.execute("INSERT INTO grant_holds(owner,kind,amount,inviter,reason,created_at) "
                           "VALUES (?,?,?,?,?,?)",
                           (owner, kind, amount, inviter, "inviter_cap", time.time()))
                return False
        if policy.total_budget is not None:
            spent = db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM ledger WHERE kind IN (%s)"
                % ",".join("?" * len(GRANT_LEDGER_KINDS)),
                GRANT_LEDGER_KINDS).fetchone()[0]
            if spent + amount > policy.total_budget:
                db.execute("INSERT INTO grant_holds(owner,kind,amount,inviter,reason,created_at) "
                           "VALUES (?,?,?,?,?,?)",
                           (owner, kind, amount, inviter, "total_budget", time.time()))
                return False
        db.execute("UPDATE accounts SET balance=balance+? WHERE id=?", (amount, owner))
        db.execute("INSERT INTO ledger(owner,kind,amount) VALUES (?,?,?)",
                   (owner, kind, amount))
        return True

    def grant_holds(self, owner=None):
        """Operator view of held (not applied) grants, newest first."""
        with self.transaction() as db:
            if owner is None:
                rows = db.execute("SELECT * FROM grant_holds ORDER BY id DESC").fetchall()
            else:
                rows = db.execute("SELECT * FROM grant_holds WHERE owner=? ORDER BY id DESC",
                                  (owner,)).fetchall()
            return [{"id": row["id"], "owner": row["owner"], "kind": row["kind"],
                     "amount": row["amount"], "inviter": row["inviter"],
                     "reason": row["reason"], "created_at": row["created_at"]}
                    for row in rows]

    def _migration_award(self, db, owner):
        if db.execute('SELECT 1 FROM migration_awards WHERE owner=?', (owner,)).fetchone():
            return
        # The award marker is written only when the grant lands, so a
        # held award stays retryable (a later import call re-enters
        # here) instead of being marked done while uncredited.
        if self._apply_grant(db, owner, 'migration_award_v1',
                             self.grant_policy.migration_credits):
            db.execute('INSERT INTO migration_awards VALUES (?)', (owner,))

    def invitation_code(self, owner):
        """Internal authenticated-account boundary; return one stable code.

        New codes use the 6-char referral format (referral.py);
        a code stored in the legacy token format is returned as-is
        and stays valid everywhere codes are looked up.
        """
        with self.transaction() as db:
            if not db.execute('SELECT 1 FROM accounts WHERE id=?', (owner,)).fetchone():
                raise KeyError('Account not found')
            return self._issue_code(db, owner)

    def regenerate_invitation_code(self, owner):
        """Replace the owner's invitation code (plan v4 §10, U7).

        Owner-only by construction (the caller passes the
        authenticated account id). The old code stops working
        immediately: validation looks codes up in invitation_codes,
        and the replaced code is kept only in the history table,
        which validation never consults. Accounts that already
        registered through the old code keep their referral record.
        """
        with self.transaction() as db:
            if not db.execute('SELECT 1 FROM accounts WHERE id=?', (owner,)).fetchone():
                raise KeyError('Account not found')
            prior = db.execute('SELECT code FROM invitation_codes WHERE owner=?', (owner,)).fetchone()
            code = self._generate_referral_code(db)
            if prior:
                db.execute('INSERT INTO invitation_code_history(owner,code,replaced_at) '
                           'VALUES (?,?,?)', (owner, prior['code'], time.time()))
                # registration_referrals.code REFERENCES
                # invitation_codes(code) (round 3, R3-SI-8b): the
                # child rows must move to the new code in the SAME
                # transaction, or replacing a code that was already
                # used at registration breaks the FK. FK checks are
                # deferred to commit so the parent and child updates
                # can land in either order within the transaction.
                db.execute('PRAGMA defer_foreign_keys=ON')
                db.execute('UPDATE invitation_codes SET code=? WHERE owner=?', (code, owner))
                db.execute('UPDATE registration_referrals SET code=? WHERE code=?',
                           (code, prior['code']))
            else:
                db.execute('INSERT INTO invitation_codes VALUES (?,?)', (owner, code))
            return code

    def _registration_referral(self, db, invitee, code, *, cross_player_id=None):
        """Registration-time referral, inside the provisioning txn.

        Returns the redemption id when a referral attached (the
        caller drains queued D-Planet ops for it after commit), else
        None.

        Fix round F-SI-2 routing:
        - Local code, inviter whose balance lives in D-Planet
          (credit_routing=dplanet): the inviter side is queued as a
          pending grant op (op_key si:referral:<id>:inviter) instead
          of a local grant, and NO SI gift row is written for the
          inviter (D-Planet notifies when the grant lands).
        - Cross code (cross_player_id resolved via D-Planet before
          provisioning): the redemption records inviter_kind
          'dplanet', the invitee is granted locally, the inviter op
          is queued, and only the invitee gift row is written.
        - Unlinked local inviter: the historical local behavior.
        A code resolving nowhere still fails registration.

        Round 3: the stored code is the canonical invitation code
        the input resolves to (R3-SI-8a), and the grant-policy hold
        precheck runs before any grant — a held referral records a
        'held' redemption with no gifts while registration itself
        succeeds (R3-SI-5b).
        """
        # Called only inside verified new-account provisioning's transaction.
        inviter = None
        canonical_code = None
        if code is not None:
            if not isinstance(code, str) or not 1 <= len(code) <= 128:
                raise ValueError('Invalid invitation code')
            if cross_player_id is None:
                # Persist the CANONICAL stored code, never the
                # registrant's raw input (round 3, R3-SI-8a): a
                # normalized variant resolves here, but storing it
                # verbatim would fail the registration_referrals FK
                # and roll the whole provisioning back.
                inviter, canonical_code = self._code_match(db, code)
                if inviter is None or inviter == invitee:
                    raise ValueError('Invalid invitation code')
        # registration_referrals.code REFERENCES invitation_codes, so
        # a cross-system code (not an SI code) is stored as NULL; the
        # redemption row below carries the code for cross referrals.
        db.execute('INSERT INTO registration_referrals VALUES (?,?)',
                   (invitee, None if cross_player_id is not None else canonical_code))
        if code is None:
            return None
        amount = self.grant_policy.referral_credits
        # Hold precheck (round 3, R3-SI-5b): registration used to
        # bypass the policy evaluation entirely and ignore
        # _apply_grant refusals. The same hold evaluation as redeem
        # now runs first; when held, the redemption is recorded with
        # status 'held', NO grants, NO queued ops and NO gift rows
        # are written, and registration itself still succeeds (the
        # held redemption can complete later via
        # resume_held_redemptions).
        redemption_id = str(uuid.uuid4())
        held_reason = self._referral_hold_reason(
            db, inviter_si_owner=inviter if cross_player_id is None else None,
            total_amount=2 * amount, side_amount=amount)
        if held_reason is not None:
            if cross_player_id is not None:
                return self._record_registration_referral(
                    db, invitee, None, code, amount, redemption_id=redemption_id,
                    inviter_kind="dplanet", inviter_player_id=cross_player_id,
                    inviter_gift=False, invitee_gift=False, status="held")
            return self._record_registration_referral(
                db, invitee, inviter, canonical_code, amount,
                redemption_id=redemption_id,
                inviter_gift=False, invitee_gift=False, status="held")
        if cross_player_id is not None:
            invitee_ok = self._apply_grant(db, invitee, 'referral_invitee_v1', amount)
            self._grant_referral_side(db, player_id=cross_player_id,
                                      role="inviter", redemption_id=redemption_id)
            self._record_registration_referral(
                db, invitee, None, code, amount, redemption_id=redemption_id,
                inviter_kind="dplanet", inviter_player_id=cross_player_id,
                invitee_gift=invitee_ok)
            return redemption_id
        link = self._routing_link(db, inviter)
        if link is not None:
            self._grant_referral_side(db, si_owner=inviter, role="inviter",
                                      redemption_id=redemption_id, gift=False)
            invitee_ok = self._apply_grant(db, invitee, 'referral_invitee_v1', amount)
            self._record_registration_referral(
                db, invitee, inviter, canonical_code, amount,
                redemption_id=redemption_id,
                inviter_gift=False, invitee_gift=invitee_ok)
            return redemption_id
        inviter_ok = self._apply_grant(db, inviter, 'referral_inviter_v1', amount,
                                       inviter=inviter)
        invitee_ok = self._apply_grant(db, invitee, 'referral_invitee_v1', amount)
        return self._record_registration_referral(
            db, invitee, inviter, canonical_code, amount,
            redemption_id=redemption_id,
            inviter_gift=inviter_ok, invitee_gift=invitee_ok)

    def legacy_replacement_receipt(self, owner):
        """The loyalty-gift replacement record (written by the L4 measure)."""
        with self.transaction() as db:
            row = db.execute("SELECT * FROM legacy_replacement_receipts WHERE owner=?",
                             (owner,)).fetchone()
            if row is None:
                return None
            return {"owner": row["owner"], "replaced_amount": row["replaced_amount"],
                    "gift_amount": row["gift_amount"], "created_at": row["created_at"]}

    def apply_legacy_replacement(self, owner, *, gift_amount=None):
        """Replace one account's SI balance with the flat loyalty gift.

        The old balance is removed (ledger legacy_replaced) and the
        gift credited (ledger legacy_loyalty_gift) in the same
        transaction as the receipt, so the measure can never apply
        twice to the same account. The gift amount defaults to the
        grant policy's loyalty gift value.
        """
        if gift_amount is None:
            gift_amount = self.grant_policy.legacy_loyalty_gift_credits
        if type(gift_amount) is not int or gift_amount < 0:
            raise ValueError("Invalid loyalty gift amount")
        with self.transaction() as db:
            account = db.execute("SELECT balance FROM accounts WHERE id=?", (owner,)).fetchone()
            if account is None:
                raise KeyError("Account not found")
            receipt = db.execute("SELECT * FROM legacy_replacement_receipts WHERE owner=?",
                                 (owner,)).fetchone()
            if receipt is not None:
                return {"owner": owner, "status": "already_replaced",
                        "replaced_amount": receipt["replaced_amount"],
                        "gift_amount": receipt["gift_amount"]}
            replaced = account["balance"]
            db.execute("UPDATE accounts SET balance=? WHERE id=?", (gift_amount, owner))
            db.execute("INSERT INTO ledger(owner,kind,amount) VALUES (?,?,?)",
                       (owner, "legacy_replaced", -replaced))
            db.execute("INSERT INTO ledger(owner,kind,amount) VALUES (?,?,?)",
                       (owner, "legacy_loyalty_gift", gift_amount))
            db.execute("INSERT INTO legacy_replacement_receipts VALUES (?,?,?,?)",
                       (owner, replaced, gift_amount, time.time()))
            return {"owner": owner, "status": "replaced",
                    "replaced_amount": replaced, "gift_amount": gift_amount}

    def legacy_replacement_candidates(self):
        """Accounts imported from the legacy snapshot — the measure's scope."""
        with self.transaction() as db:
            return [row["account_id"] for row in db.execute(
                "SELECT account_id FROM legacy_identity_imports ORDER BY account_id").fetchall()]

    def apply_legacy_replacements(self, *, excluded_owners=(),
                                  gift_amount=None, dry_run=False):
        """Batch form of the replacement measure over the imported accounts.

        excluded_owners is the operator exclusion list, passed
        explicitly by the caller (the operator's own account is never
        replaced). dry_run reports the plan — who would be replaced,
        with what amounts — without writing anything.
        """
        if gift_amount is None:
            gift_amount = self.grant_policy.legacy_loyalty_gift_credits
        if type(gift_amount) is not int or gift_amount < 0:
            raise ValueError("Invalid loyalty gift amount")
        excluded = frozenset(excluded_owners)
        results = []
        for owner in self.legacy_replacement_candidates():
            if owner in excluded:
                results.append({"owner": owner, "status": "excluded"})
            elif dry_run:
                with self.transaction() as db:
                    account = db.execute("SELECT balance FROM accounts WHERE id=?",
                                         (owner,)).fetchone()
                    receipt = db.execute(
                        "SELECT * FROM legacy_replacement_receipts WHERE owner=?",
                        (owner,)).fetchone()
                if receipt is not None:
                    results.append({"owner": owner, "status": "already_replaced",
                                    "replaced_amount": receipt["replaced_amount"],
                                    "gift_amount": receipt["gift_amount"]})
                else:
                    results.append({"owner": owner, "status": "would_replace",
                                    "replaced_amount": account["balance"],
                                    "gift_amount": gift_amount})
            else:
                results.append(self.apply_legacy_replacement(owner, gift_amount=gift_amount))
        totals = {"replaced": 0, "already_replaced": 0, "excluded": 0,
                  "replaced_amount": 0, "gift_total": 0}
        for entry in results:
            if entry["status"] in ("replaced", "would_replace"):
                totals["replaced"] += 1
                totals["replaced_amount"] += entry["replaced_amount"]
                totals["gift_total"] += entry["gift_amount"]
            elif entry["status"] in totals:
                totals[entry["status"]] += 1
        return {"dry_run": dry_run, "gift_amount": gift_amount,
                "results": results, "totals": totals}
