import test from 'node:test';
import assert from 'node:assert/strict';
import {
  armCarryBinding, armFragmentHandoff, carryTokenToAccountLinks,
  createFragmentSession, ensureFragmentHandoff,
  installCarriedLinkRefresh, readCarryBinding, readCarryBindingState,
  verifiedSharedCarryBinding,
} from '../src/account-session.js';

// Batch 2f ROUND 2 regressions — the three defects the round-1
// adversarial review reproduced against the redesign:
//
// 1. (HIGH) Legacy consumption / invalidation deleted the
//    carry expectation before the nonce, so a failed nonce
//    removal left the nonce armed WITHOUT its expectation and
//    a claim-stripped fragment was adopted unchecked. The fix
//    migrates a valid legacy pair into the single record
//    BEFORE deleting anything, and never partially deletes an
//    unverifiable state.
// 2. (MEDIUM) Shared-record validation accepted present
//    records outside the writer shapes (null identity fields,
//    unvalidated epoch metadata, mixed revision variants).
//    Recognized fields are now validated whenever present and
//    the two epoch variants never mix.
// 3. (MEDIUM) A legacy SESSION backed by a sid-bearing epoch
//    tombstone derived a {sid} verified binding while the
//    session-lineage reader reports {rev, sid: null} for the
//    same storage, so the carried token retired immediately.
//    The deferral now carries the epoch's REVISION only.
// Plus the coverage pins the review asked for: invalid-state
// classification asserted directly (not via an API that can
// rearm), invalid-v2 precedence over valid legacy state,
// stored-state assertions after invalidation, failed bind
// writes, and the click handler exercised directly.

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
  const persistentStorage = wrapMap(persistentMap);
  const location = {
    origin: 'https://asi.example.test', search: '', hash: '',
    pathname: '/account/link',
  };
  const history = { replaceState: () => {} };
  const anchor = {
    href: '/account/purchase',
    getAttribute() { return this.href; },
    setAttribute(name, value) { this.href = value; },
    closest(selector) { return selector.startsWith('a[') ? this : null; },
  };
  const document = { querySelectorAll: () => [anchor] };
  return {
    storage, sessionMap, persistentStorage, persistentMap,
    location, history, anchor, document,
  };
}

function seedLegacyPair(fx, nonce = 'legacy-n', sid = 'sid-a') {
  fx.sessionMap.set(HANDOFF_KEY, nonce);
  fx.sessionMap.set(CARRY_KEY, JSON.stringify({ sid, nonce }));
}

function refreshRig(fx, sessionA) {
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
  return { refresh, anchor };
}

// --- Finding 1: legacy removal can no longer split the pair ---

test('batch2f r2: legacy consumption migrates before deleting — a stuck nonce removal cannot shed the expectation', async () => {
  const control = { failHandoffRemoval: false };
  const fx = accountFixture({
    seedSession: recordA,
    makeSessionStorage: (map) => ({
      getItem: (k) => map.get(k) ?? null,
      setItem: (k, v) => map.set(k, v),
      removeItem: (k) => {
        if (control.failHandoffRemoval && k === HANDOFF_KEY) {
          throw new Error('nonce removal blocked');
        }
        map.delete(k);
      },
    }),
  });
  seedLegacyPair(fx);
  const hash = '#access_token=tok-a&state=legacy-n&lineage=sid-a';
  control.failHandoffRemoval = true;
  fx.location.hash = hash;
  const refused = createFragmentSession({ ...fx });
  assert.equal(await refused.getToken(), null);
  // The pair survived WHOLE, migrated into the single record:
  // nonce and expectation are still together.
  const migrated = JSON.parse(fx.sessionMap.get(RECORD_KEY));
  assert.equal(migrated.nonce, 'legacy-n');
  assert.deepEqual(migrated.carry, { sid: 'sid-a' });
  // Once removals work, the intact fragment validates whole.
  control.failHandoffRemoval = false;
  fx.location.hash = hash;
  const adopted = createFragmentSession({ ...fx });
  assert.equal(await adopted.getToken(), 'tok-a');

  // The round-1 reproduction, on a fresh fixture: fail the
  // consumption once, restore removals, then present the
  // fragment with its claim STRIPPED. Had the failed removal
  // shed the expectation, this would now be adopted as an
  // unchecked external handoff; it must be refused.
  const fx2 = accountFixture({
    seedSession: recordA,
    makeSessionStorage: (map) => ({
      getItem: (k) => map.get(k) ?? null,
      setItem: (k, v) => map.set(k, v),
      removeItem: (k) => {
        if (control.failHandoffRemoval && k === HANDOFF_KEY) {
          throw new Error('nonce removal blocked');
        }
        map.delete(k);
      },
    }),
  });
  seedLegacyPair(fx2);
  control.failHandoffRemoval = true;
  fx2.location.hash = hash;
  const refused2 = createFragmentSession({ ...fx2 });
  assert.equal(await refused2.getToken(), null);
  control.failHandoffRemoval = false;
  fx2.location.hash = '#access_token=tok-a&state=legacy-n';
  const stripped = createFragmentSession({ ...fx2 });
  assert.equal(await stripped.getToken(), null);
});

