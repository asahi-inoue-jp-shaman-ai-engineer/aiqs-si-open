import test from 'node:test';
import assert from 'node:assert/strict';
import {
  armCarryBinding, armFragmentHandoff, carryTokenToAccountLinks,
  createFragmentSession, ensureFragmentHandoff, readCarryBindingState,
  takeFragmentHandoff,
} from '../src/account-session.js';

// Batch 2i regressions (Astra batch2h diff review, blockers
// 1–2;案A4):
//
// Blocker 1 (HIGH): removeHandoffState treated a non-throwing
// removeItem as consumption. Under a storage whose removals
// are silently ignored, the handoff record survived adoption,
// so the same fragment could be adopted AGAIN after the
// shared state had been replaced by another account — the
// replacement's session was retired and the old token served
// indefinitely. Consumption is now verified by reading every
// key back, in an order that confirms each removal before
// the next key is touched (round 2): verifying only after
// all removals still let a silently ignored legacy-nonce
// removal surface after the record was already gone,
// leaving the nonce bare and separated from its expectation.
//
// Blocker 2 (HIGH): the account supply fence compared only
// the lineage projection ({rev, sid}) of the shared state.
// A session record copying the adopted tombstone's rev/sid
// with a foreign refresh token passed it, and the fragment
// token kept being served under a session it never belonged
// to — F1's account-side counterpart (the app side closed
// its half in batch 2h). The fence now binds the token to
// the shared state's whole identity from ONE derivation:
// a tombstone-bound token requires the state to still be
// exactly that tombstone, and a session-bound token retires
// when the token changes under an unchanged revision (a
// genuine rotation moves revision and token together and
// keeps the lineage, so it never trips the clause).
//
// Sweep fix (same class as batch 2h F3): writeHandoffState
// reported a landed arm without reading the record back.
// The handoff record write is a commit and is now verified
// the same way.

const SESSION_KEY = 'si-app-session-v1';
const EPOCH_KEY = 'si-app-session-epoch-v1';
const HANDOFF_KEY = 'si-fragment-handoff-v1';
const CARRY_KEY = 'si-fragment-handoff-carry-v1';
const RECORD_KEY = 'si-fragment-handoff-v2';

const recordA = {
  refreshToken: 'refresh-a1', rev: 'rev-a', owner: 'tab-0', sid: 'sid-a',
};

function accountFixture({
  seedSession = null, seedEpoch = null, makeSessionStorage = null,
  makePersistentStorage = null,
} = {}) {
  const sessionMap = new Map();
  const persistentMap = new Map();
  if (seedSession !== null) {
    persistentMap.set(SESSION_KEY,
      typeof seedSession === 'string' ? seedSession : JSON.stringify(seedSession));
  }
  if (seedEpoch !== null) {
    persistentMap.set(EPOCH_KEY,
      typeof seedEpoch === 'string' ? seedEpoch : JSON.stringify(seedEpoch));
  }
  const wrapMap = (map) => ({
    getItem: (k) => map.get(k) ?? null,
    setItem: (k, v) => map.set(k, v),
    removeItem: (k) => map.delete(k),
  });
  const storage = makeSessionStorage
    ? makeSessionStorage(sessionMap) : wrapMap(sessionMap);
  const persistentStorage = makePersistentStorage
    ? makePersistentStorage(persistentMap) : wrapMap(persistentMap);
  const location = {
    origin: 'https://asi.example.test', search: '', hash: '',
    pathname: '/account/link',
  };
  const history = { replaceState: () => {} };
  const anchor = {
    href: '/account/purchase',
    getAttribute() { return this.href; },
    setAttribute(name, value) { this.href = value; },
  };
  const document = { querySelectorAll: () => [anchor] };
  return {
    storage, sessionMap, persistentStorage, persistentMap,
    location, history, anchor, document,
  };
}

function externalAdoption(fx, token = 'token-A') {
  const nonce = ensureFragmentHandoff(fx.storage);
  assert.ok(nonce);
  fx.location.hash = `#access_token=${token}&state=${nonce}`;
  return createFragmentSession({ ...fx });
}

// --- Blocker 1: verified consumption ---

