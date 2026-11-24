import test from 'node:test';
import assert from 'node:assert/strict';
import { createAppAuth } from '../src/app-auth.js';
import {
  armCarryBinding, armFragmentHandoff, carryTokenToAccountLinks,
  createFragmentSession, ensureFragmentHandoff,
  installCarriedLinkRefresh, readCarryBinding,
  readCarryBindingState, verifiedSharedCarryBinding,
} from '../src/account-session.js';

// Batch 2f regressions: the R1 handoff protocol rebuilt on a
// SINGLE storage record (Asahi's decision after batch 2e
// round 4 — patch-stitching ended, the state representation
// itself was redesigned).
//
// - The pending nonce and its carry expectation are one JSON
//   record (RECORD_KEY). Every transition is a single write,
//   or an ordered removal (legacy keys first, the record
//   last) whose failure leaves the whole previous state.
// - Absence is decided ONLY by a getItem result of
//   null/undefined; a stored '' or any other present value is
//   parsed and judged, never "nothing".
// - The shared session/epoch records are shape-validated:
//   a present record the protocol never writes ({} / [] / a
//   session record with no usable refresh token) is
//   UNVERIFIABLE, never confirmed absence ('none').
// - Legacy two-key state (pre-2f) is still read, strictly,
//   so a tab session armed by the previous release survives
//   the deploy boundary.
// - Contract 1 coverage: the clearPersistedSession probe
//   branch gains the retire pin round 4 showed was missing.

const SESSION_KEY = 'si-app-session-v1';
const EPOCH_KEY = 'si-app-session-epoch-v1';
const HANDOFF_KEY = 'si-fragment-handoff-v1';
const CARRY_KEY = 'si-fragment-handoff-carry-v1';
const RECORD_KEY = 'si-fragment-handoff-v2';

