import test from 'node:test';
import assert from 'node:assert/strict';
import { createAppAuth } from '../src/app-auth.js';
import {
  armCarryBinding, armFragmentHandoff, carryTokenToAccountLinks,
  createFragmentSession, ensureFragmentHandoff,
  installCarriedLinkRefresh,
} from '../src/account-session.js';

// Batch 2e ROUND 4 regressions (Astra round-3 residuals, closed
// under the D-017 exception — scope is exactly these four):
//
// HIGH-1: a FAILED fragment adoption must retire the session
// this instance already holds. Round 3 rebound the retained
// credentials to the surviving epoch; when that survivor was
// another account's tombstone, the revision fence accepted the
// old account as current under the foreign lineage.
// HIGH-2: the destination's current binding is tri-state —
// confirmed absence, a valid binding, or UNVERIFIABLE — and an
// unverifiable state refuses the carry. The session binds to
// the VERIFIED snapshot, never to a second independent read.
// HIGH-3: invalidating a carry revokes its nonce as well, and
// carry metadata that is unreadable or corrupt refuses the
// adoption instead of counting as an external handoff. The
// recorded-carry equality check gains the pin round 3 showed
// was missing (its removal passed every supplied test).
// MEDIUM-4: arming a fresh external nonce retires the previous
// carry expectation, fail-closed when the retirement cannot
// complete; reusing the pending nonce preserves it.

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

// --- HIGH-1: two app tabs over one shared store ---

function twoTabFixture() {
  const persistentMap = new Map();
  const controlA = { failWrites: false };
  const wrapTab = (control) => ({
    getItem: (k) => persistentMap.get(k) ?? null,
    setItem: (k, v) => {
      if (control.failWrites) throw new Error('storage blocked');
      persistentMap.set(k, v);
    },
    removeItem: (k) => {
      if (control.failWrites) throw new Error('storage blocked');
      persistentMap.delete(k);
    },
  });
  const makeTab = (control) => {
    const sessionMap = new Map();
    const storage = {
      getItem: (k) => sessionMap.get(k) ?? null,
      setItem: (k, v) => sessionMap.set(k, v),
      removeItem: (k) => sessionMap.delete(k),
    };
    const location = {
      origin: 'https://asi.example.test', search: '', hash: '',
      pathname: '/app', assigned: null,
      assign(url) { this.assigned = url; },
    };
    const history = { replaceState: () => {} };
    const auth = createAppAuth({
      fetch: async () => ({ ok: true, status: 200, json: async () => ({}) }),
      location, history, storage,
      persistentStorage: wrapTab(control), crypto: globalThis.crypto,
    });
    return { auth, storage, sessionMap, location };
  };
  return {
    persistentMap, controlA,
    tabA: makeTab(controlA), tabB: makeTab({ failWrites: false }),
  };
}

test('batch2e r4 HIGH-1: a failed adoption retires the existing session instead of rebinding it to the foreign tombstone', async () => {
  // Astra round-3 reproduction: A adopts fragment A, B adopts
  // fragment B, A's persistent writes become unavailable, and
  // A attempts another valid state-bound adoption. The clear
  // fails; round 3 rebound A's retained credentials to B's
  // tombstone, so A's fence accepted them as current.
  const { controlA, tabA, tabB } = twoTabFixture();
  const adopt = (tab, token) => {
    const nonce = ensureFragmentHandoff(tab.storage);
    assert.ok(nonce);
    tab.location.hash = `#access_token=${token}&state=${nonce}`;
    return tab.auth.takeFragment();
  };
  assert.equal(adopt(tabA, 'access-a'), true);
  assert.equal(await tabA.auth.getToken(), 'access-a');
  assert.equal(adopt(tabB, 'access-b'), true);
  // A's writes become unavailable. A arms a fresh nonce in its
  // own tab session and attempts the adoption of C.
  controlA.failWrites = true;
  const nonceC = ensureFragmentHandoff(tabA.storage);
  assert.ok(nonceC);
  tabA.location.hash = `#access_token=access-c&state=${nonceC}`;
  assert.equal(tabA.auth.takeFragment(), false);
  // The failed adoption retired A's session: neither the
  // promise API nor the synchronous dispatch fence may serve
  // the old account's token any more.
  assert.equal(await tabA.auth.getToken(), null);
  let dispatched = 'unset';
  await tabA.auth.withCurrentToken((token) => { dispatched = token; });
  assert.equal(dispatched, null);
  // B is unaffected.
  assert.equal(await tabB.auth.getToken(), 'access-b');
});

