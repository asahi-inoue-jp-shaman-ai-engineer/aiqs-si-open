import test from 'node:test';
import assert from 'node:assert/strict';
import { createAppAuth } from '../src/app-auth.js';
import {
  createFragmentSession, ensureFragmentHandoff,
  installCarriedLinkRefresh,
} from '../src/account-session.js';

// The pending handoff nonce, read through the protocol's own
// API (batch 2f: the nonce lives inside the single handoff
// record, not under a raw key a test can read directly).
const pendingNonce = (map) => ensureFragmentHandoff({
  getItem: (k) => map.get(k) ?? null,
  setItem: (k, v) => map.set(k, v),
  removeItem: (k) => map.delete(k),
});

// Batch 2e ROUND 2 regressions (Astra adversarial review of the
// batch 2e fix diff, R1): the four sequences round 1 still
// failed, plus the carried-link invalidation they exposed.
//
// 1. The refresh fence must compare against the revision the
//    SENT token is bound to — a foreign commit landing between
//    this tab's load and its token request must not pair the
//    old token with the new session's revision.
// 2. Fragment adoption is a committed transition: when the
//    shared clear cannot land, the handed-over token is NOT
//    installed.
// 3. Supersession is an exact revision match: an older
//    tombstone written by THIS instance must not stand in for
//    a session record that has since disappeared.
// 4. Carried account links die with their session: a refresh
//    that finds the session superseded strips token-bearing
//    anchors, the click handler re-checks through the
//    synchronous fence instead of a cached token, and the
//    destination refuses a token carried under a lineage that
//    has moved.

const SESSION_KEY = 'si-app-session-v1';
const EPOCH_KEY = 'si-app-session-epoch-v1';

function fixture({ responder } = {}) {
  const session = new Map();
  const persistent = new Map();
  const control = { breakPersistentWrites: false };
  const wrap = (map) => ({
    getItem: (k) => map.get(k) ?? null,
    setItem: (k, v) => map.set(k, v),
    removeItem: (k) => map.delete(k),
  });
  const persistentStorage = {
    getItem: (k) => persistent.get(k) ?? null,
    setItem: (k, v) => {
      if (control.breakPersistentWrites) throw new Error('storage blocked');
      persistent.set(k, v);
    },
    removeItem: (k) => {
      if (control.breakPersistentWrites) throw new Error('storage blocked');
      persistent.delete(k);
    },
  };
  const location = {
    origin: 'https://asi.example.test', search: '', hash: '', pathname: '/app',
    assigned: null, assign(url) { this.assigned = url; },
  };
  const history = { replaced: [], replaceState(...args) { this.replaced.push(args); } };
  const fetch = async (url, options) => responder(url, options);
  const make = () => createAppAuth({
    fetch, location, history, storage: wrap(session),
    persistentStorage, crypto: globalThis.crypto,
  });
  return { make, session, persistent, persistentStorage, control, location };
}

const tokenResponse = (data) => ({
  ok: true, status: 200, json: async () => data,
});

test('batch2e r2 R1-1: a commit between load and request cannot pair the old token with the new revision', async () => {
  // Round-3 pin (Astra round 2, test defect): the foreign
  // commit lands INSIDE the token request — after this tab's
  // refresh snapshot was captured, before the response is
  // processed. Reverting tokenRequest to a fresh revision
  // re-read makes this test fail (A's rotation is adopted).
  const { make, persistent } = fixture({
    responder: async (url, options) => {
      const body = new URLSearchParams(options.body);
      const presented = body.get('refresh_token');
      if (presented === 'refresh-a1') {
        // B commits while A's request is in flight.
        persistent.set(SESSION_KEY, JSON.stringify({
          refreshToken: 'refresh-b1', rev: 'rev-b', owner: 'tab-b', sid: 'sid-b',
        }));
        return tokenResponse({
          access_token: 'access-a2', refresh_token: 'refresh-a2', expires_in: 3600,
        });
      }
      if (presented === 'refresh-b1') {
        return tokenResponse({
          access_token: 'access-b', refresh_token: 'refresh-b2', expires_in: 3600,
        });
      }
      throw new Error('network down');
    },
  });
  persistent.set(SESSION_KEY, JSON.stringify({
    refreshToken: 'refresh-a1', rev: 'rev-a', owner: 'tab-0', sid: 'sid-a',
  }));
  const tabA = make();
  // A is sent, its rotation is discarded against the revision
  // A is bound to, and the retry adopts B as one snapshot.
  assert.equal(await tabA.getToken(), 'access-b');
  const record = JSON.parse(persistent.get(SESSION_KEY));
  assert.equal(record.refreshToken, 'refresh-b2');
  assert.equal(record.sid, 'sid-b');
});

