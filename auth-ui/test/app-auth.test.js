import test from 'node:test';
import assert from 'node:assert/strict';
import { createAppAuth } from '../src/app-auth.js';
import { createAppClient } from '../src/app-api.js';

function fixture({ search = '', responder } = {}) {
  const items = new Map();
  const storage = {
    getItem: (k) => items.get(k) ?? null,
    setItem: (k, v) => items.set(k, v),
    removeItem: (k) => items.delete(k),
  };
  const location = {
    origin: 'https://asi.example.test', search, hash: '', pathname: '/app',
    assigned: null, assign(url) { this.assigned = url; },
  };
  const history = { replaced: [], replaceState(...args) { this.replaced.push(args); } };
  const calls = [];
  const fetch = async (url, options) => {
    calls.push([url, options]);
    return responder(url, options);
  };
  const auth = createAppAuth({
    fetch, location, history, storage, crypto: globalThis.crypto,
  });
  return { auth, items, location, history, calls };
}

const tokenResponse = (data) => ({
  ok: true, status: 200, json: async () => data,
});

test('beginLogin stores a PKCE pair and navigates to the realm', async () => {
  const { auth, items, location } = fixture({ responder: () => tokenResponse({}) });
  await auth.beginLogin();
  const pending = JSON.parse(items.get('si-app-pkce-v1'));
  assert.ok(pending.verifier.length >= 32);
  assert.ok(pending.state.length >= 16);
  const url = new URL(location.assigned);
  assert.equal(url.origin + url.pathname,
    'https://asi.example.test/realms/si/protocol/openid-connect/auth');
  assert.equal(url.searchParams.get('client_id'), 'si-app');
  assert.equal(url.searchParams.get('response_type'), 'code');
  assert.equal(url.searchParams.get('scope'), 'aiqs:use');
  assert.equal(url.searchParams.get('redirect_uri'), 'https://asi.example.test/app');
  assert.equal(url.searchParams.get('resource'), 'https://asi.example.test/mcp');
  assert.equal(url.searchParams.get('code_challenge_method'), 'S256');
  assert.equal(url.searchParams.get('state'), pending.state);
  // The challenge is the SHA-256 of the stored verifier.
  const digest = await crypto.subtle.digest(
    'SHA-256', new TextEncoder().encode(pending.verifier));
  const expected = Buffer.from(digest).toString('base64url');
  assert.equal(url.searchParams.get('code_challenge'), expected);
});

test('completeRedirect exchanges the code and keeps tokens in memory only', async () => {
  const setup = fixture({ responder: () => tokenResponse({}) });
  await setup.auth.beginLogin();
  const pending = JSON.parse(setup.items.get('si-app-pkce-v1'));
  const { auth, items, history, calls } = fixture({
    search: `?code=synthetic-code&state=${pending.state}`,
    responder: () => tokenResponse({
      access_token: 'access-1', refresh_token: 'refresh-1', expires_in: 3600,
    }),
  });
  items.set('si-app-pkce-v1', JSON.stringify(pending));
  assert.equal(await auth.completeRedirect(), true);
  assert.equal(items.has('si-app-pkce-v1'), false);
  assert.deepEqual(history.replaced[0].slice(2), ['/app']);
  const [url, options] = calls[0];
  assert.equal(url, 'https://asi.example.test/realms/si/protocol/openid-connect/token');
  const body = new URLSearchParams(options.body);
  assert.equal(body.get('grant_type'), 'authorization_code');
  assert.equal(body.get('code_verifier'), pending.verifier);
  assert.equal(await auth.getToken(), 'access-1');
});

test('completeRedirect rejects a state mismatch and clears the URL', async () => {
  const { auth, items, history } = fixture({
    search: '?code=synthetic-code&state=wrong',
    responder: () => tokenResponse({ access_token: 'x' }),
  });
  items.set('si-app-pkce-v1', JSON.stringify({ verifier: 'v', state: 'right' }));
  assert.equal(await auth.completeRedirect(), false);
  assert.equal(await auth.getToken(), null);
  assert.equal(history.replaced.length, 1);
});

