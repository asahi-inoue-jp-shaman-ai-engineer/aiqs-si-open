import test from 'node:test';
import assert from 'node:assert/strict';
import { createAppAuth } from '../src/app-auth.js';
import {
  ensureFragmentHandoff, verifiedSharedCarryBinding,
  verifiedSharedSessionSnapshot,
} from '../src/account-session.js';

// Batch 2h regressions (Astra batch2g diff review, F1–F4):
//
// F1 (HIGH): after a fragment adoption the held refreshToken
// is null, so the supersession fence's token-equality clause
// — which compared only when a token was held — never fired.
// A session record copying the adoption tombstone's rev/sid,
// with any refresh token of its own, passed the fence and
// the fragment token kept being served under a session record
// it never belonged to. The clause is now unconditional: the
// shared state must still be exactly the tombstone (snapshot
// token null) for a fragment token to be served.
//
// F2 (MEDIUM): verifiedSharedSessionSnapshot and
// verifiedSharedCarryBinding were two judgment paths sharing
// parse helpers, free to disagree — and they did, on a
// legacy session record whose epoch companion is absent.
// Both are now projections of ONE derivation
// (deriveSharedState), which decides every shape once. For
// the companion-absent shape the single decision is: a valid
// unnamed session for the app (which holds the token and
// fences it by value), naming NO carry binding (so the
// binding projection is null and carries refuse it, exactly
// as they refuse the unverifiable). The matrix test below
// pins both projections for every shape, so a future second
// judgment path — any drift in either projection — fails.
//
// F3 (MEDIUM): persistRefreshToken treated a non-throwing
// setItem as a landed commit. A storage that silently ignores
// writes reported adoption success while nothing was
// persisted. Both commit writes (the session record and the
// clear tombstone) now verify by reading back.
//
// F4 (LOW): the access-only naming judged "unnamed" by the
// DERIVED revision/sid, so a legacy record with an epoch
// companion — which derives a non-null revision — slipped
// past and stayed unnamed forever. The judgment is now by
// the record's OWN fields (the snapshot's `unnamed` flag).

const SESSION_KEY = 'si-app-session-v1';
const EPOCH_KEY = 'si-app-session-epoch-v1';

const pendingNonce = (map) => ensureFragmentHandoff({
  getItem: (k) => map.get(k) ?? null,
  setItem: (k, v) => map.set(k, v),
  removeItem: (k) => map.delete(k),
});

function fixture({ responder, persistent } = {}) {
  const session = new Map();
  const persistentMap = new Map();
  const wrap = (map) => ({
    getItem: (k) => map.get(k) ?? null,
    setItem: (k, v) => map.set(k, v),
    removeItem: (k) => map.delete(k),
  });
  const location = {
    origin: 'https://asi.example.test', search: '', hash: '', pathname: '/app',
    assigned: null, assign(url) { this.assigned = url; },
  };
  const history = { replaced: [], replaceState(...args) { this.replaced.push(args); } };
  const fetch = async (url, options) => responder(url, options);
  const make = () => createAppAuth({
    fetch, location, history, storage: wrap(session),
    persistentStorage: persistent ?? wrap(persistentMap),
    crypto: globalThis.crypto,
  });
  return { make, session, persistent: persistentMap, location };
}

const tokenResponse = (data) => ({
  ok: true, status: 200, json: async () => data,
});

const noFetch = async () => { throw new Error('no token request expected'); };

function adoptFragmentTokenA(fx) {
  const auth = fx.make();
  const state = pendingNonce(fx.session);
  assert.ok(state);
  fx.location.hash = `#access_token=token-A&state=${state}`;
  assert.equal(auth.takeFragment(), true);
  return auth;
}

// --- F1 ---

test('batch2h F1: a session record copying the tombstone binding retires the fragment token', async () => {
  const fx = fixture({ responder: noFetch });
  const auth = adoptFragmentTokenA(fx);
  // Control: the untouched tombstone still serves token-A.
  assert.equal(await auth.getToken(), 'token-A');
  // Astra's counterexample, step for step: a session record
  // that copies the adoption tombstone's revision AND lineage
  // verbatim, carrying a refresh token of its own.
  const tombstone = JSON.parse(fx.persistent.get(EPOCH_KEY));
  fx.persistent.set(SESSION_KEY, JSON.stringify({
    refreshToken: 'refresh-foreign', rev: tombstone.rev,
    owner: 'tab-foreign', sid: tombstone.sid, savedAt: 1,
  }));
  let dispatched = 'unset';
  await auth.withCurrentToken((token) => { dispatched = token; });
  assert.equal(dispatched, null);
  assert.equal(await auth.getToken(), null);
});