test('batch2e r2 R1-1b: a foreign commit between calls is adopted by the retry, never overwritten', async () => {
  let aAttempts = 0;
  const { make, persistent } = fixture({
    responder: async (url, options) => {
      const body = new URLSearchParams(options.body);
      const presented = body.get('refresh_token');
      if (presented === 'refresh-a1') {
        aAttempts += 1;
        if (aAttempts === 1) throw new Error('network down');
        return tokenResponse({
          access_token: 'access-a2', refresh_token: 'refresh-a2', expires_in: 3600,
        });
      }
      if (presented === 'refresh-b1') {
        return tokenResponse({
          access_token: 'access-b', refresh_token: 'refresh-b2', expires_in: 3600,
        });
      }
      throw new Error('network down');
    },
  });
  persistent.set(SESSION_KEY, JSON.stringify({
    refreshToken: 'refresh-a1', rev: 'rev-a', owner: 'tab-0', sid: 'sid-a',
  }));
  const tabA = make();
  assert.equal(await tabA.getToken(), null);
  persistent.set(SESSION_KEY, JSON.stringify({
    refreshToken: 'refresh-b1', rev: 'rev-b', owner: 'tab-b', sid: 'sid-b',
  }));
  assert.equal(await tabA.getToken(), 'access-b');
  const record = JSON.parse(persistent.get(SESSION_KEY));
  assert.equal(record.refreshToken, 'refresh-b2');
  assert.equal(record.sid, 'sid-b');
});

test('batch2e r2 R1-2: fragment adoption aborts when the shared clear cannot land', async () => {
  const { make, session, persistent, control, location } = fixture({
    responder: async () => tokenResponse({}),
  });
  persistent.set(SESSION_KEY, JSON.stringify({
    refreshToken: 'refresh-a1', rev: 'rev-a', owner: 'tab-0', sid: 'sid-a',
  }));
  const tab = make();
  const nonce = pendingNonce(session);
  assert.ok(nonce);
  control.breakPersistentWrites = true;
  location.hash = `#access_token=access-b&state=${nonce}`;
  assert.equal(tab.takeFragment(), false);
  // The handed-over token was not installed: nothing serves it.
  assert.notEqual(await tab.getToken(), 'access-b');
  let dispatched = 'unset';
  await tab.withCurrentToken((token) => { dispatched = token; });
  assert.notEqual(dispatched, 'access-b');
  // And the surviving record still speaks for account A alone.
  assert.equal(JSON.parse(persistent.get(SESSION_KEY)).refreshToken, 'refresh-a1');
});

test('batch2e r2 R1-3: an older own tombstone does not stand in for a disappeared session record', async () => {
  const { make, session, persistent, location } = fixture({
    responder: async (url, options) => {
      const body = new URLSearchParams(options.body);
      if (body.get('grant_type') === 'authorization_code') {
        return tokenResponse({
          access_token: 'access-a', refresh_token: 'refresh-a1', expires_in: 3600,
        });
      }
      return tokenResponse({});
    },
  });
  const tab = make();
  // This instance writes a clear tombstone first...
  tab.clearSession();
  assert.ok(persistent.get(EPOCH_KEY));
  // ...then establishes a refresh session of its own.
  await tab.beginLogin();
  const pending = JSON.parse(session.get('si-app-pkce-v1'));
  location.search = `?code=synthetic-code&state=${pending.state}`;
  assert.equal(await tab.completeRedirect(), true);
  assert.equal(await tab.getToken(), 'access-a');
  // The session record disappears; only the older tombstone
  // (same owner — this instance) survives. The session is
  // superseded all the same: the access token is not served.
  persistent.delete(SESSION_KEY);
  assert.equal(await tab.getToken(), null);
});

// --- carried links (account-session) ---

function accountFixture({ hash = '', persistentSeed = null } = {}) {
  const sessionMap = new Map();
  const persistentMap = new Map();
  if (persistentSeed) {
    persistentMap.set(SESSION_KEY, JSON.stringify(persistentSeed));
  }
  const wrapMap = (map) => ({
    getItem: (k) => map.get(k) ?? null,
    setItem: (k, v) => map.set(k, v),
    removeItem: (k) => map.delete(k),
  });
  const storage = wrapMap(sessionMap);
  const persistentStorage = wrapMap(persistentMap);
  const location = {
    origin: 'https://asi.example.test', search: '', hash,
    pathname: '/account/link',
  };
  const history = { replaceState: () => {} };
  return { storage, persistentStorage, persistentMap, location, history };
}

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