function persistentFixture({ responder } = {}) {
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
  const calls = [];
  const fetch = async (url, options) => {
    calls.push([url, options]);
    return responder(url, options);
  };
  const make = () => createAppAuth({
    fetch, location, history, storage: wrap(session),
    persistentStorage: wrap(persistent), crypto: globalThis.crypto,
  });
  return { make, session, persistent, location, history, calls };
}

test('the refresh token persists and a fresh instance silently restores', async () => {
  const grants = [];
  const { make, persistent } = persistentFixture({
    responder: (url, options) => {
      const body = new URLSearchParams(options.body);
      grants.push(body.get('grant_type'));
      if (body.get('grant_type') === 'refresh_token') {
        assert.equal(body.get('refresh_token'), 'refresh-1');
        return tokenResponse({ access_token: 'access-2', refresh_token: 'refresh-2', expires_in: 3600 });
      }
      return tokenResponse({});
    },
  });
  // Simulate the state right after a completed login redirect.
  const first = make();
  persistent.set('si-app-session-v1', JSON.stringify({ refreshToken: 'refresh-1', savedAt: 1 }));
  assert.equal(first.hasPersistedSession(), true);
  assert.equal(await first.getToken(), 'access-2');
  // Rotation: the newest refresh token replaces the stored one.
  assert.equal(JSON.parse(persistent.get('si-app-session-v1')).refreshToken, 'refresh-2');
  assert.deepEqual(grants, ['refresh_token']);
});

test('a rejected refresh clears the persisted session', async () => {
  const { make, persistent } = persistentFixture({
    responder: () => ({ ok: false, status: 400, json: async () => ({ error: 'invalid_grant' }) }),
  });
  const auth = make();
  persistent.set('si-app-session-v1', JSON.stringify({ refreshToken: 'stale', savedAt: 1 }));
  assert.equal(await auth.getToken(), null);
  assert.equal(persistent.has('si-app-session-v1'), false);
  assert.equal(auth.hasPersistedSession(), false);
});

test('a network failure keeps the persisted session for a later retry', async () => {
  let down = true;
  const { make, persistent } = persistentFixture({
    responder: () => {
      if (down) throw new Error('offline');
      return tokenResponse({ access_token: 'back', refresh_token: 'refresh-1', expires_in: 3600 });
    },
  });
  const auth = make();
  persistent.set('si-app-session-v1', JSON.stringify({ refreshToken: 'refresh-1', savedAt: 1 }));
  assert.equal(await auth.getToken(), null);
  assert.equal(persistent.has('si-app-session-v1'), true);
  down = false;
  assert.equal(await auth.getToken(), 'back');
});

test('concurrent getToken calls share a single refresh', async () => {
  let refreshes = 0;
  const { make, persistent } = persistentFixture({
    responder: async () => {
      refreshes += 1;
      await new Promise((resolve) => setTimeout(resolve, 10));
      return tokenResponse({ access_token: 'shared', refresh_token: 'refresh-1', expires_in: 3600 });
    },
  });
  const auth = make();
  persistent.set('si-app-session-v1', JSON.stringify({ refreshToken: 'refresh-1', savedAt: 1 }));
  const [a, b, c] = await Promise.all([auth.getToken(), auth.getToken(), auth.getToken()]);
  assert.deepEqual([a, b, c], ['shared', 'shared', 'shared']);
  assert.equal(refreshes, 1);
});