test('batch2i B1: a silently ignored removal fails the adoption, nonce intact', async () => {
  const fx = accountFixture({
    makeSessionStorage: (map) => ({
      getItem: (k) => map.get(k) ?? null,
      setItem: (k, v) => map.set(k, v),
      // The defect's storage: removal neither throws nor removes.
      removeItem: () => {},
    }),
  });
  const nonce = ensureFragmentHandoff(fx.storage);
  assert.ok(nonce);
  fx.location.hash = `#access_token=token-A&state=${nonce}`;
  // Astra's reproduction, step for step: the old code adopted
  // here (returned the token) while the nonce record survived.
  const handoff = takeFragmentHandoff(
    fx.location, fx.history, fx.storage, fx.persistentStorage);
  assert.equal(handoff, null);
  // The whole state is left in place: the same nonce is still
  // the pending one, so nothing was half-consumed.
  assert.equal(ensureFragmentHandoff(fx.storage), nonce);
  assert.ok(fx.sessionMap.has(RECORD_KEY));
  const session = createFragmentSession({ ...fx });
  assert.equal(await session.getToken(), null);
});

test('batch2i B1: a removal ignored for the record key alone still fails consumption', () => {
  const fx = accountFixture({
    makeSessionStorage: (map) => ({
      getItem: (k) => map.get(k) ?? null,
      setItem: (k, v) => map.set(k, v),
      removeItem: (k) => { if (k !== RECORD_KEY) map.delete(k); },
    }),
  });
  const nonce = ensureFragmentHandoff(fx.storage);
  assert.ok(nonce);
  fx.location.hash = `#access_token=token-A&state=${nonce}`;
  assert.equal(takeFragmentHandoff(
    fx.location, fx.history, fx.storage, fx.persistentStorage), null);
  assert.equal(ensureFragmentHandoff(fx.storage), nonce);
});

test('batch2i B1 (round 2): an ignored legacy-nonce removal never leaves the nonce bare', async () => {
  // Astra batch 2i round 1 residual, step for step: verifying
  // only AFTER all removals detected a silently ignored
  // legacy-nonce removal too late — the record was already
  // deleted and the surviving nonce stood separated from its
  // expectation, readable as "absent" and replayable against a
  // later account's session. Consumption now confirms each
  // removal before touching the next key, nonce key first.
  const sessionMap = new Map([
    [HANDOFF_KEY, 'nonce-A'],
    [CARRY_KEY, JSON.stringify({ nonce: 'nonce-A', sid: 'sid-a' })],
  ]);
  const persistentMap = new Map([
    [SESSION_KEY, JSON.stringify(recordA)],
  ]);
  const wrapMap = (map) => ({
    getItem: (k) => map.get(k) ?? null,
    setItem: (k, v) => map.set(k, v),
    removeItem: (k) => map.delete(k),
  });
  const storage = wrapMap(sessionMap);
  // Removals silently ignore ONLY the legacy nonce key.
  storage.removeItem = (k) => { if (k !== HANDOFF_KEY) sessionMap.delete(k); };
  const persistentStorage = wrapMap(persistentMap);
  const location = {
    origin: 'https://asi.example.test', search: '',
    pathname: '/account/link',
    hash: ['#access_token=token-A', 'state=nonce-A', 'lineage=sid-a'].join('&'),
  };
  const history = { replaceState: () => {} };
  assert.equal(takeFragmentHandoff(
    location, history, storage, persistentStorage), null);
  // The nonce is still bound to its expectation (inside the
  // migrated record) — NOT the bare "absent" the round-1 shape
  // left behind.
  assert.deepEqual(readCarryBindingState(storage, 'nonce-A'),
    { state: 'record', binding: { sid: 'sid-a' } });
  // Install account B and restore functioning removals: the
  // still-bound claim cannot adopt A over B, and B's session
  // record is untouched.
  persistentMap.set(SESSION_KEY, JSON.stringify({
    refreshToken: 'refresh-b1', rev: 'rev-b', owner: 'tab-9', sid: 'sid-b',
  }));
  storage.removeItem = (k) => sessionMap.delete(k);
  assert.equal(takeFragmentHandoff(
    location, history, storage, persistentStorage), null);
  assert.equal(
    JSON.parse(persistentMap.get(SESSION_KEY)).refreshToken, 'refresh-b1');
});