test('batch2f r2: when the migration write cannot land, legacy consumption deletes nothing', async () => {
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
  seedLegacyPair(fx);
  control.failRecordWrite = true;
  fx.location.hash = '#access_token=tok-a&state=legacy-n&lineage=sid-a';
  const refused = createFragmentSession({ ...fx });
  assert.equal(await refused.getToken(), null);
  // Nothing was deleted and nothing was written: the legacy
  // pair stands exactly as it was.
  assert.equal(fx.sessionMap.get(HANDOFF_KEY), 'legacy-n');
  assert.ok(fx.sessionMap.has(CARRY_KEY));
  assert.equal(fx.sessionMap.has(RECORD_KEY), false);
  control.failRecordWrite = false;
  fx.location.hash = '#access_token=tok-a&state=legacy-n&lineage=sid-a';
  const adopted = createFragmentSession({ ...fx });
  assert.equal(await adopted.getToken(), 'tok-a');
});

test('batch2f r2: invalidation of a legacy pair removes it whole, and only whole', async () => {
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
  // A fragment session adopted under A (external fragment).
  const nonce0 = ensureFragmentHandoff(fx.storage);
  fx.location.hash = `#access_token=tok-a&state=${nonce0}`;
  const sessionA = createFragmentSession({ ...fx });
  assert.equal(sessionA.getTokenSync(), 'tok-a');
  const { refresh } = refreshRig(fx, sessionA);
  // A pre-2f tab's armed legacy pair coexists in the store.
  seedLegacyPair(fx);
  // The session is superseded; invalidation runs but the
  // migration write cannot land: the pair must be left WHOLE
  // (its expectation still refuses a claim-stripped fragment).
  fx.persistentMap.set(SESSION_KEY, JSON.stringify(recordB));
  control.failRecordWrite = true;
  await refresh();
  assert.equal(fx.sessionMap.get(HANDOFF_KEY), 'legacy-n');
  assert.ok(fx.sessionMap.has(CARRY_KEY));
  fx.location.hash = '#access_token=tok-x&state=legacy-n';
  const stripped = createFragmentSession({ ...fx });
  assert.equal(await stripped.getToken(), null);
  // Once the write lands, invalidation removes the pair
  // together — asserted on the stored state itself.
  control.failRecordWrite = false;
  await refresh();
  assert.equal(fx.sessionMap.has(HANDOFF_KEY), false);
  assert.equal(fx.sessionMap.has(CARRY_KEY), false);
  assert.equal(fx.sessionMap.has(RECORD_KEY), false);
  // Isolation: restore the shared binding to A. If the pair
  // had survived, the intact fragment would now be adopted;
  // only its deletion explains the refusal.
  fx.persistentMap.set(SESSION_KEY, JSON.stringify(recordA));
  fx.location.hash = '#access_token=tok-x&state=legacy-n&lineage=sid-a';
  const dead = createFragmentSession({ ...fx });
  assert.equal(await dead.getToken(), null);
});