test('an expired access token is refreshed once', async () => {
  const setup = fixture({ responder: () => tokenResponse({}) });
  await setup.auth.beginLogin();
  const pending = JSON.parse(setup.items.get('si-app-pkce-v1'));
  let grants = [];
  const { auth, items } = fixture({
    search: `?code=c&state=${pending.state}`,
    responder: (url, options) => {
      const grant = new URLSearchParams(options.body).get('grant_type');
      grants.push(grant);
      if (grant === 'authorization_code') {
        return tokenResponse({ access_token: 'a1', refresh_token: 'r1', expires_in: 0 });
      }
      return tokenResponse({ access_token: 'a2', refresh_token: 'r2', expires_in: 3600 });
    },
  });
  items.set('si-app-pkce-v1', JSON.stringify(pending));
  await auth.completeRedirect();
  assert.equal(await auth.getToken(), 'a2');
  assert.deepEqual(grants, ['authorization_code', 'refresh_token']);
});

test('takeFragment adopts only a state-bound handoff token', async () => {
  const { auth, items, location } = fixture({ responder: () => tokenResponse({}) });
  // Unsolicited: no state at all (the Astra attack shape).
  location.hash = '#access_token=ATTACKER';
  assert.equal(auth.takeFragment(), false);
  assert.equal(await auth.getToken(), null);
  // The nonce this browser armed at construction is the only key.
  const state = items.get('si-fragment-handoff-v1');
  assert.ok(state);
  location.hash = `#access_token=handed-over&state=${state}`;
  assert.equal(auth.takeFragment(), true);
  assert.equal(await auth.getToken(), 'handed-over');
  // Consumed: the same link cannot be replayed.
  location.hash = `#access_token=handed-over&state=${state}`;
  assert.equal(auth.takeFragment(), false);
});

test('a fragment adoption clears a persisted session from another account', async () => {
  const { make, session, persistent, location } = persistentFixture({
    responder: () => tokenResponse({}),
  });
  persistent.set('si-app-session-v1',
    JSON.stringify({ refreshToken: 'someone-elses', savedAt: 1 }));
  const auth = make();
  const state = session.get('si-fragment-handoff-v1');
  assert.ok(state);
  location.hash = `#access_token=mine-now&state=${state}`;
  assert.equal(auth.takeFragment(), true);
  assert.equal(await auth.getToken(), 'mine-now');
  assert.equal(persistent.has('si-app-session-v1'), false);
  assert.equal(auth.hasPersistedSession(), false);
});

test('a delayed refresh cannot resurrect the previous account after a handoff', async () => {
  let releaseRefresh;
  const gate = new Promise((resolve) => { releaseRefresh = resolve; });
  const { make, session, persistent, location } = persistentFixture({
    responder: async (url, options) => {
      const grant = new URLSearchParams(options.body).get('grant_type');
      if (grant === 'refresh_token') {
        await gate;
        return tokenResponse({
          access_token: 'a-access-2', refresh_token: 'a-refresh-2', expires_in: 3600,
        });
      }
      return tokenResponse({});
    },
  });
  persistent.set('si-app-session-v1',
    JSON.stringify({ refreshToken: 'a-refresh-1', savedAt: 1 }));
  const auth = make();
  const pending = auth.getToken(); // starts account A's refresh
  // While it is in flight, a state-bound handoff for account B lands.
  const state = session.get('si-fragment-handoff-v1');
  assert.ok(state);
  location.hash = '#' + new URLSearchParams({ access_token: 'b-access', state }).toString();
  assert.equal(auth.takeFragment(), true);
  assert.equal(await auth.getToken(), 'b-access');
  releaseRefresh();
  // A's late response is discarded by the generation check: the
  // session stays B's, in memory only, with nothing persisted.
  assert.equal(await pending, 'b-access');
  assert.equal(await auth.getToken(), 'b-access');
  assert.equal(persistent.has('si-app-session-v1'), false);
  assert.equal(auth.hasPersistedSession(), false);
});

