"""Internal onboarding coordinator; not an HTTP endpoint or signature verifier.

Only a trusted adapter may call capture_verified_creation, after checking the
Supabase BEFORE-CREATE signature, freshness and configured source over the exact
body. evidence_digest is the SHA256 of that verified body, not proof by itself.
Only a server-side getUser adapter may call finalize_verified_user with a fresh,
successful response from the same configured provider. Never pass browser JSON.
No credentials, raw events, email addresses or editable metadata are retained.
"""
from datetime import datetime
import time
import uuid



class Onboarding:
    def __init__(self, store, *, issuer):
        store._admission_identity(issuer)
        self.store, self.issuer = store, issuer
        with store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS onboarding_receipts (
                issuer TEXT NOT NULL, subject TEXT NOT NULL,
                event_id TEXT NOT NULL,
                seal_digest TEXT NOT NULL, evidence_digest TEXT NOT NULL,
                referral_code TEXT, owner TEXT REFERENCES accounts(id),
                PRIMARY KEY(issuer,subject), UNIQUE(issuer,event_id))""")

    def _seal(self, db):
        row = db.execute('SELECT manifest_digest FROM identity_migrations WHERE issuer=?',
                         (self.issuer,)).fetchone()
        if row is None:
            raise ValueError('Migration reconciliation required')
        return row['manifest_digest']

    def capture_verified_creation(self, event, *, evidence_digest):
        """Freeze verified BEFORE-CREATE choice; identical delivery is idempotent.

        The SI marker selects this flow, but cannot establish newness on its own.
        No admission, account or reward exists until confirmed finalization.
        Unrelated provider signups are ignored without a receipt or balance write.
        """
        if not isinstance(event, dict) or not isinstance(event.get('user'), dict):
            raise ValueError('Verified creation user required')
        user = event['user']
        metadata = user.get('user_metadata')
        if metadata is None or (isinstance(metadata, dict) and 'si_registration' not in metadata):
            return {'status': 'ignored'}
        if not isinstance(metadata, dict) or metadata.get('si_registration') != 'v1':
            raise ValueError('Invalid SI registration marker')
        event_metadata = event.get('metadata')
        if (not isinstance(event_metadata, dict)
                or event_metadata.get('name') != 'before-user-created'
                or event_metadata.get('uuid') is None):
            raise ValueError('Verified before-user-created event required')
        event_id = event_metadata['uuid']
        self.store._admission_identity(self.issuer, event_id)
        subject = user.get('id')
        if subject is None or evidence_digest is None or user.get('is_anonymous') is not False:
            raise ValueError('Verified nonanonymous creation required')
        self.store._admission_identity(self.issuer, subject, evidence_digest)
        referral = metadata.get('referral_code')
        if referral is not None and (not isinstance(referral, str) or not 1 <= len(referral) <= 128):
            raise ValueError('Invalid registration referral')
        # Cross-system registration codes (fix round F-SI-2): a code
        # that does not resolve locally may be a D-Planet code.
        # Resolution happens BEFORE the write transaction so no
        # store lock is held across the remote call.
        cross_player = None
        if referral is not None:
            with self.store.transaction() as db:
                # Capture the CANONICAL stored code (round 3,
                # R3-SI-8a): the receipt, the provisioning and every
                # replay check downstream all carry the exact stored
                # string, never the registrant's raw variant.
                _local_owner, canonical = self.store._code_match(db, referral)
            if canonical is not None:
                referral = canonical
            elif _local_owner is None:
                cross_player = self.store._resolve_cross_player(referral)
        with self.store.transaction() as db:
            seal = self._seal(db)
            prior = db.execute('SELECT * FROM onboarding_receipts WHERE issuer=? AND subject=?',
                               (self.issuer, subject)).fetchone()
            if prior is not None:
                if (prior['event_id'], prior['seal_digest'], prior['evidence_digest'], prior['referral_code']) != (event_id, seal, evidence_digest, referral):
                    raise ValueError('Creation receipt conflict')
                return {'status': 'captured', 'subject': subject}
            if db.execute('SELECT 1 FROM onboarding_receipts WHERE issuer=? AND event_id=?',
                          (self.issuer, event_id)).fetchone():
                raise ValueError('Creation event already assigned')
            if (db.execute('SELECT 1 FROM identities WHERE issuer=? AND subject=?', (self.issuer, subject)).fetchone()
                    or db.execute('SELECT 1 FROM legacy_identity_imports WHERE issuer=? AND subject=?', (self.issuer, subject)).fetchone()
                    or db.execute('SELECT 1 FROM identity_admissions WHERE issuer=? AND subject=?', (self.issuer, subject)).fetchone()):
                raise ValueError('Existing identity requires reconciliation')
            if referral is not None and cross_player is None \
                    and self.store._code_owner(db, referral) is None:
                raise ValueError('Invalid registration referral')
            db.execute('INSERT INTO onboarding_receipts VALUES (?,?,?,?,?,?,NULL)',
                       (self.issuer, subject, event_id, seal, evidence_digest, referral))
            return {'status': 'captured', 'subject': subject}

    def finalize_verified_user(self, subject, user):
        """Provision from frozen receipt plus independently verified confirmed user.

        subject is the pending registration UUID, not a current browser identity.
        Comparing it to getUser rejects account switches. Metadata is never read.
        """
        if subject is None:
            raise ValueError('Pending registration identity required')
        self.store._admission_identity(self.issuer, subject)
        if (not isinstance(user, dict) or user.get('id') != subject
                or user.get('is_anonymous') is not False or user.get('role') != 'authenticated'):
            raise ValueError('Verified user does not match registration')
        value = user.get('email_confirmed_at')
        if not isinstance(value, str):
            raise ValueError('Verified email confirmation required')
        try:
            timestamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            raise ValueError('Invalid email confirmation timestamp') from None
        if timestamp.tzinfo is None:
            raise ValueError('Invalid email confirmation timestamp')
        # Cross-system registration codes (fix round F-SI-2): resolve
        # the frozen receipt's code against D-Planet BEFORE the
        # provisioning transaction when it does not resolve locally.
        cross_player = None
        pending_code = None
        try:
            with self.store.transaction() as db:
                row = db.execute('SELECT referral_code FROM onboarding_receipts '
                                 'WHERE issuer=? AND subject=?',
                                 (self.issuer, subject)).fetchone()
                pending_code = row['referral_code'] if row is not None else None
                local_owner = self.store._code_owner(db, pending_code) \
                    if pending_code is not None else None
        except Exception:
            pending_code, local_owner = None, None
        if pending_code is not None and local_owner is None:
            cross_player = self.store._resolve_cross_player(pending_code)
        redemption_id = None
        with self.store.transaction() as db:
            seal = self._seal(db)
            receipt = db.execute('SELECT * FROM onboarding_receipts WHERE issuer=? AND subject=?',
                                 (self.issuer, subject)).fetchone()
            if receipt is None or receipt['seal_digest'] != seal:
                raise ValueError('Matching sealed creation receipt required')
            admission = db.execute('SELECT disposition,evidence_digest FROM identity_admissions WHERE issuer=? AND subject=?',
                                   (self.issuer, subject)).fetchone()
            identity = db.execute('SELECT owner FROM identities WHERE issuer=? AND subject=?',
                                  (self.issuer, subject)).fetchone()
            if receipt['owner'] is not None:
                referral = db.execute('SELECT code FROM registration_referrals WHERE invitee=?', (receipt['owner'],)).fetchone()
                # Cross registrations store NULL in registration_referrals
                # (its code column REFERENCES invitation_codes); the match
                # is then proven by the dplanet redemption row (F-SI-2).
                referral_ok = referral is not None and referral['code'] == receipt['referral_code']
                if not referral_ok and referral is not None and referral['code'] is None \
                        and receipt['referral_code'] is not None:
                    referral_ok = db.execute(
                        "SELECT 1 FROM referral_redemptions WHERE invitee_owner=? "
                        "AND inviter_kind='dplanet' AND source='registration'",
                        (receipt['owner'],)).fetchone() is not None
                if (identity is None or identity['owner'] != receipt['owner'] or admission is None
                        or tuple(admission) != ('approved_new', receipt['evidence_digest'])
                        or not referral_ok):
                    raise ValueError('Finalized identity requires reconciliation')
                return {'status': 'provisioned', 'owner': receipt['owner']}
            if identity is not None or admission is not None or db.execute(
                    'SELECT 1 FROM legacy_identity_imports WHERE issuer=? AND subject=?', (self.issuer, subject)).fetchone():
                raise ValueError('Existing identity requires reconciliation')
            owner = str(uuid.uuid4())
            db.execute('INSERT INTO identity_admissions VALUES (?,?,?,?)',
                       (self.issuer, subject, 'approved_new', receipt['evidence_digest']))
            db.execute('INSERT INTO accounts(id,balance,created_at) VALUES (?,?,?)',
                       (owner, self.store.grant_policy.signup_campaign_credits, time.time()))
            db.execute('INSERT INTO identities VALUES (?,?,?)', (self.issuer, subject, owner))
            redemption_id = self.store._registration_referral(
                db, owner, receipt['referral_code'], cross_player_id=cross_player)
            db.execute('UPDATE onboarding_receipts SET owner=? WHERE issuer=? AND subject=?',
                       (owner, self.issuer, subject))
            result = {'status': 'provisioned', 'owner': owner}
        # Best-effort post-commit drain of any queued inviter grant
        # op (F-SI-2); failures stay queued for reconciliation.
        if redemption_id is not None:
            try:
                self.store._deliver_dplanet_ops(redemption_id)
            except Exception:
                pass
        return result