test('batch2f r2: invalidation leaves no handoff keys behind (stored-state assertion)', async () => {
  const fx = accountFixture({ seedSession: recordA });
  const nonce = ensureFragmentHandoff(fx.storage);
  fx.location.hash = `#access_token=tok-a&state=${nonce}`;
  const sessionA = createFragmentSession({ ...fx });
  assert.equal(sessionA.getTokenSync(), 'tok-a');
  const { refresh, anchor } = refreshRig(fx, sessionA);
  await refresh();
  const retained = anchor.href.slice(anchor.href.indexOf('#'));
  assert.ok(retained.includes('lineage=sid-a'), retained);
  fx.persistentMap.set(SESSION_KEY, JSON.stringify(recordB));
  await refresh();
  assert.equal(fx.sessionMap.has(RECORD_KEY), false);
  assert.equal(fx.sessionMap.has(HANDOFF_KEY), false);
  assert.equal(fx.sessionMap.has(CARRY_KEY), false);
  // Restore A: only the deletion explains the refusal now.
  fx.persistentMap.set(SESSION_KEY, JSON.stringify(recordA));
  fx.location.hash = retained;
  const dead = createFragmentSession({ ...fx });
  assert.equal(await dead.getToken(), null);
});

// --- Classification pins, asserted without a rearming API ---

test('batch2f r2: invalid states classify as invalid directly, and an invalid record outranks a valid legacy pair', async () => {
  // Legacy carry stored as '' next to a valid nonce.
  const fxCarry = accountFixture({ seedSession: recordA });
  fxCarry.sessionMap.set(HANDOFF_KEY, 'nonce-x');
  fxCarry.sessionMap.set(CARRY_KEY, '');
  assert.equal(
    readCarryBindingState(fxCarry.storage, 'nonce-x').state, 'invalid');
  // The single record stored as ''.
  const fxRecord = accountFixture({ seedSession: recordA });
  fxRecord.sessionMap.set(RECORD_KEY, '');
  assert.equal(
    readCarryBindingState(fxRecord.storage, 'nonce-x').state, 'invalid');
  // An orphaned legacy carry.
  const fxOrphan = accountFixture({ seedSession: recordA });
  fxOrphan.sessionMap.set(CARRY_KEY,
    JSON.stringify({ sid: 'sid-a', nonce: 'nonce-dead' }));
  assert.equal(
    readCarryBindingState(fxOrphan.storage, 'nonce-dead').state, 'invalid');
  // An invalid v2 record takes precedence over a complete,
  // valid legacy pair behind it: no fallback, no adoption.
  const fxMixed = accountFixture({ seedSession: recordA });
  fxMixed.sessionMap.set(RECORD_KEY, '{"nonce":"n1","carry":"sid-a"}');
  seedLegacyPair(fxMixed);
  assert.equal(
    readCarryBindingState(fxMixed.storage, 'legacy-n').state, 'invalid');
  fxMixed.location.hash = '#access_token=tok-a&state=legacy-n&lineage=sid-a';
  const dest = createFragmentSession({ ...fxMixed });
  assert.equal(await dest.getToken(), null);
});

// --- Finding 2: shared-record shapes, validated whenever present ---

test('batch2f r2: present-but-misshapen shared records are unverifiable, never a binding', () => {
  const cases = [
    // Epoch metadata outside the writer shapes.
    { seedEpoch: { rev: 'r', sid: 'A', owner: 42, kind: [], at: 'bad' } },
    { seedEpoch: '{"epoch":3,"sid":null,"owner":42}' },
    { seedEpoch: '{"rev":null,"epoch":3}' },
    { seedEpoch: { rev: 'r', kind: 'other' } },
    { seedEpoch: { epoch: 3, sid: 'A' } },
    // Null / mistyped identity fields on the session record.
    { seedSession: { refreshToken: 'r', sid: 'A', rev: null, owner: null } },
    { seedSession: { refreshToken: 'r', owner: 42 } },
    { seedSession: { refreshToken: 'r', sid: '' } },
    // A legacy session deferring to a misshapen epoch record.
    {
      seedSession: { refreshToken: 'legacy', savedAt: 1 },
      seedEpoch: { rev: 'r', owner: 42 },
    },
  ];
  for (const seeds of cases) {
    const fx = accountFixture(seeds);
    assert.equal(verifiedSharedCarryBinding(fx.persistentStorage), null,
      `expected unverifiable for ${JSON.stringify(seeds)}`);
  }
  // The writer shapes themselves still verify.
  const tomb = accountFixture({
    seedEpoch: { rev: 'r', owner: 'tab-0', kind: 'clear', sid: 'A', at: 1 },
  });
  assert.deepEqual(verifiedSharedCarryBinding(tomb.persistentStorage),
    { sid: 'A' });
  const numeric = accountFixture({ seedEpoch: { epoch: 7, owner: 'tab-0' } });
  assert.deepEqual(verifiedSharedCarryBinding(numeric.persistentStorage),
    { rev: 'legacy:7' });
});