test('a stale rejection cannot clear the session a handoff just created', async () => {
  // Astra diff review round 2 (Item 1, MEDIUM): the refresh for
  // account A is in flight; its 'rejected' outcome is produced
  // while a state-bound handoff for account B lands before the
  // refresh continuation interprets the outcome. The continuation
  // re-checks the authentication generation and touches nothing.
  let releaseRefresh;
  const gate = new Promise((resolve) => { releaseRefresh = resolve; });
  const { make, session, persistent, location } = persistentFixture({
    responder: async (url, options) => {
      const grant = new URLSearchParams(options.body).get('grant_type');
      if (grant === 'refresh_token') {
        await gate;
        return { ok: false, status: 400, json: async () => ({ error: 'invalid_grant' }) };
      }
      return tokenResponse({});
    },
  });
  persistent.set('si-app-session-v1',
    JSON.stringify({ refreshToken: 'a-refresh-1', savedAt: 1 }));
  const auth = make();
  const pending = auth.getToken(); // starts account A's refresh
  releaseRefresh();
  // Let the token request settle, then adopt B's handoff before
  // the pending getToken() settles.
  await Promise.resolve();
  const state = session.get('si-fragment-handoff-v1');
  assert.ok(state);
  location.hash = '#' + new URLSearchParams({ access_token: 'b-access', state }).toString();
  assert.equal(auth.takeFragment(), true);
  assert.equal(await pending, 'b-access');
  assert.equal(await auth.getToken(), 'b-access');
  assert.equal(persistent.has('si-app-session-v1'), false);
});

test('a generation change during a deferred response body still discards the old account', async () => {
  // Pins the generation check AFTER response.json() resolves
  // (Astra delta review): the refresh response headers arrive,
  // a state-bound handoff for account B lands, and only THEN
  // does account A's body finish parsing. Deleting the post-
  // json() generation check makes this test fail — A's tokens
  // would be adopted and persisted underneath B's session.
  let releaseBody;
  let bodyRequested;
  const bodyGate = new Promise((resolve) => { releaseBody = resolve; });
  const bodyCalled = new Promise((resolve) => { bodyRequested = resolve; });
  const { make, session, persistent, location } = persistentFixture({
    responder: async (url, options) => {
      const grant = new URLSearchParams(options.body).get('grant_type');
      if (grant === 'refresh_token') {
        return {
          ok: true, status: 200,
          json: async () => {
            bodyRequested();
            await bodyGate;
            return {
              access_token: 'a-access-2', refresh_token: 'a-refresh-2', expires_in: 3600,
            };
          },
        };
      }
      return tokenResponse({});
    },
  });
  persistent.set('si-app-session-v1',
    JSON.stringify({ refreshToken: 'a-refresh-1', savedAt: 1 }));
  const auth = make();
  const pending = auth.getToken(); // starts account A's refresh
  await bodyCalled; // A's response is now mid-parse
  const state = session.get('si-fragment-handoff-v1');
  assert.ok(state);
  location.hash = '#' + new URLSearchParams({ access_token: 'b-access', state }).toString();
  assert.equal(auth.takeFragment(), true);
  releaseBody();
  assert.equal(await pending, 'b-access');
  assert.equal(await auth.getToken(), 'b-access');
  // A's rotated refresh token was never persisted under B.
  assert.equal(persistent.has('si-app-session-v1'), false);
  assert.equal(auth.hasPersistedSession(), false);
});