test('batch2h F1: the fragment token is served only while the shared state is exactly its tombstone', async () => {
  const fx = fixture({ responder: noFetch });
  const auth = adoptFragmentTokenA(fx);
  assert.equal(await auth.getToken(), 'token-A');
  // A session record that copies only the lineage (no rev of
  // its own) borrows the same binding half-way: still a
  // session record where the tombstone stood, still retire.
  const tombstone = JSON.parse(fx.persistent.get(EPOCH_KEY));
  fx.persistent.set(SESSION_KEY, JSON.stringify({
    refreshToken: 'refresh-foreign', sid: tombstone.sid,
  }));
  assert.equal(await auth.getToken(), null);
});

// --- F2 ---

test('batch2h F2: one derivation — both projections pinned for every shape', () => {
  const store = (entries) => ({
    getItem: (k) => (k in entries ? entries[k] : null),
  });
  const J = JSON.stringify;
  // Confirmed absence on both surfaces.
  assert.deepEqual(verifiedSharedSessionSnapshot(store({})),
    { state: 'absent' });
  assert.equal(verifiedSharedCarryBinding(store({})), 'none');
  // THE shape the two judgment paths used to classify
  // separately: a legacy session record whose epoch
  // companion is absent. The one derivation's single
  // decision: a valid unnamed session (snapshot reports it,
  // rev and sid null) that names NO carry binding (binding
  // projection null — a carry cannot name a lineage that
  // does not exist, and the account surface cannot fence an
  // unnamed record by token value the way the app can).
  const legacyAlone = {
    [SESSION_KEY]: J({ refreshToken: 'refresh-a1', savedAt: 1 }),
  };
  assert.deepEqual(verifiedSharedSessionSnapshot(store(legacyAlone)), {
    state: 'record', refreshToken: 'refresh-a1', rev: null,
    sid: null, unnamed: true,
  });
  assert.equal(verifiedSharedCarryBinding(store(legacyAlone)), null);
  // The same record WITH its companion defers to the epoch's
  // revision on both surfaces, and reports itself unnamed.
  const legacyDeferred = {
    [SESSION_KEY]: J({ refreshToken: 'refresh-a1', savedAt: 1 }),
    [EPOCH_KEY]: J({ rev: 'rev-e', owner: 'tab-a' }),
  };
  assert.deepEqual(verifiedSharedSessionSnapshot(store(legacyDeferred)), {
    state: 'record', refreshToken: 'refresh-a1', rev: 'rev-e',
    sid: null, unnamed: true,
  });
  assert.deepEqual(verifiedSharedCarryBinding(store(legacyDeferred)),
    { rev: 'rev-e' });
  // Numeric legacy epoch: the derived revision is the
  // 'legacy:<n>' form on both surfaces.
  const legacyNumeric = {
    [SESSION_KEY]: J({ refreshToken: 'refresh-a1', savedAt: 1 }),
    [EPOCH_KEY]: J({ epoch: 3, owner: 'tab-a' }),
  };
  assert.deepEqual(verifiedSharedSessionSnapshot(store(legacyNumeric)), {
    state: 'record', refreshToken: 'refresh-a1', rev: 'legacy:3',
    sid: null, unnamed: true,
  });
  assert.deepEqual(verifiedSharedCarryBinding(store(legacyNumeric)),
    { rev: 'legacy:3' });
  // A malformed companion leaves the deferral unverifiable
  // on both surfaces too.
  const legacyBadCompanion = {
    [SESSION_KEY]: J({ refreshToken: 'refresh-a1', savedAt: 1 }),
    [EPOCH_KEY]: '{}',
  };
  assert.deepEqual(verifiedSharedSessionSnapshot(store(legacyBadCompanion)),
    { state: 'unverifiable' });
  assert.equal(verifiedSharedCarryBinding(store(legacyBadCompanion)), null);
  // A named record and a lone tombstone agree as well.
  const named = {
    [SESSION_KEY]: J({
      refreshToken: 'refresh-a1', rev: 'rev-1', owner: 'tab-a',
      sid: 'sid-1', savedAt: 1,
    }),
  };
  assert.deepEqual(verifiedSharedSessionSnapshot(store(named)), {
    state: 'record', refreshToken: 'refresh-a1', rev: 'rev-1',
    sid: 'sid-1', unnamed: false,
  });
  assert.deepEqual(verifiedSharedCarryBinding(store(named)), { sid: 'sid-1' });
  const tombstoneOnly = {
    [EPOCH_KEY]: J({
      rev: 'rev-t', owner: 'tab-a', kind: 'clear', sid: 'sid-t', at: 1,
    }),
  };
  assert.deepEqual(verifiedSharedSessionSnapshot(store(tombstoneOnly)), {
    state: 'record', refreshToken: null, rev: 'rev-t',
    sid: 'sid-t', unnamed: false,
  });
  assert.deepEqual(verifiedSharedCarryBinding(store(tombstoneOnly)),
    { sid: 'sid-t' });
});

