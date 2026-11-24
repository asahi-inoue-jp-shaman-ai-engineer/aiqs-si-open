import test from 'node:test';
import assert from 'node:assert/strict';
import { createAppAuth } from '../src/app-auth.js';
import {
  carryTokenToAccountLinks, createFragmentSession,
  ensureFragmentHandoff,
} from '../src/account-session.js';

// Batch 2e ROUND 3 regressions (Astra round-2 residuals +
// D-019 pass-line checklist):
//
// HIGH-2: fragment adoption requires a COMMITTED clear — a
// fresh tombstone that landed AND a session record confirmed
// absent. "Unreadable" is not "absent", and an unchanged old
// tombstone cannot bind new credentials.
// HIGH-4: a carried handoff names its complete originating
// binding — sid, legacy revision, or an explicit none marker —
// recorded next to the nonce at carry time. The destination
// requires fragment claim == carry record == current shared
// binding. External handoffs are the inverse shape (no record,
// no claim) and anything else is refused.

const SESSION_KEY = 'si-app-session-v1';
const EPOCH_KEY = 'si-app-session-epoch-v1';

// The pending handoff nonce, read through the protocol's own
// API (batch 2f: the nonce lives inside the single handoff
// record, not under a raw key a test can read directly).
const pendingNonce = (map) => ensureFragmentHandoff({
  getItem: (k) => map.get(k) ?? null,
  setItem: (k, v) => map.set(k, v),
  removeItem: (k) => map.delete(k),
});

function appFixture({ persistent, persistentStorage }) {
  const session = new Map();
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
  const auth = createAppAuth({
    fetch: async () => ({ ok: true, status: 200, json: async () => ({}) }),
    location, history, storage: wrap(session),
    persistentStorage, crypto: globalThis.crypto,
  });
  return { auth, session, location };
}

test('batch2e r3 HIGH-2a: adoption over an unchanged tombstone aborts when writes fail', async () => {
  // Astra round-2 reproduction (a): account A is fragment-only
  // — no session record, only A's clear tombstone in the epoch
  // record. Persistent writes now fail. Adopting fragment B
  // must abort: there is no fresh tombstone to bind B to, and
  // the account page bound to A's tombstone would keep
  // serving A while this tab served B.
  const persistent = new Map();
  persistent.set(EPOCH_KEY, JSON.stringify({
    rev: 'rev-ta', owner: 'tab-0', kind: 'clear', sid: 'sid-ta',
  }));
  const persistentStorage = {
    getItem: (k) => persistent.get(k) ?? null,
    setItem: () => { throw new Error('storage blocked'); },
    removeItem: () => { throw new Error('storage blocked'); },
  };
  const { auth, session, location } = appFixture({ persistent, persistentStorage });
  const nonce = pendingNonce(session);
  assert.ok(nonce);
  location.hash = `#access_token=access-b&state=${nonce}`;
  assert.equal(auth.takeFragment(), false);
  assert.notEqual(await auth.getToken(), 'access-b');
  // A's tombstone is untouched: the binding reality is unchanged.
  assert.equal(JSON.parse(persistent.get(EPOCH_KEY)).rev, 'rev-ta');
});

test('batch2e r3 HIGH-2b: adoption aborts when the record cannot be verified absent', async () => {
  // Astra round-2 reproduction (b), read side: writes work,
  // but reads throw. A failed read is UNREADABLE, not absent —
  // a surviving record must not be adopted over.
  const persistent = new Map();
  persistent.set(SESSION_KEY, JSON.stringify({
    refreshToken: 'refresh-a1', rev: 'rev-a', owner: 'tab-0', sid: 'sid-a',
  }));
  const persistentStorage = {
    getItem: () => { throw new Error('storage unreadable'); },
    setItem: (k, v) => persistent.set(k, v),
    removeItem: (k) => persistent.delete(k),
  };
  const { auth, session, location } = appFixture({ persistent, persistentStorage });
  const nonce = pendingNonce(session);
  assert.ok(nonce);
  location.hash = `#access_token=access-b&state=${nonce}`;
  assert.equal(auth.takeFragment(), false);
  assert.notEqual(await auth.getToken(), 'access-b');
});

test('batch2e r3 HIGH-1 pin: a commit injected at the request revision read cannot pair the old token with the new revision', async () => {
  // Astra round-2's exact sequence: B's commit lands after
  // refresh captured A's snapshot, at the moment the request
  // machinery next reads the shared revision. Reverting
  // tokenRequest to an independent re-read captures B's
  // revision there, pairs it with A's in-flight token, and
  // A's rotation overwrites B — this test then fails.
  const persistent = new Map();
  persistent.set(SESSION_KEY, JSON.stringify({
    refreshToken: 'refresh-a1', rev: 'rev-a', owner: 'tab-0', sid: 'sid-a',
  }));
  let sessionReads = 0;
  const persistentStorage = {
    getItem: (k) => {
      if (k === SESSION_KEY) {
        sessionReads += 1;
        if (sessionReads === 3) {
          // Reads 1-2: getToken's fence and refresh's snapshot
          // load (both see A). Read 3 is the request's own
          // revision touch: B commits first, then answers.
          persistent.set(SESSION_KEY, JSON.stringify({
            refreshToken: 'refresh-b1', rev: 'rev-b', owner: 'tab-b', sid: 'sid-b',
          }));
        }
      }
      return persistent.get(k) ?? null;
    },
    setItem: (k, v) => persistent.set(k, v),
    removeItem: (k) => persistent.delete(k),
  };
  const session = new Map();
  const wrap = (map) => ({
    getItem: (k) => map.get(k) ?? null,
    setItem: (k, v) => map.set(k, v),
    removeItem: (k) => map.delete(k),
  });
  const sent = [];
  const auth = createAppAuth({
    fetch: async (url, options) => {
      const presented = new URLSearchParams(options.body).get('refresh_token');
      sent.push(presented);
      if (presented === 'refresh-a1') {
        return { ok: true, status: 200, json: async () => ({
          access_token: 'access-a2', refresh_token: 'refresh-a2', expires_in: 3600 }) };
      }
      if (presented === 'refresh-b1') {
        return { ok: true, status: 200, json: async () => ({
          access_token: 'access-b', refresh_token: 'refresh-b2', expires_in: 3600 }) };
      }
      throw new Error('network down');
    },
    location: {
      origin: 'https://asi.example.test', search: '', hash: '', pathname: '/app',
      assigned: null, assign(url) { this.assigned = url; },
    },
    history: { replaceState: () => {} },
    storage: wrap(session), persistentStorage, crypto: globalThis.crypto,
  });
  assert.equal(await auth.getToken(), 'access-b');
  assert.equal(sent[0], 'refresh-a1');
  const record = JSON.parse(persistent.get(SESSION_KEY));
  assert.equal(record.refreshToken, 'refresh-b2');
  assert.equal(record.sid, 'sid-b');
});