test('cross-tab: a late refresh for the old account cannot resurrect it (batch 2d HIGH 1)', async () => {
  // Astra's reproduction: tab 1 holds account A's persisted
  // session and its refresh is in flight; tab 2 adopts account
  // B from a fragment handoff (which clears the shared session);
  // tab 1's slow response then lands. Before the fix it
  // re-persisted A's rotated refresh token, and the next load
  // silently restored A.
  let releaseRefresh;
  const gate = new Promise((resolve) => { releaseRefresh = resolve; });
  const { make, session, persistent, location } = persistentFixture({
    responder: async (url, options) => {
      const body = new URLSearchParams(options.body);
      if (body.get('grant_type') === 'refresh_token'
          && body.get('refresh_token') === 'refresh-a1') {
        await gate;
        return tokenResponse({
          access_token: 'access-a2', refresh_token: 'refresh-a2',
          expires_in: 3600,
        });
      }
      return tokenResponse({});
    },
  });
  persistent.set('si-app-session-v1',
    JSON.stringify({ refreshToken: 'refresh-a1', savedAt: 1 }));
  const tabA = make();
  const pending = tabA.getToken();
  // Let the refresh reach the (gated) token endpoint.
  await new Promise((resolve) => setImmediate(resolve));
  // Tab 2 adopts B through the state-bound fragment handoff.
  const tabB = make();
  const nonce = session.get('si-fragment-handoff-v1');
  assert.ok(nonce);
  location.hash = `#access_token=access-b&state=${nonce}`;
  assert.equal(tabB.takeFragment(), true);
  assert.equal(persistent.has('si-app-session-v1'), false);
  // Tab 1's response for A finally lands — and is discarded.
  releaseRefresh();
  assert.equal(await pending, null);
  assert.equal(persistent.has('si-app-session-v1'), false);
  // A fresh tab finds no session to resurrect.
  const tabC = make();
  assert.equal(tabC.hasPersistedSession(), false);
  assert.equal(await tabC.getToken(), null);
});

test('cross-tab: a rotation race resolves through the newer persisted token', async () => {
  // Both tabs refresh with the same token; the loser of the
  // race must not be signed out — it retries once with the
  // winner's rotated token.
  let releaseFirst;
  const gate = new Promise((resolve) => { releaseFirst = resolve; });
  let firstCalls = 0;
  const { make, persistent } = persistentFixture({
    responder: async (url, options) => {
      const body = new URLSearchParams(options.body);
      const presented = body.get('refresh_token');
      if (presented === 'refresh-1') {
        firstCalls += 1;
        if (firstCalls === 1) {
          await gate;
          return tokenResponse({
            access_token: 'access-late', refresh_token: 'refresh-late',
            expires_in: 3600,
          });
        }
        return tokenResponse({
          access_token: 'access-b2', refresh_token: 'refresh-2',
          expires_in: 3600,
        });
      }
      if (presented === 'refresh-2') {
        return tokenResponse({
          access_token: 'access-a3', refresh_token: 'refresh-3',
          expires_in: 3600,
        });
      }
      return tokenResponse({});
    },
  });
  persistent.set('si-app-session-v1',
    JSON.stringify({ refreshToken: 'refresh-1', savedAt: 1 }));
  const tabA = make();
  const pendingA = tabA.getToken();
  await new Promise((resolve) => setImmediate(resolve));
  const tabB = make();
  assert.equal(await tabB.getToken(), 'access-b2');
  releaseFirst();
  assert.equal(await pendingA, 'access-a3');
  assert.equal(JSON.parse(persistent.get('si-app-session-v1')).refreshToken,
    'refresh-3');
});

test('cross-tab: sign-out in another tab drops this tab session', async () => {
  const { make, persistent } = persistentFixture({
    responder: () => tokenResponse({
      access_token: 'access-a', refresh_token: 'refresh-2', expires_in: 3600,
    }),
  });
  persistent.set('si-app-session-v1',
    JSON.stringify({ refreshToken: 'refresh-1', savedAt: 1 }));
  const tabA = make();
  assert.equal(await tabA.getToken(), 'access-a');
  const tabB = make();
  tabB.clearSession();
  assert.equal(persistent.has('si-app-session-v1'), false);
  // Tab A must not keep acting on the signed-out session: its
  // next token request drops the stale in-memory credentials.
  assert.equal(await tabA.getToken(), null);
});


