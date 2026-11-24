import test from 'node:test';
import assert from 'node:assert/strict';
import { createAppAuth } from '../src/app-auth.js';
import {
  ensureFragmentHandoff, verifiedSharedSessionSnapshot,
} from '../src/account-session.js';

// Batch 2g regressions (Astra batch2f diff review, R1 app-side
// variant): the account pages' fence was unified onto the
// verified derivation in batch 2f, but the app (app-auth.js)
// still parsed the shared records itself, leniently — a stored
// 'null' literal or '{}' collapsed into "no record", and a
// session record carrying a lineage but no revision inherited
// the epoch record's revision. After a fragment adoption
// (which binds the token to the clear tombstone it wrote),
// each of those shapes left the retained token dispatchable
// under a binding the record never belonged to. These tests
// pin Astra's executed counterexample verbatim, plus the
// snapshot classification the fix routes through.

const SESSION_KEY = 'si-app-session-v1';
const EPOCH_KEY = 'si-app-session-epoch-v1';

const pendingNonce = (map) => ensureFragmentHandoff({
  getItem: (k) => map.get(k) ?? null,
  setItem: (k, v) => map.set(k, v),
  removeItem: (k) => map.delete(k),
});

function fixture({ responder } = {}) {
  const session = new Map();
  const persistent = new Map();
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
    persistentStorage: wrap(persistent), crypto: globalThis.crypto,
  });
  return { make, session, persistent, location };
}

const tokenResponse = (data) => ({
  ok: true, status: 200, json: async () => data,
});

const noFetch = async () => { throw new Error('no token request expected'); };

// Adopt fragment token-A exactly as Astra's counterexample
// does: a state-bound external handoff through takeFragment(),
// which clears the shared session and binds the token to the
// clear tombstone.
function adoptFragmentTokenA(fx) {
  const auth = fx.make();
  const state = pendingNonce(fx.session);
  assert.ok(state);
  fx.location.hash = `#access_token=token-A&state=${state}`;
  assert.equal(auth.takeFragment(), true);
  return auth;
}

test('batch2g R1: a stored null literal after fragment adoption retires the token', async () => {
  const fx = fixture({ responder: noFetch });
  const auth = adoptFragmentTokenA(fx);
  // Control: the untouched tombstone still serves token-A.
  assert.equal(await auth.getToken(), 'token-A');
  // Astra's counterexample, step for step.
  fx.persistent.set(SESSION_KEY, 'null');
  assert.deepEqual(verifiedSharedSessionSnapshot({
    getItem: (k) => fx.persistent.get(k) ?? null,
  }), { state: 'unverifiable' });
  let dispatched = 'unset';
  await auth.withCurrentToken((token) => { dispatched = token; });
  assert.equal(dispatched, null);
  assert.equal(await auth.getToken(), null);
});

test('batch2g R1: a stored {} after fragment adoption retires the token', async () => {
  const fx = fixture({ responder: noFetch });
  const auth = adoptFragmentTokenA(fx);
  assert.equal(await auth.getToken(), 'token-A');
  fx.persistent.set(SESSION_KEY, '{}');
  let dispatched = 'unset';
  await auth.withCurrentToken((token) => { dispatched = token; });
  assert.equal(dispatched, null);
  assert.equal(await auth.getToken(), null);
});

test('batch2g R1: a foreign sid-carrying session record retires the fragment token', async () => {
  const fx = fixture({ responder: noFetch });
  const auth = adoptFragmentTokenA(fx);
  assert.equal(await auth.getToken(), 'token-A');
  // The verified derivation derives sid-B for this record; the
  // lenient app derivation fell back to the tombstone's epoch
  // revision and dispatched token-A.
  fx.persistent.set(SESSION_KEY,
    JSON.stringify({ refreshToken: 'refresh-B', sid: 'sid-B' }));
  let dispatched = 'unset';
  await auth.withCurrentToken((token) => { dispatched = token; });
  assert.equal(dispatched, null);
  assert.equal(await auth.getToken(), null);
});

