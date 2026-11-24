import test from 'node:test';
import assert from 'node:assert/strict';
import {
  carryTokenToAccountLinks, createFragmentSession, ensureFragmentHandoff,
  verifiedSharedCarryBinding,
} from '../src/account-session.js';

// Batch 2f ADDITIONAL round regressions (D-017 exception —
// scope is exactly the round-3 residue):
//
// 1. The runtime fence's lineage derivation is unified onto
//    the VERIFIED derivation (verifiedSharedCarryBinding).
//    The fence's own parser disagreed with the verified
//    shapes: a bare {"refreshToken": ...} record derived the
//    empty lineage {rev: null, sid: null} at the fence while
//    the verified derivation called the same state
//    unverifiable, and a token bound to that emptiness (or
//    compared equal to it on the revision fallback) was
//    served indefinitely. The adoption snapshot for external
//    handoffs comes through the same reader, so no side of
//    the fence comparison can hold a value the verified
//    derivation cannot produce; an unverifiable snapshot
//    serves nothing.
// 2. A stored JSON 'null' literal is classified exactly as
//    readSharedRecord classifies it — present but unreadable,
//    never confirmed absence — on both the verification side
//    and the fence side.
// 3. Coverage: the numeric epoch's finiteness check gains a
//    pin — round 3 noted that dropping it passed every test
//    then supplied.

const SESSION_KEY = 'si-app-session-v1';
const EPOCH_KEY = 'si-app-session-epoch-v1';

const recordA = {
  refreshToken: 'refresh-a1', rev: 'rev-a', owner: 'tab-a', sid: 'sid-a',
};
const recordB = {
  refreshToken: 'refresh-b1', rev: 'rev-b', owner: 'tab-b', sid: 'sid-b',
};