test('cross-tab: a fragment-only session is superseded by another tab (round 2)', async () => {
  // Round 1's supersession check required a refresh token, so
  // a tab holding ONLY a fragment session kept serving the old
  // account after another tab adopted a different one.
  const { make, session, persistent, location } = persistentFixture({
    responder: (url, options) => {
      const body = new URLSearchParams(options.body);
      if (body.get('refresh_token') === 'refresh-b1') {
        return tokenResponse({
          access_token: 'access-b2', refresh_token: 'refresh-b2',
          expires_in: 3600,
        });
      }
      return tokenResponse({});
    },
  });
  const tabA = make();
  const nonce = session.get('si-fragment-handoff-v1');
  assert.ok(nonce);
  location.hash = `#access_token=access-frag-a&state=${nonce}`;
  assert.equal(tabA.takeFragment(), true);
  assert.equal(await tabA.getToken(), 'access-frag-a');
  // Another tab signs in with its own account and persists it.
  persistent.set('si-app-session-v1',
    JSON.stringify({ refreshToken: 'refresh-b1', savedAt: 1 }));
  const tabB = make();
  assert.equal(await tabB.getToken(), 'access-b2');
  // Tab A's fragment session is superseded: it is never served
  // again (the call resolves the CURRENT shared session or
  // nothing — here nothing usable answers, so null).
  assert.notEqual(await tabA.getToken(), 'access-frag-a');
  assert.equal(await tabA.getToken(), null);
});

test('cross-tab: a fenced proactive refresh never falls back to the old token (round 2)', async () => {
  // The access token has 30s left, inside the refresh margin,
  // so getToken refreshes proactively. Another tab signs out
  // while the refresh is in flight; the fence discards the
  // response, and getToken must return null rather than the
  // still-valid old access token (Astra's exact counterexample).
  let releaseRefresh;
  const gate = new Promise((resolve) => { releaseRefresh = resolve; });
  const { make, persistent } = persistentFixture({
    responder: async (url, options) => {
      const body = new URLSearchParams(options.body);
      const presented = body.get('refresh_token');
      if (presented === 'refresh-a1') {
        return tokenResponse({
          access_token: 'access-a1', refresh_token: 'refresh-a1r',
          expires_in: 30,
        });
      }
      if (presented === 'refresh-a1r') {
        await gate;
        return tokenResponse({
          access_token: 'access-a2', refresh_token: 'refresh-a2',
          expires_in: 3600,
        });
      }
      return tokenResponse({});
    },
  });
  persistent.set('si-app-session-v1',
    JSON.stringify({ refreshToken: 'refresh-a1', savedAt: 1 }));
  const tabA = make();
  assert.equal(await tabA.getToken(), 'access-a1');
  const pending = tabA.getToken(); // proactive refresh starts
  await new Promise((resolve) => setImmediate(resolve));
  const tabB = make();
  tabB.clearSession();
  releaseRefresh();
  assert.equal(await pending, null);
  assert.equal(await tabA.getToken(), null);
});

test('cross-tab: the fence survives a legacy record at the numeric ceiling (round 2)', async () => {
  // A numeric epoch at 2^53 cannot advance (+1 is a no-op), so
  // a write from another tab left the compared value unchanged
  // and an in-flight refresh for the old account was adopted.
  // String revisions have no ceiling: any foreign write moves
  // the fence.
  let releaseRefresh;
  const gate = new Promise((resolve) => { releaseRefresh = resolve; });
  const { make, persistent } = persistentFixture({
    responder: async (url, options) => {
      const body = new URLSearchParams(options.body);
      if (body.get('refresh_token') === 'refresh-a1') {
        await gate;
        return tokenResponse({
          access_token: 'access-a2', refresh_token: 'refresh-a2',
          expires_in: 3600,
        });
      }
      return tokenResponse({});
    },
  });
  persistent.set('si-app-session-v1',
    JSON.stringify({ refreshToken: 'refresh-a1', savedAt: 1 }));
  persistent.set('si-app-session-epoch-v1', JSON.stringify({
    epoch: 9007199254740992, owner: 'another-tab', kind: 'persist', at: 1,
  }));
  const tabA = make();
  const pending = tabA.getToken();
  await new Promise((resolve) => setImmediate(resolve));
  const tabB = make();
  tabB.clearSession();
  releaseRefresh();
  assert.equal(await pending, null);
  assert.equal(persistent.has('si-app-session-v1'), false);
  // The record now carries a string revision, not a number.
  const record = JSON.parse(persistent.get('si-app-session-epoch-v1'));
  assert.equal(typeof record.rev, 'string');
  assert.ok(record.rev.length >= 16);
});