test('batch2g R1: a null literal also retires a loaded persisted session', async () => {
  const fx = fixture({
    responder: async (url, options) => {
      const body = new URLSearchParams(options.body);
      if (body.get('grant_type') === 'refresh_token'
          && body.get('refresh_token') === 'refresh-a1') {
        return tokenResponse({
          access_token: 'access-a', refresh_token: 'refresh-a2', expires_in: 3600,
        });
      }
      return tokenResponse({});
    },
  });
  fx.persistent.set(SESSION_KEY, JSON.stringify({
    refreshToken: 'refresh-a1', rev: 'rev-a', owner: 'tab-a',
    sid: 'sid-a', savedAt: 1,
  }));
  const auth = fx.make();
  assert.equal(await auth.getToken(), 'access-a');
  fx.persistent.set(SESSION_KEY, 'null');
  assert.equal(await auth.getToken(), null);
  let dispatched = 'unset';
  await auth.withCurrentToken((token) => { dispatched = token; });
  assert.equal(dispatched, null);
});

test('batch2g snapshot: classification matches the verified derivation', () => {
  const store = (entries) => ({
    getItem: (k) => (k in entries ? entries[k] : null),
  });
  // Confirmed absence.
  assert.deepEqual(verifiedSharedSessionSnapshot(store({})),
    { state: 'absent' });
  // Present-but-foreign shapes are unverifiable, never absent.
  assert.deepEqual(verifiedSharedSessionSnapshot(
    store({ [SESSION_KEY]: 'null' })), { state: 'unverifiable' });
  assert.deepEqual(verifiedSharedSessionSnapshot(
    store({ [SESSION_KEY]: '{}' })), { state: 'unverifiable' });
  assert.deepEqual(verifiedSharedSessionSnapshot(
    store({ [SESSION_KEY]: '[]' })), { state: 'unverifiable' });
  assert.deepEqual(verifiedSharedSessionSnapshot(
    store({ [EPOCH_KEY]: '{}' })), { state: 'unverifiable' });
  // A lineage-carrying record reports its OWN revision (here:
  // none) — the epoch tombstone's revision is never inherited.
  assert.deepEqual(verifiedSharedSessionSnapshot(store({
    [SESSION_KEY]: JSON.stringify({ refreshToken: 'refresh-B', sid: 'sid-B' }),
    [EPOCH_KEY]: JSON.stringify({
      rev: 'rev-tomb', owner: 'tab-a', kind: 'clear', sid: 'sid-a', at: 1,
    }),
  })), {
    state: 'record', refreshToken: 'refresh-B', rev: null, sid: 'sid-B',
    unnamed: false,
  });
  // A legacy session record defers its revision to the epoch,
  // and reports itself unnamed: the record's OWN fields carry
  // neither revision nor lineage (batch 2h, F4).
  assert.deepEqual(verifiedSharedSessionSnapshot(store({
    [SESSION_KEY]: JSON.stringify({ refreshToken: 'refresh-a1', savedAt: 1 }),
    [EPOCH_KEY]: JSON.stringify({ rev: 'rev-e', owner: 'tab-a', sid: 'sid-e' }),
  })), {
    state: 'record', refreshToken: 'refresh-a1', rev: 'rev-e', sid: null,
    unnamed: true,
  });
  // ... and with the epoch companion ABSENT the same record
  // still reports, unnamed (batch 2h, F2): the snapshot and
  // the carry binding are projections of one derivation, and
  // the derivation decides this shape once — a valid unnamed
  // session for the app (which fences it by token value),
  // naming no carry binding (the binding projection is null
  // for it, so the account surface refuses to carry under
  // it). What batch 2h removed is the second judgment path
  // that could classify the bytes differently, not the
  // classification.
  assert.deepEqual(verifiedSharedSessionSnapshot(store({
    [SESSION_KEY]: JSON.stringify({ refreshToken: 'refresh-a1', savedAt: 1 }),
  })), {
    state: 'record', refreshToken: 'refresh-a1', rev: null, sid: null,
    unnamed: true,
  });
  // A tombstone alone reports its revision and lineage.
  assert.deepEqual(verifiedSharedSessionSnapshot(store({
    [EPOCH_KEY]: JSON.stringify({
      rev: 'rev-tomb', owner: 'tab-a', kind: 'clear', sid: 'sid-a', at: 1,
    }),
  })), {
    state: 'record', refreshToken: null, rev: 'rev-tomb', sid: 'sid-a',
    unnamed: false,
  });
  // No storage at all: no snapshot exists.
  assert.equal(verifiedSharedSessionSnapshot(null), null);
});
