import test from 'node:test';
import assert from 'node:assert/strict';
import {
  carryTokenToAccountLinks, createFragmentSession, ensureFragmentHandoff,
  takeFragmentHandoff,
} from '../src/account-session.js';

// Batch 2j regressions (Astra batch2i diff review, F1–F2):
//
// F1 (HIGH): the account supply fence compared every later
// state against the identity captured at adoption. Accepting
// a legitimate rotation did not advance that baseline, so a
// substitution under the ROTATED revision — same rev, foreign
// refresh token — fell outside the same-revision token clause
// and the captured token kept being served. The baseline now
// ratchets: every accepted state becomes the new baseline,
// and a revision the fence has already moved past never
// serves again.
//
// F2 (HIGH): the fence judged each call statelessly, so a
// state that retired the token — a foreign record standing
// where its tombstone stood, the record disappearing, an
// unverifiable shape — retired it only for as long as that
// state lasted. When the offending state went away and the
// captured state became visible again, the account side
// REVIVED the token, while the app side (which drops its
// in-memory session on supersession) stayed retired. The two
// surfaces judged the same bytes inconsistently. Retirement
// now latches: once any fence check retires the token, this
// session never serves it again.
//
// The audit premise for this batch covers MULTI-STEP
// transitions (rotation accepted, substitution, retirement,
// re-exposure), not only predicates relative to the capture
// instant — the batch 2i pinpoint review closed on the
// capture-relative predicate, and the differential review
// then executed these sequences from outside it.

const SESSION_KEY = 'si-app-session-v1';
const EPOCH_KEY = 'si-app-session-epoch-v1';