test('cross-tab: a failed retry never serves the superseded access token (round 3)', async () => {
  // Astra's round-2 counterexample: A refreshes proactively;
  // B signs in; A's response is fenced off as superseded; A
  // retries with B's persisted token — and the retry FAILS.
  // The retry switch must retire A's access token with A's
  // refresh token: credentials and binding are one unit, and
  // the revision check alone passes once A rebinds to B.
  let releaseRefresh;
  const gate = new Promise((resolve) => { releaseRefresh = resolve; });
  const { make, persistent } = persistentFixture({
    responder: async (url, options) => {
      const body = new URLSearchParams(options.body);
      const presented = body.get('refresh_token');
      if (presented === 'refresh-a1') {
        return tokenResponse({
          access_token: 'access-a1', refresh_token: 'refresh-a1r',
          expires_in: 30,
        });
      }
      if (presented === 'refresh-a1r') {
        await gate;
        return tokenResponse({
          access_token: 'access-a2', refresh_token: 'refresh-a2',
          expires_in: 3600,
        });
      }
      if (presented === 'refresh-b1') {
        return tokenResponse({
          access_token: 'access-b', refresh_token: 'refresh-b2',
          expires_in: 3600,
        });
      }
      if (presented === 'refresh-b2') {
        throw new Error('transport down');
      }
      return tokenResponse({});
    },
  });
  persistent.set('si-app-session-v1',
    JSON.stringify({ refreshToken: 'refresh-a1', savedAt: 1 }));
  const tabA = make();
  assert.equal(await tabA.getToken(), 'access-a1');
  const pending = tabA.getToken(); // proactive refresh, gated
  await new Promise((resolve) => setImmediate(resolve));
  // B signs in over the shared storage.
  persistent.set('si-app-session-v1',
    JSON.stringify({ refreshToken: 'refresh-b1', savedAt: 1 }));
  const tabB = make();
  assert.equal(await tabB.getToken(), 'access-b');
  releaseRefresh();
  // A's response is discarded; the retry against B's token
  // fails on transport; nothing of A's may be served.
  assert.equal(await pending, null);
  assert.equal(await tabA.getToken(), null);
});

test('cross-tab: a malformed retry response never serves the old token (round 3)', async () => {
  let releaseRefresh;
  const gate = new Promise((resolve) => { releaseRefresh = resolve; });
  const { make, persistent } = persistentFixture({
    responder: async (url, options) => {
      const body = new URLSearchParams(options.body);
      const presented = body.get('refresh_token');
      if (presented === 'refresh-a1') {
        return tokenResponse({
          access_token: 'access-a1', refresh_token: 'refresh-a1r',
          expires_in: 30,
        });
      }
      if (presented === 'refresh-a1r') {
        await gate;
        return tokenResponse({
          access_token: 'access-a2', refresh_token: 'refresh-a2',
          expires_in: 3600,
        });
      }
      if (presented === 'refresh-b1') {
        return tokenResponse({
          access_token: 'access-b', refresh_token: 'refresh-b2',
          expires_in: 3600,
        });
      }
      // refresh-b2 answers with a body carrying no access token.
      return tokenResponse({});
    },
  });
  persistent.set('si-app-session-v1',
    JSON.stringify({ refreshToken: 'refresh-a1', savedAt: 1 }));
  const tabA = make();
  assert.equal(await tabA.getToken(), 'access-a1');
  const pending = tabA.getToken();
  await new Promise((resolve) => setImmediate(resolve));
  persistent.set('si-app-session-v1',
    JSON.stringify({ refreshToken: 'refresh-b1', savedAt: 1 }));
  const tabB = make();
  assert.equal(await tabB.getToken(), 'access-b');
  releaseRefresh();
  assert.equal(await pending, null);
  assert.equal(await tabA.getToken(), null);
});