// --- Finding 3: legacy session + sid-bearing tombstone ---

test('batch2f r2: a legacy session backed by a sid-bearing tombstone carries the epoch revision, and the token lives', async () => {
  const fx = accountFixture({
    seedSession: { refreshToken: 'legacy', savedAt: 1 },
    seedEpoch: { rev: 'r', sid: 'A', owner: 'tab-9', kind: 'clear', at: 1 },
  });
  // The verified binding is the revision the lineage reader
  // reports for this storage — not the tombstone's sid.
  assert.deepEqual(verifiedSharedCarryBinding(fx.persistentStorage),
    { rev: 'r' });
  assert.equal(carryTokenToAccountLinks(
    fx.document, 'tok-a', fx.storage, fx.persistentStorage), 1);
  const hash = fx.anchor.href.slice(fx.anchor.href.indexOf('#'));
  assert.ok(hash.includes('lrev=r'), hash);
  assert.ok(!hash.includes('lineage='), hash);
  fx.location.hash = hash;
  const dest = createFragmentSession({ ...fx });
  assert.equal(await dest.getToken(), 'tok-a');
  // Not retired on the next fence check either.
  assert.equal(await dest.getToken(), 'tok-a');
});

// --- Failed bind writes and the click handler, exercised directly ---

test('batch2f r2: a bind write that cannot land binds nothing', () => {
  const control = { failRecordWrite: true };
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
  const nonce = armFragmentHandoff(fx.storage);
  assert.equal(nonce, null); // arming itself is the same write
  // With a standing nonce, a failing bind reports failure.
  control.failRecordWrite = false;
  const armed = armFragmentHandoff(fx.storage);
  assert.ok(armed);
  control.failRecordWrite = true;
  assert.equal(armCarryBinding(fx.storage, { sid: 'sid-a' }), false);
  // No binding was recorded, and the nonce itself survived.
  assert.equal(readCarryBinding(fx.storage), null);
  assert.equal(ensureFragmentHandoff(fx.storage), armed);
  // The carrying helper writes no links when its bind fails.
  assert.equal(carryTokenToAccountLinks(
    fx.document, 'tok-a', fx.storage, fx.persistentStorage), 0);
  assert.equal(fx.anchor.href, '/account/purchase');
});

test('batch2f r2: the click handler refuses to carry under an unverifiable shared state', async () => {
  const fx = accountFixture({ seedSession: recordA });
  let clickHandler = null;
  const document = {
    querySelectorAll: () => [fx.anchor],
    addEventListener: (type, fn) => { if (type === 'click') clickHandler = fn; },
  };
  const target = { addEventListener: () => {} };
  const refresh = installCarriedLinkRefresh({
    target, document, tokenProvider: async () => 'tok-a',
    storage: fx.storage, persistentStorage: fx.persistentStorage,
  });
  await refresh();
  assert.ok(fx.anchor.href.includes('access_token=tok-a'), fx.anchor.href);
  // The shared state becomes unverifiable; a click must not
  // re-bind the carry under the unverified derivation — the
  // anchors are stripped instead.
  fx.persistentMap.set(SESSION_KEY, '{}');
  clickHandler({ target: fx.anchor });
  assert.equal(fx.anchor.href, '/account/purchase');
});
