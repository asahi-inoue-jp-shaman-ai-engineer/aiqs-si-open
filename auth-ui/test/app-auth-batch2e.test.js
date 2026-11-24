import test from 'node:test';
import assert from 'node:assert/strict';
import { createAppAuth } from '../src/app-auth.js';

// Batch 2e regressions (Astra glass round 2, R1): the three
// cross-tab sequences the two-record fence still failed.
//
// 1. Adoption whose shared commit fails must abort: the tab
//    holds nothing, and the previous account's tab keeps
//    dispatching — never both accounts at once.
// 2. A foreign adoption landing between this tab's load and
//    its refresh response is adopted as ONE snapshot: the
//    response is discarded and the shared record is not
//    overwritten by the loser's rotated token.
// 3. Disappearance (or corruption) of an established shared
//    record invalidates this tab's session: an unexpired
//    in-memory access token is NOT served.

function fixture({ responder } = {}) {
  const session = new Map();
  const persistent = new Map();
  const control = { failSessionWrites: false };
  const wrap = (map) => ({
    getItem: (k) => map.get(k) ?? null,
    setItem: (k, v) => map.set(k, v),
    removeItem: (k) => map.delete(k),
  });
  const persistentStorage = {
    getItem: (k) => persistent.get(k) ?? null,
    setItem: (k, v) => {
      if (control.failSessionWrites && k === 'si-app-session-v1') {
        throw new Error('storage blocked');
      }
      persistent.set(k, v);
    },
    removeItem: (k) => persistent.delete(k),
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
  return { make, session, persistent, control, location };
}

const tokenResponse = (data) => ({
  ok: true, status: 200, json: async () => data,
});

test('batch2e R1-1: an adoption whose commit fails aborts; the old session stays sole', async () => {
  const { make, session, persistent, control, location } = fixture({
    responder: async (url, options) => {
      const body = new URLSearchParams(options.body);
      if (body.get('grant_type') === 'refresh_token'
          && body.get('refresh_token') === 'refresh-a1') {
        return tokenResponse({
          access_token: 'access-a', refresh_token: 'refresh-a2', expires_in: 3600,
        });
      }
      if (body.get('grant_type') === 'authorization_code') {
        return tokenResponse({
          access_token: 'access-b', refresh_token: 'refresh-b1', expires_in: 3600,
        });
      }
      return tokenResponse({});
    },
  });
  persistent.set('si-app-session-v1',
    JSON.stringify({ refreshToken: 'refresh-a1', savedAt: 1 }));
  const tabA = make();
  assert.equal(await tabA.getToken(), 'access-a');
  // Tab B completes the code exchange for account B, but the
  // shared commit (the session record write) fails: B's
  // adoption aborts and B holds nothing at all.
  control.failSessionWrites = true;
  const tabB = make();
  await tabB.beginLogin();
  const pending = JSON.parse(session.get('si-app-pkce-v1'));
  location.search = `?code=synthetic-code&state=${pending.state}`;
  assert.equal(await tabB.completeRedirect(), false);
  // B holds no credentials of its own: nothing it can serve or
  // dispatch is account B's. (hasPersistedSession is not the
  // measure here — the persisted session it reports is A's,
  // which is exactly the point: A's record survived untouched.)
  assert.equal(await tabB.getToken(), null);
  let dispatchedByB = 'unset';
  await tabB.withCurrentToken((token) => { dispatchedByB = token; });
  assert.notEqual(dispatchedByB, 'access-b');
  // The shared record still belongs to A alone, and A keeps
  // dispatching its own token — the two accounts are never
  // live at once.
  const record = JSON.parse(persistent.get('si-app-session-v1'));
  assert.equal(record.refreshToken, 'refresh-a2');
  location.search = '';
  assert.equal(await tabA.getToken(), 'access-a');
});

test('batch2e R1-2: a foreign atomic adoption mid-refresh is never overwritten by the loser', async () => {
  let releaseRefresh;
  const gate = new Promise((resolve) => { releaseRefresh = resolve; });
  const { make, persistent } = fixture({
    responder: async (url, options) => {
      const body = new URLSearchParams(options.body);
      const presented = body.get('refresh_token');
      if (presented === 'refresh-a1') {
        await gate;
        return tokenResponse({
          access_token: 'access-a2', refresh_token: 'refresh-a2', expires_in: 3600,
        });
      }
      if (presented === 'refresh-b1') {
        return tokenResponse({
          access_token: 'access-b', refresh_token: 'refresh-b2', expires_in: 3600,
        });
      }
      return tokenResponse({});
    },
  });
  persistent.set('si-app-session-v1',
    JSON.stringify({ refreshToken: 'refresh-a1', savedAt: 1 }));
  const tabA = make();
  const pending = tabA.getToken();
  await new Promise((resolve) => setImmediate(resolve));
  // Tab B's adoption lands as ONE atomic record (the batch 2e
  // shape) while A's refresh is in flight.
  persistent.set('si-app-session-v1', JSON.stringify({
    refreshToken: 'refresh-b1', rev: 'rev-b', owner: 'tab-b',
    sid: 'sid-b', savedAt: 2,
  }));
  releaseRefresh();
  const served = await pending;
  assert.notEqual(served, 'access-a2');
  // The shared record still belongs to B: A's rotated token
  // was never persisted over it.
  const record = JSON.parse(persistent.get('si-app-session-v1'));
  assert.equal(record.sid, 'sid-b');
  assert.ok(['refresh-b1', 'refresh-b2'].includes(record.refreshToken));
});

test('batch2e R1-3: disappearance of the shared record invalidates the in-memory session', async () => {
  const { make, persistent } = fixture({
    responder: async (url, options) => {
      const body = new URLSearchParams(options.body);
      if (body.get('refresh_token') === 'refresh-a1') {
        return tokenResponse({
          access_token: 'access-a', refresh_token: 'refresh-a2', expires_in: 3600,
        });
      }
      return tokenResponse({});
    },
  });
  persistent.set('si-app-session-v1',
    JSON.stringify({ refreshToken: 'refresh-a1', savedAt: 1 }));
  const tabA = make();
  assert.equal(await tabA.getToken(), 'access-a');
  // The established record vanishes (eviction, external
  // clear): the still-unexpired access token must NOT be
  // served — disappearance is invalidation.
  persistent.delete('si-app-session-v1');
  persistent.delete('si-app-session-epoch-v1');
  assert.equal(await tabA.getToken(), null);
  let dispatched = 'unset';
  await tabA.withCurrentToken((token) => { dispatched = token; });
  assert.equal(dispatched, null);
});

test('batch2e R1-3b: corruption of the shared record invalidates the in-memory session', async () => {
  const { make, persistent } = fixture({
    responder: async (url, options) => {
      const body = new URLSearchParams(options.body);
      if (body.get('refresh_token') === 'refresh-a1') {
        return tokenResponse({
          access_token: 'access-a', refresh_token: 'refresh-a2', expires_in: 3600,
        });
      }
      return tokenResponse({});
    },
  });
  persistent.set('si-app-session-v1',
    JSON.stringify({ refreshToken: 'refresh-a1', savedAt: 1 }));
  const tabA = make();
  assert.equal(await tabA.getToken(), 'access-a');
  persistent.set('si-app-session-v1', 'not-json{');
  persistent.delete('si-app-session-epoch-v1');
  assert.equal(await tabA.getToken(), null);
});