test('cross-tab: a successful refresh is fenced again before it is served (round 4)', async () => {
  // Astra's round-3 race: A's refresh passes both token-request
  // fences and adopts A's new credentials; B replaces the
  // shared session in the microtask gap BEFORE A's awaiting
  // getToken resumes. Returning the captured refresh result
  // would serve the superseded account — the supersession
  // check must run after every awaited refresh, success
  // included, and what is served is the current session's
  // token or nothing.
  const { make, persistent } = persistentFixture({
    responder: async (url, options) => {
      const body = new URLSearchParams(options.body);
      if (body.get('refresh_token') === 'refresh-a1') {
        return {
          ok: true, status: 200,
          json: async () => {
            // Land B's replacement two microtask hops out:
            // after this response's second fence check and
            // adoption, before the awaiting caller resumes.
            Promise.resolve().then().then(() => {
              persistent.set('si-app-session-v1', JSON.stringify({
                refreshToken: 'refresh-b', savedAt: 2,
              }));
              persistent.set('si-app-session-epoch-v1', JSON.stringify({
                rev: 'b-revision', owner: 'another-instance',
                kind: 'persist', at: 2,
              }));
            });
            return {
              access_token: 'access-a2', refresh_token: 'refresh-a2',
              expires_in: 3600,
            };
          },
        };
      }
      return tokenResponse({});
    },
  });
  persistent.set('si-app-session-v1',
    JSON.stringify({ refreshToken: 'refresh-a1', savedAt: 1 }));
  const tabA = make();
  assert.equal(await tabA.getToken(), null);
  assert.equal(await tabA.getToken(), null);
});


test('cross-tab: dispatch is fenced — a replacement before dispatch prevents the request (round 5)', async () => {
  // Astra's round-4 consumer gap: the API client awaited
  // getToken() and dispatched with the captured string, so a
  // session replacement landing after getToken returned but
  // before dispatch was answered with the superseded account's
  // token. withCurrentToken puts the final fence, the token
  // selection and the dispatch invocation in one synchronous
  // block. Both wirings are exercised against the same
  // interleaving (the other tab's sign-out is queued before
  // the call, landing between selection and dispatch).
  async function setup(wiring) {
    const fixture = persistentFixture({
      responder: (url, options) => {
        const body = new URLSearchParams(options.body);
        if (body.get('refresh_token') === 'refresh-a1') {
          return tokenResponse({
            access_token: 'access-a', refresh_token: 'refresh-a2',
            expires_in: 3600,
          });
        }
        return tokenResponse({});
      },
    });
    fixture.persistent.set('si-app-session-v1',
      JSON.stringify({ refreshToken: 'refresh-a1', savedAt: 1 }));
    const tabA = fixture.make();
    assert.equal(await tabA.getToken(), 'access-a');
    const apiCalls = [];
    const apiFetch = async (url, options) => {
      apiCalls.push([url, options]);
      return { status: 200, json: async () => ({ ok: true }) };
    };
    const client = createAppClient(wiring === 'fenced'
      ? {
        fetch: apiFetch, getToken: () => tabA.getToken(),
        withToken: (d) => tabA.withCurrentToken(d),
      }
      : { fetch: apiFetch, getToken: () => tabA.getToken() });
    return { client, tabB: fixture.make(), apiCalls };
  }
  {
    const { client, tabB, apiCalls } = await setup('fenced');
    queueMicrotask(() => tabB.clearSession());
    const result = await client.session();
    assert.equal(result.kind, 'login');
    assert.deepEqual(apiCalls, []);
  }
  {
    // The pre-round-5 wiring, kept as the documented gap: the
    // same interleaving dispatches with the old token.
    const { client, tabB, apiCalls } = await setup('naive');
    queueMicrotask(() => tabB.clearSession());
    const result = await client.session();
    assert.equal(result.kind, 'ok');
    assert.equal(apiCalls.length, 1);
    assert.equal(apiCalls[0][1].headers.authorization, 'Bearer ' + 'access-a');
  }
});