function accountFixture({ seedSession = null, seedEpoch = null } = {}) {
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

function setSession(fx, record) {
  if (record === null) fx.persistentMap.delete(SESSION_KEY);
  else fx.persistentMap.set(SESSION_KEY, JSON.stringify(record));
}

function externalAdoption(fx, token = 'token-A') {
  const nonce = ensureFragmentHandoff(fx.storage);
  assert.ok(nonce);
  fx.location.hash = `#access_token=${token}&state=${nonce}`;
  return createFragmentSession({ ...fx });
}

const recordA0 = {
  refreshToken: 'refresh-A0', rev: 'r0', owner: 'tab-0', sid: 'sid-A',
};
const recordA1 = {
  refreshToken: 'refresh-A1', rev: 'r1', owner: 'tab-0', sid: 'sid-A',
};
const recordA2 = {
  refreshToken: 'refresh-A2', rev: 'r2', owner: 'tab-0', sid: 'sid-A',
};

// --- F1: the baseline ratchets ---

test('batch2j F1: substitution under the rotated revision retires the token', async () => {
  // Astra's executed sequence, step for step (sid-A fixed).
  const fx = accountFixture({ seedSession: recordA0 });
  const session = externalAdoption(fx);
  assert.equal(await session.getToken(), 'token-A');
  // A legitimate rotation is accepted.
  setSession(fx, recordA1);
  assert.equal(await session.getToken(), 'token-A');
  // A substitution under the CURRENT revision: the baseline
  // has advanced to r1, so the same-revision token clause
  // applies to it.
  setSession(fx, {
    refreshToken: 'refresh-B', rev: 'r1', owner: 'tab-9', sid: 'sid-A',
  });
  assert.equal(await session.getToken(), null);
  assert.equal(session.getTokenSync(), null);
  let dispatched = 'unset';
  session.withToken((token) => { dispatched = token; });
  assert.equal(dispatched, null);
});

test('batch2j F1: consecutive genuine rotations keep serving, then substitution retires', async () => {
  const fx = accountFixture({ seedSession: recordA0 });
  const session = externalAdoption(fx);
  assert.equal(await session.getToken(), 'token-A');
  setSession(fx, recordA1);
  assert.equal(await session.getToken(), 'token-A');
  setSession(fx, recordA2);
  assert.equal(await session.getToken(), 'token-A');
  // Repeated checks against the newest accepted state serve.
  assert.equal(await session.getToken(), 'token-A');
  setSession(fx, {
    refreshToken: 'refresh-B', rev: 'r2', owner: 'tab-9', sid: 'sid-A',
  });
  assert.equal(await session.getToken(), null);
});

test('batch2j F1: a substitution under a revision the fence moved past retires', async () => {
  const fx = accountFixture({ seedSession: recordA0 });
  const session = externalAdoption(fx);
  assert.equal(await session.getToken(), 'token-A');
  setSession(fx, recordA1);
  assert.equal(await session.getToken(), 'token-A');
  // Rollback-shaped substitution: an old revision carrying a
  // foreign token. A ratchet never goes back.
  setSession(fx, {
    refreshToken: 'refresh-B', rev: 'r0', owner: 'tab-9', sid: 'sid-A',
  });
  assert.equal(await session.getToken(), null);
});

test('batch2j F1: the previously accepted state itself never serves again once passed', async () => {
  const fx = accountFixture({ seedSession: recordA0 });
  const session = externalAdoption(fx);
  assert.equal(await session.getToken(), 'token-A');
  setSession(fx, recordA1);
  assert.equal(await session.getToken(), 'token-A');
  // The genuine r0 state reappears (a restored stale record):
  // the fence has ratcheted past r0, so it stays retired —
  // the app side likewise never re-serves a superseded
  // binding.
  setSession(fx, recordA0);
  assert.equal(await session.getToken(), null);
});

test('batch2j F1: an unobserved double rotation is accepted as one ratchet step', async () => {
  const fx = accountFixture({ seedSession: recordA0 });
  const session = externalAdoption(fx);
  assert.equal(await session.getToken(), 'token-A');
  // The account page was idle across two rotations: the first
  // state it observes is r2, a forward jump along the same
  // lineage.
  setSession(fx, recordA2);
  assert.equal(await session.getToken(), 'token-A');
  setSession(fx, {
    refreshToken: 'refresh-B', rev: 'r2', owner: 'tab-9', sid: 'sid-A',
  });
  assert.equal(await session.getToken(), null);
});

// --- F2: retirement latches ---

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

test('batch2j F2: a copied tombstone substitution stays retired after it is removed', async () => {
  // Astra's executed sequence, step for step.
  const fx = carriedUnderTombstone();
  const session = createFragmentSession({ ...fx });
  assert.equal(await session.getToken(), 'token-A');
  // A foreign session record copying the tombstone's rev/sid.
  setSession(fx, {
    refreshToken: 'refresh-B', rev: 'rev-t', owner: 'tab-x',
    sid: 'sid-t', savedAt: 1,
  });
  assert.equal(await session.getToken(), null);
  // The foreign record is removed; the original tombstone is
  // visible again, byte for byte. The app side stays retired
  // here; the account side must too.
  setSession(fx, null);
  assert.equal(await session.getToken(), null);
  assert.equal(session.getTokenSync(), null);
});

test('batch2j F2: a distinct foreign lineage stays retired after it is removed', async () => {
  const fx = carriedUnderTombstone();
  const session = createFragmentSession({ ...fx });
  assert.equal(await session.getToken(), 'token-A');
  setSession(fx, {
    refreshToken: 'refresh-B', rev: 'rev-x', owner: 'tab-x', sid: 'sid-x',
  });
  assert.equal(await session.getToken(), null);
  setSession(fx, null);
  assert.equal(await session.getToken(), null);
});

test('batch2j F2: disappearance and exact restoration of the captured record stays retired', async () => {
  const fx = accountFixture({ seedSession: recordA0 });
  const session = externalAdoption(fx);
  assert.equal(await session.getToken(), 'token-A');
  // The established record disappears: invalidation.
  setSession(fx, null);
  assert.equal(await session.getToken(), null);
  // The identical record is restored. Stateless judgment
  // would serve again; the latch does not.
  setSession(fx, recordA0);
  assert.equal(await session.getToken(), null);
});

test('batch2j F2: an unverifiable excursion stays retired after the state heals', async () => {
  const fx = accountFixture({ seedSession: recordA0 });
  const session = externalAdoption(fx);
  assert.equal(await session.getToken(), 'token-A');
  fx.persistentMap.set(SESSION_KEY, '{}');
  assert.equal(await session.getToken(), null);
  setSession(fx, recordA0);
  assert.equal(await session.getToken(), null);
});

test('batch2j F2: a record appearing over an absent capture stays retired after it vanishes', async () => {
  const fx = accountFixture();
  const session = externalAdoption(fx);
  assert.equal(await session.getToken(), 'token-A');
  setSession(fx, recordA0);
  assert.equal(await session.getToken(), null);
  setSession(fx, null);
  assert.equal(await session.getToken(), null);
});

test('batch2j F2: control — an untouched state never latches', async () => {
  const fx = carriedUnderTombstone();
  const session = createFragmentSession({ ...fx });
  assert.equal(await session.getToken(), 'token-A');
  assert.equal(await session.getToken(), 'token-A');
  assert.equal(session.getTokenSync(), 'token-A');
});