test('batch2e r2 R1-4a: carried links are stripped when the session is superseded', async () => {
  const fx = accountFixture({
    persistentSeed: { refreshToken: 'refresh-a1', rev: 'rev-a', owner: 'tab-0', sid: 'sid-a' },
  });
  const { storage, persistentStorage } = fx;
  // Arm the handoff and adopt a fragment token as account A.
  const { ensureFragmentHandoff } = await import('../src/account-session.js');
  const nonce = ensureFragmentHandoff(storage);
  fx.location.hash = `#access_token=tok-a&state=${nonce}`;
  const sessionA = createFragmentSession({ ...fx, storage, persistentStorage });
  assert.equal(sessionA.getTokenSync(), 'tok-a');
  const { anchor, document, target, listeners } = fakeDocument();
  const refresh = installCarriedLinkRefresh({
    target, document, tokenProvider: sessionA.getToken,
    session: sessionA, storage, persistentStorage,
  });
  await refresh();
  assert.ok(anchor.href.includes('#access_token=tok-a'), anchor.href);
  assert.ok(anchor.href.includes('lineage=sid-a'), anchor.href);
  // Account B replaces the shared session (new lineage).
  fx.persistentMap.set(SESSION_KEY, JSON.stringify({
    refreshToken: 'refresh-b1', rev: 'rev-b', owner: 'tab-b', sid: 'sid-b',
  }));
  // The next refresh finds the session gone: the carried
  // token is stripped from the anchor, not left to be followed.
  await refresh();
  assert.equal(anchor.href, '/account/purchase');
  // And a click now cannot resurrect it from a cached token:
  // the synchronous fence answers null and the anchor stays bare.
  listeners.click.fn({ target: anchor });
  assert.equal(anchor.href, '/account/purchase');
});

test('batch2e r2 R1-4b: the destination refuses a token carried under a moved lineage', async () => {
  // Round-3 shape: the carry is produced by the real carrying
  // code (which records the binding next to the nonce), then
  // the shared session is replaced before the destination
  // loads. A hand-written fragment with a lineage claim but
  // no carry record is refused as well (see the r3 tests).
  const { carryTokenToAccountLinks } = await import('../src/account-session.js');
  const fx = accountFixture({
    persistentSeed: { refreshToken: 'refresh-a1', rev: 'rev-a', owner: 'tab-0', sid: 'sid-a' },
  });
  const { anchor, document } = fakeDocument();
  assert.equal(carryTokenToAccountLinks(
    document, 'tok-a', fx.storage, fx.persistentStorage), 1);
  assert.ok(anchor.href.includes('lineage=sid-a'), anchor.href);
  const carriedHash = anchor.href.slice(anchor.href.indexOf('#'));
  // B replaces the shared session before the link is followed.
  fx.persistentMap.set(SESSION_KEY, JSON.stringify({
    refreshToken: 'refresh-b1', rev: 'rev-b', owner: 'tab-b', sid: 'sid-b',
  }));
  fx.location.hash = carriedHash;
  const refused = createFragmentSession({ ...fx });
  assert.equal(await refused.getToken(), null);
  // The same carry, followed while its lineage is still
  // current, is adopted.
  const fx2 = accountFixture({
    persistentSeed: { refreshToken: 'refresh-b1', rev: 'rev-b', owner: 'tab-b', sid: 'sid-b' },
  });
  const doc2 = fakeDocument();
  assert.equal(carryTokenToAccountLinks(
    doc2.document, 'tok-b', fx2.storage, fx2.persistentStorage), 1);
  fx2.location.hash = doc2.anchor.href.slice(doc2.anchor.href.indexOf('#'));
  const accepted = createFragmentSession({ ...fx2 });
  assert.equal(await accepted.getToken(), 'tok-b');
});

test('batch2e r3 R1-4a-click: the click fence strips a stale carry without an intervening refresh', async () => {
  // Round-3 pin (Astra round 2, test defect): the cache is
  // populated by a refresh, the lineage is then replaced, and
  // the click happens WITHOUT another refresh. A click handler
  // that trusts the cached token would rewrite the anchor with
  // the stale token; the fence must strip it instead.
  const fx = accountFixture({
    persistentSeed: { refreshToken: 'refresh-a1', rev: 'rev-a', owner: 'tab-0', sid: 'sid-a' },
  });
  const { storage, persistentStorage } = fx;
  const { ensureFragmentHandoff } = await import('../src/account-session.js');
  const nonce = ensureFragmentHandoff(storage);
  fx.location.hash = `#access_token=tok-a&state=${nonce}`;
  const sessionA = createFragmentSession({ ...fx, storage, persistentStorage });
  assert.equal(sessionA.getTokenSync(), 'tok-a');
  const { anchor, document, target, listeners } = fakeDocument();
  const refresh = installCarriedLinkRefresh({
    target, document, tokenProvider: sessionA.getToken,
    session: sessionA, storage, persistentStorage,
  });
  await refresh();
  assert.ok(anchor.href.includes('#access_token=tok-a'), anchor.href);
  // B replaces the shared session. NO refresh intervenes.
  fx.persistentMap.set(SESSION_KEY, JSON.stringify({
    refreshToken: 'refresh-b1', rev: 'rev-b', owner: 'tab-b', sid: 'sid-b',
  }));
  listeners.click.fn({ target: anchor });
  assert.equal(anchor.href, '/account/purchase');
});