test('batch2h F2: the companion-absent legacy record still loads, refreshes and is named', async () => {
  // The app side of the single F2 decision: the record is a
  // valid unnamed session. The first successful rotation
  // re-persists it in the atomic shape (the migration the
  // protocol always intended), after which it names a
  // binding like any other session.
  const fx = fixture({
    responder: async (url, options) => {
      const body = new URLSearchParams(options.body);
      if (body.get('grant_type') === 'refresh_token'
          && body.get('refresh_token') === 'refresh-legacy') {
        return tokenResponse({
          access_token: 'access-new', refresh_token: 'refresh-new',
          expires_in: 3600,
        });
      }
      return tokenResponse({});
    },
  });
  fx.persistent.set(SESSION_KEY,
    JSON.stringify({ refreshToken: 'refresh-legacy', savedAt: 1 }));
  const auth = fx.make();
  assert.equal(auth.hasPersistedSession(), true);
  assert.equal(await auth.getToken(), 'access-new');
  const stored = JSON.parse(fx.persistent.get(SESSION_KEY));
  assert.equal(stored.refreshToken, 'refresh-new');
  assert.equal(typeof stored.rev, 'string');
  assert.ok(stored.rev.length > 0);
  assert.equal(typeof stored.sid, 'string');
  assert.ok(stored.sid.length > 0);
});

// --- F3 ---

test('batch2h F3: a silently ignored session write fails the code-exchange adoption', async () => {
  // A storage whose setItem neither throws nor stores.
  const silent = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
  const fx = fixture({
    responder: async () => tokenResponse({
      access_token: 'access-1', refresh_token: 'refresh-1', expires_in: 3600,
    }),
    persistent: silent,
  });
  const auth = fx.make();
  fx.session.set('si-app-pkce-v1',
    JSON.stringify({ verifier: 'v'.repeat(32), state: 'state-1' }));
  fx.location.search = '?code=synthetic-code&state=state-1';
  // Astra's probe: completeRedirect() returned true while
  // nothing was persisted. The read-back makes the commit —
  // and therefore the adoption — fail instead.
  assert.equal(await auth.completeRedirect(), false);
  assert.equal(await auth.getToken(), null);
});

test('batch2h F3: a silently ignored tombstone write fails fragment adoption', async () => {
  const backing = new Map();
  const epochSilent = {
    getItem: (k) => backing.get(k) ?? null,
    setItem: (k, v) => { if (k !== EPOCH_KEY) backing.set(k, v); },
    removeItem: (k) => backing.delete(k),
  };
  const fx = fixture({ responder: noFetch, persistent: epochSilent });
  const auth = fx.make();
  const state = pendingNonce(fx.session);
  assert.ok(state);
  fx.location.hash = `#access_token=token-A&state=${state}`;
  // The clear cannot commit without its tombstone, so the
  // handed-over token is not installed.
  assert.equal(auth.takeFragment(), false);
  assert.equal(await auth.getToken(), null);
});

test('batch2h F3: a read-back that returns different bytes fails the commit', async () => {
  const backing = new Map();
  const corrupting = {
    getItem: (k) => backing.get(k) ?? null,
    setItem: (k, v) => backing.set(k, `${v} `),
    removeItem: (k) => backing.delete(k),
  };
  const fx = fixture({
    responder: async () => tokenResponse({
      access_token: 'access-1', refresh_token: 'refresh-1', expires_in: 3600,
    }),
    persistent: corrupting,
  });
  const auth = fx.make();
  fx.session.set('si-app-pkce-v1',
    JSON.stringify({ verifier: 'v'.repeat(32), state: 'state-1' }));
  fx.location.search = '?code=synthetic-code&state=state-1';
  assert.equal(await auth.completeRedirect(), false);
});

// --- F4 ---

test('batch2h F4: an access-only rotation names a legacy record that has an epoch companion', async () => {
  const fx = fixture({
    responder: async (url, options) => {
      const body = new URLSearchParams(options.body);
      if (body.get('grant_type') === 'refresh_token'
          && body.get('refresh_token') === 'refresh-legacy') {
        // Access-only rotation: no refresh token comes back.
        return tokenResponse({ access_token: 'access-new', expires_in: 3600 });
      }
      return tokenResponse({});
    },
  });
  fx.persistent.set(SESSION_KEY,
    JSON.stringify({ refreshToken: 'refresh-legacy', savedAt: 1 }));
  fx.persistent.set(EPOCH_KEY,
    JSON.stringify({ rev: 'rev-e', owner: 'tab-a', sid: 'sid-e' }));
  const auth = fx.make();
  assert.equal(await auth.getToken(), 'access-new');
  // The record itself now carries its own revision and
  // lineage: it was named by the rotation instead of deriving
  // the epoch's revision forever.
  const stored = JSON.parse(fx.persistent.get(SESSION_KEY));
  assert.equal(stored.refreshToken, 'refresh-legacy');
  assert.equal(typeof stored.rev, 'string');
  assert.ok(stored.rev.length > 0);
  assert.equal(typeof stored.sid, 'string');
  assert.ok(stored.sid.length > 0);
  // And the session keeps serving under its new name.
  assert.equal(await auth.getToken(), 'access-new');
});