const recordA = {
  refreshToken: 'refresh-a1', rev: 'rev-a', owner: 'tab-0', sid: 'sid-a',
};
const recordB = {
  refreshToken: 'refresh-b1', rev: 'rev-b', owner: 'tab-b', sid: 'sid-b',
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

function carryHash(fx, token) {
  assert.equal(carryTokenToAccountLinks(
    fx.document, token, fx.storage, fx.persistentStorage), 1);
  return fx.anchor.href.slice(fx.anchor.href.indexOf('#'));
}

// --- Single-record atomicity ---

test('batch2f: a failed arm write arms nothing and the previous nonce+expectation stay together', async () => {
  const control = { failRecordWrite: false };
  const fx = accountFixture({
    seedSession: recordA,
    makeSessionStorage: (map) => ({
      getItem: (k) => map.get(k) ?? null,
      setItem: (k, v) => {
        if (control.failRecordWrite && k === RECORD_KEY) {
          throw new Error('record write blocked');
        }
        map.set(k, v);
      },
      removeItem: (k) => map.delete(k),
    }),
  });
  const hash = carryHash(fx, 'tok-a');
  const pending = ensureFragmentHandoff(fx.storage);
  assert.ok(pending);
  control.failRecordWrite = true;
  assert.equal(armFragmentHandoff(fx.storage), null);
  assert.equal(ensureFragmentHandoff(fx.storage), pending);
  control.failRecordWrite = false;
  // The pair is intact: the carried fragment still validates
  // whole under the old nonce.
  fx.location.hash = hash;
  const dest = createFragmentSession({ ...fx });
  assert.equal(await dest.getToken(), 'tok-a');
});

test('batch2f: consumption that cannot complete refuses the adoption and leaves the whole state', async () => {
  const control = { failRecordRemoval: false };
  const fx = accountFixture({
    seedSession: recordA,
    makeSessionStorage: (map) => ({
      getItem: (k) => map.get(k) ?? null,
      setItem: (k, v) => map.set(k, v),
      removeItem: (k) => {
        if (control.failRecordRemoval && k === RECORD_KEY) {
          throw new Error('record removal blocked');
        }
        map.delete(k);
      },
    }),
  });
  const hash = carryHash(fx, 'tok-a');
  control.failRecordRemoval = true;
  fx.location.hash = hash;
  const refused = createFragmentSession({ ...fx });
  assert.equal(await refused.getToken(), null);
  // Nothing was consumed: once the failure lifts, the same
  // link validates — the state was left whole, not half-eaten.
  control.failRecordRemoval = false;
  fx.location.hash = hash;
  const adopted = createFragmentSession({ ...fx });
  assert.equal(await adopted.getToken(), 'tok-a');
  // And now it IS consumed: a replay is refused.
  fx.location.hash = hash;
  const replay = createFragmentSession({ ...fx });
  assert.equal(await replay.getToken(), null);
});

test('batch2f: invalidation removes nonce and expectation together; a blocked removal leaves a consistent state', async () => {
  const control = { failRemovals: false };
  const fx = accountFixture({
    seedSession: recordA,
    makeSessionStorage: (map) => ({
      getItem: (k) => map.get(k) ?? null,
      setItem: (k, v) => map.set(k, v),
      removeItem: (k) => {
        if (control.failRemovals) throw new Error('removals blocked');
        map.delete(k);
      },
    }),
  });
  const nonce = ensureFragmentHandoff(fx.storage);
  fx.location.hash = `#access_token=tok-a&state=${nonce}`;
  const sessionA = createFragmentSession({ ...fx });
  assert.equal(sessionA.getTokenSync(), 'tok-a');
  const anchor = {
    href: '/account/purchase',
    getAttribute() { return this.href; },
    setAttribute(name, value) { this.href = value; },
    closest(selector) { return selector.startsWith('a[') ? this : null; },
  };
  const document = {
    querySelectorAll: () => [anchor],
    addEventListener: () => {},
  };
  const target = { addEventListener: () => {} };
  const refresh = installCarriedLinkRefresh({
    target, document, tokenProvider: sessionA.getToken,
    session: sessionA, storage: fx.storage,
    persistentStorage: fx.persistentStorage,
  });
  await refresh();
  assert.ok(anchor.href.includes('lineage=sid-a'), anchor.href);
  const retained = anchor.href.slice(anchor.href.indexOf('#'));
  // The session is replaced; the refresh invalidates. With
  // removals blocked, the handoff state survives WHOLE: the
  // retained fragment, claim stripped, is still refused (its
  // expectation is still recorded with its nonce).
  fx.persistentMap.set(SESSION_KEY, JSON.stringify(recordB));
  control.failRemovals = true;
  await refresh();
  fx.location.hash = retained.replace('&lineage=sid-a', '');
  const stripped = createFragmentSession({ ...fx });
  assert.equal(await stripped.getToken(), null);
  // Once removals work, invalidation kills the pair together:
  // even the intact retained fragment is dead.
  control.failRemovals = false;
  await refresh();
  fx.location.hash = retained;
  const dead = createFragmentSession({ ...fx });
  assert.equal(await dead.getToken(), null);
});

// --- Strict shapes for the handoff record itself ---

test('batch2f: malformed handoff records are invalid, never absent', async () => {
  const malformed = [
    '{"nonce":"n1"}',                                    // carry missing
    '{"nonce":"n1","carry":null,"extra":1}',             // extra key
    '{"nonce":"","carry":null}',                         // empty nonce
    '{"nonce":"n1","carry":{}}',                         // empty carry
    '{"nonce":"n1","carry":{"sid":"s","rev":"r"}}',      // two claims
    '{"nonce":"n1","carry":{"sid":""}}',                 // empty sid
    '{"nonce":"n1","carry":"sid-a"}',                    // wrong type
    '[]', '"nonce"', 'not-json{',
  ];
  for (const raw of malformed) {
    const fx = accountFixture({ seedSession: recordA });
    fx.sessionMap.set(RECORD_KEY, raw);
    fx.location.hash = '#access_token=tok-a&state=n1';
    const dest = createFragmentSession({ ...fx });
    assert.equal(await dest.getToken(), null, `record ${raw} must refuse`);
  }
});

// --- Absence is null/undefined only (contract 3b) ---

test('batch2f: a stored empty string is a present, invalid value — never absence', async () => {
  // Legacy nonce stored as ''.
  const fxNonce = accountFixture({ seedSession: recordA });
  fxNonce.sessionMap.set(HANDOFF_KEY, '');
  fxNonce.location.hash = '#access_token=tok-a&state=';
  const destNonce = createFragmentSession({ ...fxNonce });
  assert.equal(await destNonce.getToken(), null);
  // Legacy carry stored as '' next to a valid nonce: the
  // metadata is present and unparseable — not "no record".
  const fxCarry = accountFixture({ seedSession: recordA });
  fxCarry.sessionMap.set(HANDOFF_KEY, 'nonce-x');
  fxCarry.sessionMap.set(CARRY_KEY, '');
  fxCarry.location.hash = '#access_token=tok-a&state=nonce-x';
  const destCarry = createFragmentSession({ ...fxCarry });
  assert.equal(await destCarry.getToken(), null);
  // The single record stored as ''.
  const fxRecord = accountFixture({ seedSession: recordA });
  fxRecord.sessionMap.set(RECORD_KEY, '');
  fxRecord.location.hash = '#access_token=tok-a&state=nonce-x';
  const destRecord = createFragmentSession({ ...fxRecord });
  assert.equal(await destRecord.getToken(), null);
});

// --- Legacy two-key state: read strictly, survives the deploy boundary ---

test('batch2f: legacy state is adopted whole, and only whole', async () => {
  // A complete legacy pair (nonce + associated carry record)
  // with a matching claim and a standing shared binding.
  const fx = accountFixture({ seedSession: recordA });
  fx.sessionMap.set(HANDOFF_KEY, 'legacy-nonce');
  fx.sessionMap.set(CARRY_KEY,
    JSON.stringify({ sid: 'sid-a', nonce: 'legacy-nonce' }));
  fx.location.hash = '#access_token=tok-a&state=legacy-nonce&lineage=sid-a';
  const dest = createFragmentSession({ ...fx });
  assert.equal(await dest.getToken(), 'tok-a');
  // Consumed as one: the replay is refused.
  fx.location.hash = '#access_token=tok-a&state=legacy-nonce&lineage=sid-a';
  const replay = createFragmentSession({ ...fx });
  assert.equal(await replay.getToken(), null);

  // A legacy carry naming a DIFFERENT nonce is stale: it
  // counts as no expectation, so an external fragment is
  // adopted and a claimed one is refused.
  const stale = accountFixture({ seedSession: recordA });
  stale.sessionMap.set(HANDOFF_KEY, 'nonce-now');
  stale.sessionMap.set(CARRY_KEY,
    JSON.stringify({ sid: 'sid-a', nonce: 'nonce-dead' }));
  stale.location.hash = '#access_token=tok-b&state=nonce-now';
  const external = createFragmentSession({ ...stale });
  assert.equal(await external.getToken(), 'tok-b');
  const stale2 = accountFixture({ seedSession: recordA });
  stale2.sessionMap.set(HANDOFF_KEY, 'nonce-now');
  stale2.sessionMap.set(CARRY_KEY,
    JSON.stringify({ sid: 'sid-a', nonce: 'nonce-dead' }));
  stale2.location.hash = '#access_token=tok-b&state=nonce-now&lineage=sid-a';
  const claimed = createFragmentSession({ ...stale2 });
  assert.equal(await claimed.getToken(), null);

  // An orphaned legacy carry (no nonce at all) is invalid
  // metadata: arming replaces it wholesale and the fresh
  // external handoff proceeds.
  const orphan = accountFixture({ seedSession: recordA });
  orphan.sessionMap.set(CARRY_KEY,
    JSON.stringify({ sid: 'sid-a', nonce: 'nonce-dead' }));
  const fresh = ensureFragmentHandoff(orphan.storage);
  assert.ok(fresh);
  orphan.location.hash = `#access_token=tok-c&state=${fresh}`;
  const adopted = createFragmentSession({ ...orphan });
  assert.equal(await adopted.getToken(), 'tok-c');
});

// --- Shared-record shape validation (contract 2) ---

test('batch2f: verifiedSharedCarryBinding validates record shapes strictly', () => {
  const store = (session, epoch) => {
    const map = new Map();
    if (session !== undefined) map.set(SESSION_KEY, session);
    if (epoch !== undefined) map.set(EPOCH_KEY, epoch);
    return {
      getItem: (k) => map.get(k) ?? null,
      setItem: (k, v) => map.set(k, v),
      removeItem: (k) => map.delete(k),
    };
  };
  const J = JSON.stringify;
  // Confirmed absence is the ONLY 'none'.
  assert.equal(verifiedSharedCarryBinding(store()), 'none');
  // Present-but-unusable session records are unverifiable.
  assert.equal(verifiedSharedCarryBinding(store('{}')), null);
  assert.equal(verifiedSharedCarryBinding(store('[]')), null);
  assert.equal(verifiedSharedCarryBinding(store('')), null);
  assert.equal(verifiedSharedCarryBinding(store('not-json{')), null);
  assert.equal(verifiedSharedCarryBinding(store(J({ refreshToken: 42 }))), null);
  assert.equal(verifiedSharedCarryBinding(store(J({ refreshToken: '' }))), null);
  assert.equal(verifiedSharedCarryBinding(
    store(J({ refreshToken: 'r', rev: 42 }))), null);
  assert.equal(verifiedSharedCarryBinding(
    store(J({ refreshToken: 'r', sid: '' }))), null);
  // A session with no lineage of its own and no epoch record
  // is unverifiable — it exists, so it cannot be 'none'.
  assert.equal(verifiedSharedCarryBinding(store(J({ refreshToken: 'r' }))), null);
  // Valid shapes derive their bindings.
  assert.equal(verifiedSharedCarryBinding(
    store(J({ refreshToken: 'r', sid: 's1', rev: 'v1' }))).sid, 's1');
  assert.equal(verifiedSharedCarryBinding(
    store(J({ refreshToken: 'r', rev: 'v1' }))).rev, 'v1');
  assert.equal(verifiedSharedCarryBinding(
    store(J({ refreshToken: 'r', savedAt: 1 }), J({ epoch: 7, owner: 't' }))).rev,
    'legacy:7');
  // Epoch records: tombstones and legacy numeric forms.
  assert.equal(verifiedSharedCarryBinding(
    store(undefined, J({ rev: 'v9', owner: 't', kind: 'clear', sid: 's9' }))).sid,
    's9');
  assert.equal(verifiedSharedCarryBinding(
    store(undefined, J({ rev: 'v9' }))).rev, 'v9');
  assert.equal(verifiedSharedCarryBinding(
    store(undefined, J({ epoch: 3 }))).rev, 'legacy:3');
  assert.equal(verifiedSharedCarryBinding(store(undefined, '{}')), null);
  assert.equal(verifiedSharedCarryBinding(
    store(undefined, J({ epoch: '3' }))), null);
  assert.equal(verifiedSharedCarryBinding(
    store(undefined, J({ epoch: 3, sid: 's' }))), null);
  assert.equal(verifiedSharedCarryBinding(
    store(undefined, J({ rev: '' }))), null);
});

test('batch2f: a {} session record refuses a none-carry at the destination (contract 2 reproduction)', async () => {
  // Astra round-4 reproduction: SESSION_KEY holds {} — the
  // old verified derivation passed it through to 'none', so a
  // none-carry validated against a state that was never
  // confirmed absent.
  const fx = accountFixture();
  const hash = carryHash(fx, 'tok-a');
  assert.ok(hash.includes('lstate=none'), hash);
  fx.persistentMap.set(SESSION_KEY, '{}');
  fx.location.hash = hash;
  const dest = createFragmentSession({ ...fx });
  assert.equal(await dest.getToken(), null);
});

// --- The carrying side uses the verified derivation ---

test('batch2f: the carrying side writes no tokenized links while the shared state is unverifiable', async () => {
  const fx = accountFixture({ seedSession: '{}' });
  assert.equal(carryTokenToAccountLinks(
    fx.document, 'tok-a', fx.storage, fx.persistentStorage), 0);
  assert.equal(fx.anchor.href, '/account/purchase');
  const throwing = accountFixture({
    seedSession: recordA,
    makePersistentStorage: (map) => ({
      getItem: () => { throw new Error('persistent unreadable'); },
      setItem: (k, v) => map.set(k, v),
      removeItem: (k) => map.delete(k),
    }),
  });
  assert.equal(carryTokenToAccountLinks(
    throwing.document, 'tok-a', throwing.storage,
    throwing.persistentStorage), 0);
});

// --- Derived carry-state API over the single record ---

test('batch2f: readCarryBindingState and readCarryBinding derive from the one record', () => {
  const fx = accountFixture({ seedSession: recordA });
  const nonce = ensureFragmentHandoff(fx.storage);
  assert.deepEqual(readCarryBindingState(fx.storage, nonce),
    { state: 'absent' });
  assert.equal(readCarryBinding(fx.storage), null);
  assert.equal(armCarryBinding(fx.storage, { sid: 'sid-a' }), true);
  assert.deepEqual(readCarryBindingState(fx.storage, nonce),
    { state: 'record', binding: { sid: 'sid-a' } });
  assert.deepEqual(readCarryBinding(fx.storage), { sid: 'sid-a' });
  assert.deepEqual(readCarryBindingState(fx.storage, 'another-nonce'),
    { state: 'absent' });
  // A fresh arm retires the expectation in the same write.
  armFragmentHandoff(fx.storage);
  assert.equal(readCarryBinding(fx.storage), null);
});

// --- Contract 1 coverage: the probe branch retires ---

test('batch2f contract 1: a clear whose post-removal probe cannot verify absence retires the held session', async () => {
  const persistent = new Map();
  const control = { throwSessionReads: false };
  const persistentStorage = {
    getItem: (k) => {
      if (control.throwSessionReads && k === SESSION_KEY) {
        throw new Error('session record unreadable');
      }
      return persistent.get(k) ?? null;
    },
    setItem: (k, v) => persistent.set(k, v),
    removeItem: (k) => persistent.delete(k),
  };
  const session = new Map();
  const storage = {
    getItem: (k) => session.get(k) ?? null,
    setItem: (k, v) => session.set(k, v),
    removeItem: (k) => session.delete(k),
  };
  const location = {
    origin: 'https://asi.example.test', search: '', hash: '',
    pathname: '/app', assigned: null,
    assign(url) { this.assigned = url; },
  };
  const history = { replaceState: () => {} };
  const auth = createAppAuth({
    fetch: async () => ({ ok: true, status: 200, json: async () => ({}) }),
    location, history, storage, persistentStorage,
    crypto: globalThis.crypto,
  });
  const nonce1 = ensureFragmentHandoff(storage);
  location.hash = `#access_token=access-a&state=${nonce1}`;
  assert.equal(auth.takeFragment(), true);
  assert.equal(await auth.getToken(), 'access-a');
  // Session-record reads now throw. The tombstone write still
  // lands and the removal still works, so the clear reaches
  // exactly the PROBE branch: absence cannot be verified.
  control.throwSessionReads = true;
  const nonce2 = ensureFragmentHandoff(storage);
  location.hash = `#access_token=access-c&state=${nonce2}`;
  assert.equal(auth.takeFragment(), false);
  // Proof the probe branch was the one taken: a fresh clear
  // tombstone landed in the epoch record.
  const tombstone = JSON.parse(persistent.get(EPOCH_KEY));
  assert.equal(tombstone.kind, 'clear');
  // The held session was retired by that branch: neither the
  // promise API nor the synchronous dispatch fence may serve
  // the old account — its revision would otherwise match the
  // new tombstone's and pass the fence.
  assert.equal(await auth.getToken(), null);
  let dispatched = 'unset';
  await auth.withCurrentToken((token) => { dispatched = token; });
  assert.equal(dispatched, null);
});
