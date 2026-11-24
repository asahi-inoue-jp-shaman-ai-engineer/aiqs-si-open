import test from 'node:test';
import assert from 'node:assert/strict';
import {
  carryTokenToAccountLinks, createFragmentSession,
  verifiedSharedCarryBinding,
} from '../src/account-session.js';

// Batch 2f ROUND 3 regressions — the round-2 review's residue:
//
// 1. Timestamp fields accepted non-finite numbers (a raw
//    savedAt / at of 1e400 parses to Infinity): no writer
//    produces those, so they are outside the verified shapes.
// 2. A SESSION record carrying an `epoch` field was tolerated
//    by the validator while the lineage reader interpreted it
//    as the session's own revision — the verified derivation
//    deferred to the EPOCH record instead, the two named
//    different lineages, and the carried token died at its
//    first fence check. An accepted record must mean the same
//    lineage to every derivation, so the foreign field now
//    makes the record unverifiable.
// 3. (HIGH, residual) An adopted 'none' carry failed OPEN on
//    later storage-read failures: the runtime fence read the
//    lineage through a reader that collapses a throwing read
//    into the same null as confirmed absence, so the token
//    kept being served while the shared state was unreadable
//    (and had in fact moved). The fence now reads the lineage
//    WITH its readability and retires on unverifiable.
// Plus single-defect shape fixtures: the round-2 review noted
// several round-2 fixtures masked individual validations by
// carrying multiple defects at once.

const SESSION_KEY = 'si-app-session-v1';
const EPOCH_KEY = 'si-app-session-epoch-v1';

const recordB = {
  refreshToken: 'refresh-b1', rev: 'rev-b', owner: 'tab-b', sid: 'sid-b',
};

function accountFixture({
  seedSession = null, seedEpoch = null, makePersistentStorage = null,
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
  const storage = wrapMap(sessionMap);
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

// --- 1. Non-finite timestamps are outside the writer shapes ---

test('batch2f r3: non-finite timestamps never verify', () => {
  const fxSession = accountFixture({
    seedSession: '{"refreshToken":"r","rev":"r","sid":"A","owner":"t","savedAt":1e400}',
  });
  assert.equal(verifiedSharedCarryBinding(fxSession.persistentStorage), null);
  const fxEpoch = accountFixture({
    seedEpoch: '{"rev":"r","sid":"A","owner":"t","kind":"clear","at":1e400}',
  });
  assert.equal(verifiedSharedCarryBinding(fxEpoch.persistentStorage), null);
  // Finite timestamps still verify (control).
  const fxOk = accountFixture({
    seedSession: { refreshToken: 'r', rev: 'r', sid: 'A', owner: 't', savedAt: 1 },
  });
  assert.deepEqual(verifiedSharedCarryBinding(fxOk.persistentStorage),
    { sid: 'A' });
});

// --- Single-defect fixtures: each validation stands alone ---

test('batch2f r3: every present-key validation rejects on its own single defect', () => {
  const cases = [
    { seedSession: { refreshToken: 'r', rev: '' } },
    { seedSession: { refreshToken: 'r', rev: 'ok', sid: 7 } },
    { seedSession: { refreshToken: 'r', rev: 'ok', owner: '' } },
    { seedSession: { refreshToken: 'r', rev: 'ok', savedAt: 'now' } },
    { seedSession: { refreshToken: 'r', rev: 'ok', epoch: 2 } },
    { seedEpoch: { rev: 'r', at: 'bad' } },
    { seedEpoch: { rev: 'r', sid: '' } },
    { seedEpoch: { rev: 'r', owner: 42 } },
    { seedEpoch: { rev: 'r', kind: 'other' } },
    { seedEpoch: { rev: 'r', epoch: 3 } },
    { seedEpoch: { epoch: 3, sid: 'A' } },
    { seedEpoch: { epoch: 'three' } },
  ];
  for (const seeds of cases) {
    const fx = accountFixture(seeds);
    assert.equal(verifiedSharedCarryBinding(fx.persistentStorage), null,
      `expected unverifiable for ${JSON.stringify(seeds)}`);
  }
});

// --- 2. A SESSION record carrying the epoch field ---

test('batch2f r3: a session record carrying an epoch field is unverifiable, and no carry is written under it', () => {
  const fx = accountFixture({
    seedSession: { refreshToken: 'legacy', savedAt: 1, epoch: 2 },
    seedEpoch: { rev: 'r', owner: 't', kind: 'clear', sid: 'A', at: 1 },
  });
  assert.equal(verifiedSharedCarryBinding(fx.persistentStorage), null);
  assert.equal(carryTokenToAccountLinks(
    fx.document, 'tok-a', fx.storage, fx.persistentStorage), 0);
  assert.equal(fx.anchor.href, '/account/purchase');
});

// --- 3. The adopted 'none' carry retires when the shared state turns unreadable ---

test('batch2f r3: an adopted none-carry fails closed when later reads throw', async () => {
  const control = { throwReads: false };
  const fx = accountFixture({
    makePersistentStorage: (map) => ({
      getItem: (k) => {
        if (control.throwReads) throw new Error('storage unavailable');
        return map.get(k) ?? null;
      },
      setItem: (k, v) => map.set(k, v),
      removeItem: (k) => map.delete(k),
    }),
  });
  // Both shared keys absent: the carry is written under a
  // confirmed 'none' and adopted.
  assert.equal(carryTokenToAccountLinks(
    fx.document, 'tok-none', fx.storage, fx.persistentStorage), 1);
  const hash = fx.anchor.href.slice(fx.anchor.href.indexOf('#'));
  assert.ok(hash.includes('lstate=none'), hash);
  fx.location.hash = hash;
  const dest = createFragmentSession({ ...fx });
  assert.equal(await dest.getToken(), 'tok-none');
  // Another account signs in, and then the storage stops
  // answering: the token must retire, not keep being served
  // under an unreadable (in fact moved) state.
  fx.persistentMap.set(SESSION_KEY, JSON.stringify(recordB));
  control.throwReads = true;
  assert.equal(dest.getTokenSync(), null);
  assert.equal(await dest.getToken(), null);
});

test('batch2f r3: an adopted none-carry also retires when the shared state turns unparseable', async () => {
  const fx = accountFixture({});
  assert.equal(carryTokenToAccountLinks(
    fx.document, 'tok-none', fx.storage, fx.persistentStorage), 1);
  const hash = fx.anchor.href.slice(fx.anchor.href.indexOf('#'));
  fx.location.hash = hash;
  const dest = createFragmentSession({ ...fx });
  assert.equal(await dest.getToken(), 'tok-none');
  fx.persistentMap.set(SESSION_KEY, 'not-json{');
  assert.equal(dest.getTokenSync(), null);
  assert.equal(await dest.getToken(), null);
});