test('batch2i B1 (round 2): an ignored record removal keeps the nonce bound inside the record', () => {
  const fx = accountFixture({
    seedSession: recordA,
    makeSessionStorage: (map) => ({
      getItem: (k) => map.get(k) ?? null,
      setItem: (k, v) => map.set(k, v),
      removeItem: (k) => { if (k !== RECORD_KEY) map.delete(k); },
    }),
  });
  const nonce = ensureFragmentHandoff(fx.storage);
  assert.ok(nonce);
  assert.equal(armCarryBinding(fx.storage, { sid: 'sid-a' }), true);
  fx.location.hash = `#access_token=<redacted>&state=${nonce}&lineage=sid-a`;
  assert.equal(takeFragmentHandoff(
    fx.location, fx.history, fx.storage, fx.persistentStorage), null);
  // Aborted at the record step: the record still binds the
  // nonce to its expectation.
  assert.deepEqual(readCarryBindingState(fx.storage, nonce),
    { state: 'record', binding: { sid: 'sid-a' } });
});

test('batch2i B1 (round 2): an ignored expectation removal leaves nothing replayable', () => {
  // The expectation lives under its own key only in the legacy
  // pair shape (in the v2 record it is embedded, and comes down
  // with the record). Consumption aborts at the expectation
  // step — after both nonce carriers are confirmed gone — so
  // the surviving expectation is inert.
  const sessionMap = new Map([
    [HANDOFF_KEY, 'nonce-A'],
    [CARRY_KEY, JSON.stringify({ nonce: 'nonce-A', sid: 'sid-a' })],
  ]);
  const persistentMap = new Map([
    [SESSION_KEY, JSON.stringify(recordA)],
  ]);
  const wrapMap = (map) => ({
    getItem: (k) => map.get(k) ?? null,
    setItem: (k, v) => map.set(k, v),
    removeItem: (k) => map.delete(k),
  });
  const storage = wrapMap(sessionMap);
  storage.removeItem = (k) => { if (k !== CARRY_KEY) sessionMap.delete(k); };
  const persistentStorage = wrapMap(persistentMap);
  const location = {
    origin: 'https://asi.example.test', search: '',
    pathname: '/account/link',
    hash: ['#access_token=token-A', 'state=nonce-A', 'lineage=sid-a'].join('&'),
  };
  const history = { replaceState: () => {} };
  assert.equal(takeFragmentHandoff(
    location, history, storage, persistentStorage), null);
  assert.equal(sessionMap.has(RECORD_KEY), false);
  assert.equal(sessionMap.has(HANDOFF_KEY), false);
  // The orphaned expectation reads as invalid — present but
  // unverifiable metadata, never as a bindable state.
  assert.deepEqual(readCarryBindingState(storage, 'nonce-A'),
    { state: 'invalid' });
  assert.equal(takeFragmentHandoff(
    location, history, storage, persistentStorage), null);
});

test('batch2i B1: a confirmed consumption is single-use — the same fragment cannot replay', async () => {
  const fx = accountFixture({ seedSession: recordA });
  const session = externalAdoption(fx);
  assert.equal(await session.getToken(), 'token-A');
  // Replay: the identical fragment presented again finds no
  // pending record and is refused.
  const nonce = 'replay-attempt';
  fx.location.hash = `#access_token=token-A&state=${nonce}`;
  assert.equal(takeFragmentHandoff(
    fx.location, fx.history, fx.storage, fx.persistentStorage), null);
});

// --- Blocker 2: the fence compares the whole identity ---

const tombstone = {
  rev: 'rev-t', owner: 'tab-a', kind: 'clear', sid: 'sid-t', at: 1,
};

function carriedUnderTombstone() {
  const fx = accountFixture({ seedEpoch: tombstone });
  assert.equal(carryTokenToAccountLinks(
    fx.document, 'token-A', fx.storage, fx.persistentStorage), 1);
  fx.location.hash = fx.anchor.href.slice(fx.anchor.href.indexOf('#'));
  return fx;
}

test('batch2i B2: control — the untouched tombstone still serves the carried token', async () => {
  const fx = carriedUnderTombstone();
  const session = createFragmentSession({ ...fx });
  assert.equal(await session.getToken(), 'token-A');
});

