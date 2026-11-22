import test from 'node:test';
import assert from 'node:assert/strict';
import { createAppAuth } from '../src/app-auth.js';

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