// --- HIGH-4 destination races ---

function accountFixture({ seedSession = null, seedEpoch = null } = {}) {
  const sessionMap = new Map();
  const persistentMap = new Map();
  if (seedSession) persistentMap.set(SESSION_KEY, JSON.stringify(seedSession));
  if (seedEpoch) persistentMap.set(EPOCH_KEY, JSON.stringify(seedEpoch));
  const wrapMap = (map) => ({
    getItem: (k) => map.get(k) ?? null,
    setItem: (k, v) => map.set(k, v),
    removeItem: (k) => map.delete(k),
  });
  const storage = wrapMap(sessionMap);
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
    storage, persistentStorage, persistentMap, location, history,
    anchor, document,
  };
}

function carryAndFollow(fx, token, { commitBefore = null } = {}) {
  assert.equal(carryTokenToAccountLinks(
    fx.document, token, fx.storage, fx.persistentStorage), 1);
  const hash = fx.anchor.href.slice(fx.anchor.href.indexOf('#'));
  if (commitBefore) commitBefore(fx.persistentMap);
  fx.location.hash = hash;
  return createFragmentSession({ ...fx });
}

test('batch2e r3 HIGH-4a: a none-carry is refused after a session commits, adopted while none stands', async () => {
  const commitB = (map) => map.set(SESSION_KEY, JSON.stringify({
    refreshToken: 'refresh-b1', rev: 'rev-b', owner: 'tab-b', sid: 'sid-b',
  }));
  const refusedFx = accountFixture();
  const refused = carryAndFollow(refusedFx, 'tok-a', { commitBefore: commitB });
  assert.equal(await refused.getToken(), null);
  assert.ok(refusedFx.anchor.href.includes('lstate=none'),
    refusedFx.anchor.href);
  const adoptedFx = accountFixture();
  const adopted = carryAndFollow(adoptedFx, 'tok-a');
  assert.equal(await adopted.getToken(), 'tok-a');
});

test('batch2e r3 HIGH-4b: a legacy-rev carry is refused after replacement, adopted while it stands', async () => {
  const legacy = {
    seedSession: { refreshToken: 'refresh-old', savedAt: 1 },
    seedEpoch: { epoch: 7, owner: 'tab-0' },
  };
  const commitB = (map) => map.set(SESSION_KEY, JSON.stringify({
    refreshToken: 'refresh-b1', rev: 'rev-b', owner: 'tab-b', sid: 'sid-b',
  }));
  const refusedFx = accountFixture(legacy);
  const refused = carryAndFollow(refusedFx, 'tok-a', { commitBefore: commitB });
  assert.equal(await refused.getToken(), null);
  assert.ok(refusedFx.anchor.href.includes('lrev=legacy%3A7'),
    refusedFx.anchor.href);
  const adoptedFx = accountFixture(legacy);
  const adopted = carryAndFollow(adoptedFx, 'tok-a');
  assert.equal(await adopted.getToken(), 'tok-a');
});

test('batch2e r3 HIGH-4c: polluted or unrecorded lineage claims are refused', async () => {
  // Parameter pollution: two claims in one fragment, even
  // with a valid nonce and a matching carry record.
  const fx = accountFixture({
    seedSession: { refreshToken: 'refresh-a1', rev: 'rev-a', owner: 'tab-0', sid: 'sid-a' },
  });
  assert.equal(carryTokenToAccountLinks(
    fx.document, 'tok-a', fx.storage, fx.persistentStorage), 1);
  const goodHash = fx.anchor.href.slice(fx.anchor.href.indexOf('#'));
  fx.location.hash = `${goodHash}&lstate=none`;
  const polluted = createFragmentSession({ ...fx });
  assert.equal(await polluted.getToken(), null);
  // Inverse shape: an externally armed nonce (no carry record)
  // plus a fragment that carries a lineage claim.
  const fx2 = accountFixture({
    seedSession: { refreshToken: 'refresh-b1', rev: 'rev-b', owner: 'tab-b', sid: 'sid-b' },
  });
  const nonce = ensureFragmentHandoff(fx2.storage);
  fx2.location.hash = `#access_token=tok-b&state=${nonce}&lineage=sid-b`;
  const downgraded = createFragmentSession({ ...fx2 });
  assert.equal(await downgraded.getToken(), null);
});