test('batch2i B2: a session record copying the tombstone rev/sid retires the token', async () => {
  const fx = carriedUnderTombstone();
  const session = createFragmentSession({ ...fx });
  assert.equal(await session.getToken(), 'token-A');
  // Astra's counterexample, step for step: a foreign session
  // record copying the tombstone's revision AND lineage,
  // carrying a refresh token of its own.
  fx.persistentMap.set(SESSION_KEY, JSON.stringify({
    refreshToken: 'refresh-b1', rev: 'rev-t', owner: 'tab-x',
    sid: 'sid-t', savedAt: 1,
  }));
  assert.equal(await session.getToken(), null);
  assert.equal(session.getTokenSync(), null);
});

test('batch2i B2: a session record copying only the tombstone lineage retires the token', async () => {
  const fx = carriedUnderTombstone();
  const session = createFragmentSession({ ...fx });
  assert.equal(await session.getToken(), 'token-A');
  // No revision of its own: the lineage half matches, the
  // state is nevertheless no longer the tombstone.
  fx.persistentMap.set(SESSION_KEY, JSON.stringify({
    refreshToken: 'refresh-b1', sid: 'sid-t',
  }));
  assert.equal(await session.getToken(), null);
});

test('batch2i B2: a genuine rotation of the bound session keeps serving', async () => {
  const fx = accountFixture({ seedSession: recordA });
  const session = externalAdoption(fx);
  assert.equal(await session.getToken(), 'token-A');
  // Rotation: revision and token move together in one
  // commit, lineage kept — the app tab's routine refresh
  // must never kill an open account page.
  fx.persistentMap.set(SESSION_KEY, JSON.stringify({
    refreshToken: 'refresh-a2', rev: 'rev-a2', owner: 'tab-0', sid: 'sid-a',
  }));
  assert.equal(await session.getToken(), 'token-A');
});

test('batch2i B2: a token swap under the copied revision retires the token', async () => {
  const fx = accountFixture({ seedSession: recordA });
  const session = externalAdoption(fx);
  assert.equal(await session.getToken(), 'token-A');
  // Same revision, same lineage, different token: the record
  // was replaced in place, not rotated.
  fx.persistentMap.set(SESSION_KEY, JSON.stringify({
    refreshToken: 'refresh-b1', rev: 'rev-a', owner: 'tab-0', sid: 'sid-a',
  }));
  assert.equal(await session.getToken(), null);
});

test('batch2i B2: a token swap on an unnamed legacy record retires the token', async () => {
  const fx = accountFixture({
    seedSession: { refreshToken: 'refresh-l1', savedAt: 1 },
    seedEpoch: { rev: 'rev-e', owner: 'tab-a' },
  });
  const session = externalAdoption(fx);
  assert.equal(await session.getToken(), 'token-A');
  // Two unnamed records share the derived revision; only the
  // token value tells this session from its replacement.
  fx.persistentMap.set(SESSION_KEY,
    JSON.stringify({ refreshToken: 'refresh-l2', savedAt: 1 }));
  assert.equal(await session.getToken(), null);
});

test('batch2i B2: a record appearing where none was captured retires the token', async () => {
  const fx = accountFixture();
  const session = externalAdoption(fx);
  assert.equal(await session.getToken(), 'token-A');
  // An unnamed record names no binding, so the lineage
  // projection alone would see no change at all; the
  // classification comparison retires the capture.
  fx.persistentMap.set(SESSION_KEY,
    JSON.stringify({ refreshToken: 'refresh-x', savedAt: 1 }));
  assert.equal(await session.getToken(), null);
});

// --- Sweep fix: the handoff record write is verified ---

test('batch2i sweep: a silently ignored handoff write arms nothing', () => {
  const silent = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
  assert.equal(armFragmentHandoff(silent), null);
  assert.equal(ensureFragmentHandoff(silent), null);
});

test('batch2i sweep: a corrupted handoff write arms nothing and records no carry', () => {
  const control = { corrupt: false };
  const fx = accountFixture({
    seedSession: recordA,
    makeSessionStorage: (map) => ({
      getItem: (k) => map.get(k) ?? null,
      setItem: (k, v) => map.set(k, control.corrupt ? `${v} ` : v),
      removeItem: (k) => map.delete(k),
    }),
  });
  const nonce = ensureFragmentHandoff(fx.storage);
  assert.ok(nonce);
  control.corrupt = true;
  // The expectation write cannot be verified, so the carry
  // refuses to record — it must not write token-bearing
  // hrefs under an expectation that does not exist.
  assert.equal(armCarryBinding(fx.storage, { sid: 'sid-a' }), false);
  assert.equal(armFragmentHandoff(fx.storage), null);
});
