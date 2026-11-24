import test from 'node:test';
import assert from 'node:assert/strict';
import { createAppAuth } from '../src/app-auth.js';

// Batch 2g round 2 regressions (Astra round 1, R1): the
// revision-only in-flight fence and the access-only adoption
// still paired tokens with the wrong shared state.
//
// 1. A refresh in flight for an UNNAMED (legacy) session whose
//    record is replaced mid-flight by a foreign record that
//    keeps revision null: the response must be discarded, the
//    replacement must not be overwritten, and the old access
//    token must never be served.
// 2. An access-only response to a fresh code exchange must not
//    be adopted: it would serve the new account's access token
//    under the retained session's binding.
// 3. An access-only rotation of a legacy session names the
//    record (same token, atomic shape), and a later foreign
//    legacy record is then told apart from it.
// 4. A foreign record reusing this session's exact revision
//    and lineage with a different token is supersession (the
//    token itself is part of the serving comparison).
// 5. Control: an access-only rotation of a named session
//    still succeeds (refresh-token-unchanged semantics).

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

const SESSION = 'si-app-session-v1';
const readRecord = (persistent) => JSON.parse(persistent.get(SESSION));

test('batch2g-r2 R1: an in-flight legacy refresh cannot commit over a foreign replacement', async () => {
  let releaseFirst;
  const firstResponse = new Promise((resolve) => { releaseFirst = resolve; });
  let calls = 0;
  const { make, persistent } = fixture({
    responder: () => {
      calls += 1;
      if (calls === 1) return firstResponse;
      return tokenResponse({
        access_token: 'access-B2', refresh_token: 'refresh-B2', expires_in: 3600,
      });
    },
  });
  persistent.set(SESSION, JSON.stringify({ refreshToken: 'refresh-A', savedAt: 1 }));
  const auth = make();
  const pending = auth.getToken();
  // Another tab replaces the shared session mid-flight with a
  // record that carries a lineage but no revision of its own:
  // both snapshots report revision null.
  persistent.set(SESSION, JSON.stringify({ refreshToken: 'refresh-B', sid: 'sid-B' }));
  releaseFirst(tokenResponse({
    access_token: 'access-A', refresh_token: 'refresh-A2', expires_in: 3600,
  }));
  const served = await pending;
  assert.notEqual(served, 'access-A');
  assert.equal(served, 'access-B2');
  // A's rotation never landed on the shared record.
  assert.equal(readRecord(persistent).refreshToken, 'refresh-B2');
  let dispatched = 'unset';
  await auth.withCurrentToken((token) => { dispatched = token; });
  assert.equal(dispatched, 'access-B2');
});

test('batch2g-r2 R1: an access-only code exchange is refused, the old session stands', async () => {
  const { make, session, persistent, location } = fixture({
    responder: async (url, options) => {
      const body = new URLSearchParams(options.body);
      if (body.get('grant_type') === 'refresh_token') {
        return tokenResponse({
          access_token: 'access-A2', refresh_token: 'refresh-A2', expires_in: 3600,
        });
      }
      if (body.get('grant_type') === 'authorization_code') {
        return tokenResponse({ access_token: 'access-B', expires_in: 3600 });
      }
      return tokenResponse({});
    },
  });
  persistent.set(SESSION, JSON.stringify({
    refreshToken: 'refresh-A', rev: 'rev-A', owner: 'other-tab', sid: 'sid-A', savedAt: 1,
  }));
  const auth = make();
  assert.equal(await auth.getToken(), 'access-A2');
  // A fresh login for account B on the same instance; the
  // exchange returns an access token with NO refresh token.
  await auth.beginLogin();
  const pendingLogin = JSON.parse(session.get('si-app-pkce-v1'));
  location.search = `?code=code-b&state=${pendingLogin.state}`;
  assert.equal(await auth.completeRedirect(), false);
  // B's access token is never served; A's session is intact.
  let dispatched = 'unset';
  await auth.withCurrentToken((token) => { dispatched = token; });
  assert.equal(dispatched, 'access-A2');
  assert.equal(readRecord(persistent).refreshToken, 'refresh-A2');
  assert.equal(readRecord(persistent).sid, 'sid-A');
});

test('batch2g-r2 R1: an access-only rotation names a legacy record; a foreign legacy record is told apart', async () => {
  const { make, persistent } = fixture({
    responder: async (url, options) => {
      const body = new URLSearchParams(options.body);
      if (body.get('refresh_token') === 'refresh-A') {
        return tokenResponse({ access_token: 'access-A', expires_in: 3600 });
      }
      if (body.get('refresh_token') === 'refresh-B') {
        return tokenResponse({
          access_token: 'access-B', refresh_token: 'refresh-B2', expires_in: 3600,
        });
      }
      return tokenResponse({});
    },
  });
  persistent.set(SESSION, JSON.stringify({ refreshToken: 'refresh-A', savedAt: 1 }));
  const auth = make();
  assert.equal(await auth.getToken(), 'access-A');
  // The record was named in place: same token, atomic shape.
  const named = readRecord(persistent);
  assert.equal(named.refreshToken, 'refresh-A');
  assert.equal(typeof named.rev, 'string');
  assert.notEqual(named.rev, '');
  assert.equal(typeof named.sid, 'string');
  assert.notEqual(named.sid, '');
  // A foreign legacy record replaces it: A's access token is
  // not served; the tab follows the shared record to B.
  persistent.set(SESSION, JSON.stringify({ refreshToken: 'refresh-B', savedAt: 2 }));
  let dispatched = 'unset';
  await auth.withCurrentToken((token) => { dispatched = token; });
  assert.equal(dispatched, 'access-B');
});

test('batch2g-r2 R1: a foreign record reusing the exact binding with a different token supersedes', async () => {
  const { make, persistent } = fixture({
    responder: async (url, options) => {
      const body = new URLSearchParams(options.body);
      if (body.get('refresh_token') === 'refresh-A') {
        return tokenResponse({
          access_token: 'access-A2', refresh_token: 'refresh-A2', expires_in: 3600,
        });
      }
      if (body.get('refresh_token') === 'refresh-FOREIGN') {
        return tokenResponse({
          access_token: 'access-F', refresh_token: 'refresh-F2', expires_in: 3600,
        });
      }
      return tokenResponse({});
    },
  });
  persistent.set(SESSION, JSON.stringify({
    refreshToken: 'refresh-A', rev: 'rev-A', owner: 'other-tab', sid: 'sid-A', savedAt: 1,
  }));
  const auth = make();
  assert.equal(await auth.getToken(), 'access-A2');
  // A foreign writer copies the record's shape exactly —
  // same revision, same lineage — but swaps the token.
  const current = readRecord(persistent);
  persistent.set(SESSION, JSON.stringify({ ...current, refreshToken: 'refresh-FOREIGN' }));
  let dispatched = 'unset';
  await auth.withCurrentToken((token) => { dispatched = token; });
  assert.equal(dispatched, 'access-F');
});

test('batch2g-r2 R1 control: an access-only rotation of a named session still succeeds', async () => {
  const { make, persistent } = fixture({
    responder: async () => tokenResponse({ access_token: 'access-A9', expires_in: 3600 }),
  });
  persistent.set(SESSION, JSON.stringify({
    refreshToken: 'refresh-A', rev: 'rev-A', owner: 'other-tab', sid: 'sid-A', savedAt: 1,
  }));
  const auth = make();
  assert.equal(await auth.getToken(), 'access-A9');
  const record = readRecord(persistent);
  assert.equal(record.refreshToken, 'refresh-A');
  assert.equal(record.sid, 'sid-A');
});