function accountFixture({
  seedSession = null, seedEpoch = null,
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

function adoptCarried(fx, token) {
  assert.equal(carryTokenToAccountLinks(
    fx.document, token, fx.storage, fx.persistentStorage), 1);
  fx.location.hash = fx.anchor.href.slice(fx.anchor.href.indexOf('#'));
  return createFragmentSession({ ...fx });
}

function adoptExternal(fx, token) {
  const nonce = ensureFragmentHandoff(fx.storage);
  assert.ok(nonce);
  fx.location.hash = `#access_token=${token}&state=${nonce}`;
  return createFragmentSession({ ...fx });
}

// --- 1a. A carried token retires when the session record is
// replaced by a bare refreshToken record (round-3 repro) ---

test('batch2f r4: a carried token retires when the session record becomes a bare refreshToken record', async () => {
  const fx = accountFixture({ seedSession: recordA });
  const dest = adoptCarried(fx, 'tok-a');
  assert.equal(await dest.getToken(), 'tok-a');
  // The shared record is replaced by a shape the verified
  // derivation calls unverifiable (a session record whose
  // revision should live in an epoch record that is not
  // there). The round-3 fence derived {rev: null, sid: null}
  // from it, which compared equal to the binding's revision
  // fallback, and the old token kept being served.
  fx.persistentMap.set(SESSION_KEY, '{"refreshToken":"rb"}');
  assert.equal(dest.getTokenSync(), null);
  assert.equal(await dest.getToken(), null);
  let dispatched = 'unset';
  dest.withToken((token) => { dispatched = token; });
  assert.equal(dispatched, null);
});

// --- 1b. An external token adopted under an unverifiable
// state is bound to nothing servable ---

test('batch2f r4: an external token adopted under a bare refreshToken record is never served', async () => {
  const fx = accountFixture({ seedSession: '{"refreshToken":"rb"}' });
  const dest = adoptExternal(fx, 'tok-x');
  assert.equal(dest.getTokenSync(), null);
  assert.equal(await dest.getToken(), null);
  // A real lineage appearing later must not resurrect it:
  // the snapshot was unverifiable, not an empty lineage the
  // new record could compare equal to.
  fx.persistentMap.set(SESSION_KEY, JSON.stringify(recordB));
  assert.equal(dest.getTokenSync(), null);
  assert.equal(await dest.getToken(), null);
  // Neither may a later confirmed absence: the token was
  // never bound to 'none' either.
  fx.persistentMap.delete(SESSION_KEY);
  assert.equal(dest.getTokenSync(), null);
  assert.equal(await dest.getToken(), null);
});

// --- 2. A stored 'null' literal is unreadable, not absence ---

test('batch2f r4: a stored null literal is unverifiable on the verified derivation', () => {
  const fxSession = accountFixture({ seedSession: 'null' });
  assert.equal(verifiedSharedCarryBinding(fxSession.persistentStorage), null);
  const fxEpoch = accountFixture({ seedEpoch: 'null' });
  assert.equal(verifiedSharedCarryBinding(fxEpoch.persistentStorage), null);
  // Control: genuine absence is still confirmed absence.
  const fxAbsent = accountFixture({});
  assert.equal(verifiedSharedCarryBinding(fxAbsent.persistentStorage), 'none');
});

test('batch2f r4: an adopted none-carry retires when a null literal appears at either shared key', async () => {
  const fxSession = accountFixture({});
  const destSession = adoptCarried(fxSession, 'tok-none');
  assert.equal(await destSession.getToken(), 'tok-none');
  fxSession.persistentMap.set(SESSION_KEY, 'null');
  assert.equal(destSession.getTokenSync(), null);
  assert.equal(await destSession.getToken(), null);

  const fxEpoch = accountFixture({});
  const destEpoch = adoptCarried(fxEpoch, 'tok-none');
  assert.equal(await destEpoch.getToken(), 'tok-none');
  fxEpoch.persistentMap.set(EPOCH_KEY, 'null');
  assert.equal(destEpoch.getTokenSync(), null);
  assert.equal(await destEpoch.getToken(), null);
});

test('batch2f r4: an external token adopted over a stored null literal is never served', async () => {
  const fx = accountFixture({ seedSession: 'null' });
  const dest = adoptExternal(fx, 'tok-x');
  assert.equal(dest.getTokenSync(), null);
  assert.equal(await dest.getToken(), null);
});

// --- 3. The numeric epoch must be finite ---

test('batch2f r4: a non-finite numeric epoch never verifies', () => {
  // Raw strings: JSON.stringify would turn Infinity into
  // null; the stored text below is what a foreign writer (or
  // corruption) can actually leave behind, and JSON.parse
  // reads 1e400 back as Infinity.
  const fxBare = accountFixture({ seedEpoch: '{"epoch":1e400}' });
  assert.equal(verifiedSharedCarryBinding(fxBare.persistentStorage), null);
  const fxOwned = accountFixture({ seedEpoch: '{"epoch":1e400,"owner":"tab"}' });
  assert.equal(verifiedSharedCarryBinding(fxOwned.persistentStorage), null);
  // The deferral path: a legacy session record whose
  // revision lives in a non-finite epoch record.
  const fxDeferred = accountFixture({
    seedSession: { refreshToken: 'r' },
    seedEpoch: '{"epoch":1e400}',
  });
  assert.equal(verifiedSharedCarryBinding(fxDeferred.persistentStorage), null);
  // Finite numeric epochs still verify (control).
  const fxOk = accountFixture({ seedEpoch: { epoch: 3, owner: 'tab' } });
  assert.deepEqual(verifiedSharedCarryBinding(fxOk.persistentStorage),
    { rev: 'legacy:3' });
});

// --- Controls: ordinary external-handoff behavior is
// unchanged by the unified snapshot ---

test('batch2f r4: an external token survives rotation and retires on replacement', async () => {
  const fx = accountFixture({ seedSession: recordA });
  const dest = adoptExternal(fx, 'tok-x');
  assert.equal(await dest.getToken(), 'tok-x');
  // Rotation: fresh revision and refresh token, same lineage.
  fx.persistentMap.set(SESSION_KEY, JSON.stringify({
    refreshToken: 'refresh-a2', rev: 'rev-a2', owner: 'tab-a', sid: 'sid-a',
  }));
  assert.equal(dest.getTokenSync(), 'tok-x');
  assert.equal(await dest.getToken(), 'tok-x');
  // Replacement: a different lineage retires the token.
  fx.persistentMap.set(SESSION_KEY, JSON.stringify(recordB));
  assert.equal(dest.getTokenSync(), null);
  assert.equal(await dest.getToken(), null);
});

test('batch2f r4: an external token retires when the shared state disappears', async () => {
  const fx = accountFixture({ seedSession: recordA });
  const dest = adoptExternal(fx, 'tok-x');
  assert.equal(await dest.getToken(), 'tok-x');
  fx.persistentMap.delete(SESSION_KEY);
  assert.equal(dest.getTokenSync(), null);
  assert.equal(await dest.getToken(), null);
});