// --- account-page fixtures ---

function accountFixture({
  seedSession = null, seedEpoch = null, makeSessionStorage = null,
} = {}) {
  const sessionMap = new Map();
  const persistentMap = new Map();
  if (seedSession) persistentMap.set(SESSION_KEY, JSON.stringify(seedSession));
  if (seedEpoch) persistentMap.set(EPOCH_KEY, JSON.stringify(seedEpoch));
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

// --- HIGH-2: unverifiable current binding ---

test('batch2e r4 HIGH-2a: a transient read failure during claim validation refuses a none-carry', async () => {
  // Astra round-3 reproduction: the carry is written while
  // the shared state is empty (lstate=none), B then commits,
  // and the destination's reads throw during claim validation
  // but recover for the lifetime-binding read. Round 3
  // collapsed the failure into 'none', validated the claim,
  // and bound A to B through the second read.
  const fx = accountFixture();
  const hash = carryHash(fx, 'tok-a');
  assert.ok(hash.includes('lstate=none'), hash);
  fx.persistentMap.set(SESSION_KEY, JSON.stringify(recordB));
  let reads = 0;
  const flakyPersistent = {
    getItem: (k) => {
      reads += 1;
      if (reads <= 2) throw new Error('transient read failure');
      return fx.persistentMap.get(k) ?? null;
    },
    setItem: (k, v) => fx.persistentMap.set(k, v),
    removeItem: (k) => fx.persistentMap.delete(k),
  };
  fx.location.hash = hash;
  const dest = createFragmentSession({ ...fx, persistentStorage: flakyPersistent });
  assert.equal(await dest.getToken(), null);
});

test('batch2e r4 HIGH-2b: a none-carry is refused while the shared state stays unreadable', async () => {
  const fx = accountFixture();
  const hash = carryHash(fx, 'tok-a');
  fx.persistentMap.set(SESSION_KEY, JSON.stringify(recordB));
  const unreadablePersistent = {
    getItem: () => { throw new Error('storage unreadable'); },
    setItem: (k, v) => fx.persistentMap.set(k, v),
    removeItem: (k) => fx.persistentMap.delete(k),
  };
  fx.location.hash = hash;
  const dest = createFragmentSession({ ...fx, persistentStorage: unreadablePersistent });
  assert.equal(await dest.getToken(), null);
});

test('batch2e r4 HIGH-2c: the destination binds to the verified snapshot, not a second read', async () => {
  // The claim validates against A's record; every later read
  // observes B's commit. Round 3's independent lifetime read
  // bound the token to B and served it; the verified snapshot
  // binds it to A, and the fence retires it against B.
  const fx = accountFixture({ seedSession: recordA });
  const hash = carryHash(fx, 'tok-a');
  assert.ok(hash.includes('lineage=sid-a'), hash);
  let sessionReads = 0;
  const flippingPersistent = {
    getItem: (k) => {
      if (k === SESSION_KEY) {
        sessionReads += 1;
        return JSON.stringify(sessionReads === 1 ? recordA : recordB);
      }
      return fx.persistentMap.get(k) ?? null;
    },
    setItem: (k, v) => fx.persistentMap.set(k, v),
    removeItem: (k) => fx.persistentMap.delete(k),
  };
  fx.location.hash = hash;
  const dest = createFragmentSession({ ...fx, persistentStorage: flippingPersistent });
  assert.equal(await dest.getToken(), null);
});

// --- HIGH-3: nonce lifecycle and carry metadata ---

function fakeDocument() {
  const anchor = {
    href: '/account/purchase',
    getAttribute() { return this.href; },
    setAttribute(name, value) { this.href = value; },
    closest(selector) { return selector.startsWith('a[') ? this : null; },
  };
  const listeners = {};
  const document = {
    querySelectorAll: () => [anchor],
    addEventListener: (type, fn, capture) => { listeners[type] = { fn, capture }; },
  };
  const target = { addEventListener: () => {} };
  return { anchor, document, target, listeners };
}

test('batch2e r4 HIGH-3a: invalidation revokes the nonce, so a retained fragment cannot shed its claim', async () => {
  // Astra round-3 reproduction (no storage failures): A's
  // carried link is populated and its fragment retained; B
  // replaces the session; A's carried links are refreshed —
  // the anchor is stripped and the carry record deleted, but
  // round 3 left the nonce armed, so the retained fragment
  // with its lineage parameter removed was adopted as an
  // external handoff under B's binding.
  const fx = accountFixture({ seedSession: recordA });
  const { storage, persistentStorage } = fx;
  const nonce = ensureFragmentHandoff(storage);
  fx.location.hash = `#access_token=tok-a&state=${nonce}`;
  const sessionA = createFragmentSession({ ...fx, storage, persistentStorage });
  assert.equal(sessionA.getTokenSync(), 'tok-a');
  const { anchor, document, target } = fakeDocument();
  const refresh = installCarriedLinkRefresh({
    target, document, tokenProvider: sessionA.getToken,
    session: sessionA, storage, persistentStorage,
  });
  await refresh();
  assert.ok(anchor.href.includes('lineage=sid-a'), anchor.href);
  const retained = anchor.href.slice(anchor.href.indexOf('#'));
  // B replaces the shared session; the next refresh finds the
  // session gone and invalidates the carry.
  fx.persistentMap.set(SESSION_KEY, JSON.stringify(recordB));
  await refresh();
  assert.equal(anchor.href, '/account/purchase');
  // The retained fragment, claim stripped, must now be dead:
  // its nonce was revoked with the carry expectation.
  fx.location.hash = retained.replace('&lineage=sid-a', '');
  const dest = createFragmentSession({ ...fx, storage, persistentStorage });
  assert.equal(await dest.getToken(), null);
});

test('batch2e r4 HIGH-3b: corrupt or unreadable carry metadata refuses the adoption', async () => {
  // The claim-stripping path through metadata failure: with
  // the carry metadata unreadable or corrupt, round 3 treated
  // it as absent, so a claim-stripped fragment was accepted
  // as an external handoff. Unverifiable metadata refuses.
  // Batch 2f shapes: the single handoff record itself, and
  // the legacy two-key state it replaced.
  const corruptFx = accountFixture({ seedSession: recordA });
  const corruptHash = carryHash(corruptFx, 'tok-a');
  corruptFx.sessionMap.set(RECORD_KEY, 'not-json{');
  corruptFx.location.hash = corruptHash.replace('&lineage=sid-a', '');
  const corruptDest = createFragmentSession({ ...corruptFx });
  assert.equal(await corruptDest.getToken(), null);

  const legacyFx = accountFixture({ seedSession: recordA });
  legacyFx.sessionMap.set(HANDOFF_KEY, 'legacy-nonce');
  legacyFx.sessionMap.set(CARRY_KEY, 'not-json{');
  legacyFx.location.hash = '#access_token=tok-a&state=legacy-nonce';
  const legacyDest = createFragmentSession({ ...legacyFx });
  assert.equal(await legacyDest.getToken(), null);

  const throwControl = { throwRecordReads: false };
  const throwingFx = accountFixture({
    seedSession: recordA,
    makeSessionStorage: (map) => ({
      getItem: (k) => {
        if (throwControl.throwRecordReads && k === RECORD_KEY) {
          throw new Error('record unreadable');
        }
        return map.get(k) ?? null;
      },
      setItem: (k, v) => map.set(k, v),
      removeItem: (k) => map.delete(k),
    }),
  });
  const throwingHash = carryHash(throwingFx, 'tok-a');
  throwControl.throwRecordReads = true;
  throwingFx.location.hash = throwingHash.replace('&lineage=sid-a', '');
  const throwingDest = createFragmentSession({ ...throwingFx });
  assert.equal(await throwingDest.getToken(), null);
});

test('batch2e r4 HIGH-3c: rejection depends on the recorded carry expectation itself', async () => {
  // The coverage pin round 3 was missing: current shared
  // state B, recorded carry A, fragment claiming B. Removing
  // the recorded-carry equality check lets the claim pass
  // against the current binding and this test fails — the
  // rejection must come from the recorded expectation.
  const fx = accountFixture({ seedSession: recordB });
  const nonce = ensureFragmentHandoff(fx.storage);
  assert.equal(armCarryBinding(fx.storage, { sid: 'sid-a' }), true);
  fx.location.hash = `#access_token=tok-x&state=${nonce}&lineage=sid-b`;
  const refused = createFragmentSession({ ...fx });
  assert.equal(await refused.getToken(), null);
  // Control: the same fragment under a record naming B is
  // adopted — the record, not the claim, decides.
  const fx2 = accountFixture({ seedSession: recordB });
  const nonce2 = ensureFragmentHandoff(fx2.storage);
  assert.equal(armCarryBinding(fx2.storage, { sid: 'sid-b' }), true);
  fx2.location.hash = `#access_token=tok-x&state=${nonce2}&lineage=sid-b`;
  const adopted = createFragmentSession({ ...fx2 });
  assert.equal(await adopted.getToken(), 'tok-x');
});

// --- MEDIUM-4: fresh external arming ---

test('batch2e r4 MEDIUM-4a: fresh external arming retires the previous carry expectation', async () => {
  // Astra round-3 reproduction: a carried link exists; a
  // fresh external nonce is armed; an ordinary external
  // fragment (no claim) arrives under the new nonce. Round 3
  // judged it against the stale carry record and refused it.
  const fx = accountFixture({ seedSession: recordA });
  carryHash(fx, 'tok-a');
  const fresh = armFragmentHandoff(fx.storage);
  assert.ok(fresh);
  fx.location.hash = `#access_token=tok-ext&state=${fresh}`;
  const dest = createFragmentSession({ ...fx });
  assert.equal(await dest.getToken(), 'tok-ext');
});

test('batch2e r4 MEDIUM-4c: re-arming under the same nonce value still retires the old expectation', async () => {
  // Deterministic randomness makes the "fresh" nonce collide
  // with the carried one, so nonce association alone cannot
  // distinguish the records: only the retirement at arm time
  // keeps the external handoff adoptable. (With distinct
  // nonces the association already covers MEDIUM-4a; this pin
  // isolates the retirement itself.)
  const realCrypto = Object.getOwnPropertyDescriptor(globalThis, 'crypto');
  Object.defineProperty(globalThis, 'crypto', {
    configurable: true,
    writable: true,
    value: { getRandomValues: (arr) => { arr.fill(7); return arr; } },
  });
  try {
    const fx = accountFixture({ seedSession: recordA });
    carryHash(fx, 'tok-a');
    const pending = ensureFragmentHandoff(fx.storage);
    const fresh = armFragmentHandoff(fx.storage);
    assert.equal(fresh, pending); // same value by construction
    fx.location.hash = `#access_token=tok-ext&state=${fresh}`;
    const dest = createFragmentSession({ ...fx });
    assert.equal(await dest.getToken(), 'tok-ext');
  } finally {
    Object.defineProperty(globalThis, 'crypto', realCrypto);
  }
});

test('batch2e r4 MEDIUM-4b: arming is one write — removal failure cannot split it, write failure arms nothing', async () => {
  // Batch 2f contract (supersedes the round-4 mechanics this
  // test originally pinned): retirement and arming are the
  // SAME single write of the handoff record, so a failing
  // legacy-key removal cannot split nonce from expectation —
  // and the failure that matters is the write itself.
  const control = { failCarryRemoval: false, failRecordWrite: false };
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
      removeItem: (k) => {
        if (control.failCarryRemoval && k === CARRY_KEY) {
          throw new Error('carry removal blocked');
        }
        map.delete(k);
      },
    }),
  });
  const carriedHash = carryHash(fx, 'tok-a');
  const pendingBefore = ensureFragmentHandoff(fx.storage);
  assert.ok(pendingBefore);
  // A failing legacy removal does not block the arm: the
  // write retires the old expectation atomically. (While the
  // removal stays blocked, consumption is likewise refused —
  // single-use is a hard condition — so the failure is lifted
  // before the link is followed.)
  control.failCarryRemoval = true;
  const fresh = armFragmentHandoff(fx.storage);
  assert.ok(fresh && fresh !== pendingBefore);
  control.failCarryRemoval = false;
  fx.location.hash = `#access_token=tok-ext&state=${fresh}`;
  const dest = createFragmentSession({ ...fx });
  assert.equal(await dest.getToken(), 'tok-ext');
  // A failing WRITE arms nothing: the previous nonce and its
  // expectation stay together (the carried fragment still
  // validates whole, a claim-stripped one still refuses).
  const carriedHash2 = carryHash(fx, 'tok-a');
  const pending2 = ensureFragmentHandoff(fx.storage);
  assert.ok(pending2);
  control.failRecordWrite = true;
  assert.equal(armFragmentHandoff(fx.storage), null);
  assert.equal(ensureFragmentHandoff(fx.storage), pending2);
  fx.location.hash = carriedHash2.replace('&lineage=sid-a', '');
  const stripped = createFragmentSession({ ...fx });
  assert.equal(await stripped.getToken(), null);
  // Recovery: once the write lands, arming succeeds again.
  control.failRecordWrite = false;
  const fresh2 = armFragmentHandoff(fx.storage);
  assert.ok(fresh2 && fresh2 !== pending2);
  assert.ok(carriedHash && carriedHash2);
});
