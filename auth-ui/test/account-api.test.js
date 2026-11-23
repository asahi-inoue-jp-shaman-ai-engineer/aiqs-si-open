import test from 'node:test';
import assert from 'node:assert/strict';
import { createAccountClient } from '../src/account-api.js';
import {
  armFragmentHandoff, carryTokenToAccountLinks, createTokenProvider,
  ensureFragmentHandoff, hrefWithFragmentToken, installCarriedLinkRefresh,
  takeFragmentToken,
} from '../src/account-session.js';

function fakeStorage() {
  const items = new Map();
  return {
    getItem: (k) => items.get(k) ?? null,
    setItem: (k, v) => items.set(k, v),
    removeItem: (k) => items.delete(k),
  };
}

function fixture({ token = 'synthetic-token', responder } = {}) {
  const calls = [];
  const fetch = async (url, options) => {
    calls.push([url, options]);
    return responder(url, options);
  };
  const client = createAccountClient({ fetch, getToken: async () => token });
  return { calls, client };
}

const jsonResponse = (status, data) => ({ status, json: async () => data });

test('without a token the client never calls the API', async () => {
  const { calls, client } = fixture({ token: null, responder: () => jsonResponse(200, {}) });
  assert.deepEqual(await client.linkStatus(), { kind: 'login' });
  assert.deepEqual(await client.purchase(), { kind: 'login' });
  assert.equal(calls.length, 0);
});

test('a 401 answer means the session is gone, not an operation error', async () => {
  const { client } = fixture({ responder: () => jsonResponse(401, { error: 'unauthorized' }) });
  assert.deepEqual(await client.linkStatus(), { kind: 'login' });
});

test('link calls carry the bearer token and the exact bodies', async () => {
  const { calls, client } = fixture({
    responder: () => jsonResponse(200, { configured: true, linked: false }),
  });
  const status = await client.linkStatus();
  assert.equal(status.kind, 'ok');
  assert.equal(status.data.configured, true);
  await client.verifyLink('1234-5678');
  await client.confirmLink('reservation-1');
  await client.unlink();
  await client.purchase();
  assert.deepEqual(calls.map(([url]) => url), [
    '/api/account/link',
    '/api/account/link/verify',
    '/api/account/link/confirm',
    '/api/account/link/unlink',
    '/api/account/purchase',
  ]);
  for (const [, options] of calls) {
    assert.equal(options.headers.authorization, 'Bearer synthetic-token');
  }
  assert.equal(calls[0][1].method, 'GET');
  assert.equal(calls[0][1].body, undefined);
  assert.deepEqual(JSON.parse(calls[1][1].body), { code: '1234-5678' });
  assert.deepEqual(JSON.parse(calls[2][1].body), { reservation_id: 'reservation-1' });
  assert.deepEqual(JSON.parse(calls[3][1].body), {});
});

test('referral calls reuse the app referral endpoints with the account token', async () => {
  const { calls, client } = fixture({
    responder: () => jsonResponse(200, { code: 'ABC234' }),
  });
  const status = await client.referral();
  assert.equal(status.kind, 'ok');
  assert.equal(status.data.code, 'ABC234');
  await client.redeemReferral('XYZ789');
  await client.markReferralGiftsSeen(['gift-1']);
  assert.deepEqual(calls.map(([url]) => url), [
    '/api/app/referral',
    '/api/app/referral/redeem',
    '/api/app/referral/gifts/seen',
  ]);
  assert.deepEqual(JSON.parse(calls[1][1].body), { code: 'XYZ789' });
  assert.deepEqual(JSON.parse(calls[2][1].body), { ids: ['gift-1'] });
  for (const [, options] of calls) {
    assert.equal(options.headers.authorization, 'Bearer ' + 'synthetic-token');
  }
});

test('a 404 referral answer stays visible so the page can hide its section', async () => {
  const { client } = fixture({
    responder: () => jsonResponse(404, { error: 'not_found' }),
  });
  const result = await client.referral();
  assert.equal(result.kind, 'ok');
  assert.equal(result.status, 404);
});

test('busy/rate-limited JSON answers are errors for account calls, never data', async () => {
  // The admission middleware answers 503/429 with a JSON body; the
  // account endpoints themselves always answer results with 200.
  // Treating the busy body as data made linkStatus render the
  // feature as "準備中" and purchase as unconfigured.
  for (const status of [429, 500, 503]) {
    const { client } = fixture({
      responder: () => jsonResponse(status, { error: status === 429 ? 'rate_limited' : 'server_busy' }),
    });
    assert.deepEqual(await client.linkStatus(), { kind: 'error', status });
    assert.deepEqual(await client.purchase(), { kind: 'error', status });
    assert.deepEqual(await client.verifyLink('1234'), { kind: 'error', status });
    assert.deepEqual(await client.unlink(), { kind: 'error', status });
  }
});

test('referral calls keep their status-coded contract (404 and error codes are data)', async () => {
  const off = fixture({ responder: () => jsonResponse(404, { error: 'not_found' }) });
  const offResult = await off.client.referral();
  assert.equal(offResult.kind, 'ok');
  assert.equal(offResult.status, 404);
  const conflict = fixture({
    responder: () => jsonResponse(409, { error: 'already_redeemed' }),
  });
  const redeem = await conflict.client.redeemReferral('ABC234');
  assert.equal(redeem.kind, 'ok');
  assert.equal(redeem.data.error, 'already_redeemed');
});

test('account-page links carry the token only with an armed state, and nothing else changes', () => {
  // Without a state the href is left untouched: a bare token never
  // enters a URL (Astra #1, new contract).
  assert.equal(hrefWithFragmentToken('/account/link', 'tok en'), '/account/link');
  assert.equal(hrefWithFragmentToken('/account/link', 'tok en', 'state-1'),
    '/account/link#access_token=tok%20en&state=state-1');
  assert.equal(hrefWithFragmentToken('/account/purchase?x=1', 'tok', 's'),
    '/account/purchase?x=1#access_token=tok&state=s');
  assert.equal(hrefWithFragmentToken('/account/link#old', 'tok', 's'),
    '/account/link#access_token=tok&state=s');
  // Non-account destinations never receive the token in a URL.
  assert.equal(hrefWithFragmentToken('/', 'tok', 's'), '/');
  assert.equal(hrefWithFragmentToken('/oauth/consent', 'tok', 's'), '/oauth/consent');
  assert.equal(hrefWithFragmentToken('https://example.com/account', 'tok', 's'),
    'https://example.com/account');
  assert.equal(hrefWithFragmentToken('/account/link', null, 's'), '/account/link');

  const anchors = [
    { href: '/account/link', getAttribute() { return this.href; }, setAttribute(name, value) { this.href = value; } },
    { href: '/', getAttribute() { return this.href; }, setAttribute(name, value) { this.href = value; } },
  ];
  const document = { querySelectorAll: () => anchors };
  const storage = fakeStorage();
  assert.equal(carryTokenToAccountLinks(document, 'tok', storage), 1);
  const state = ensureFragmentHandoff(storage);
  assert.equal(anchors[0].href, `/account/link#access_token=tok&state=${state}`);
  assert.equal(anchors[1].href, '/');
  assert.equal(carryTokenToAccountLinks(document, null, storage), 0);
  // Without storage no nonce can be armed, so nothing is rewritten.
  const bareAnchors = [
    { href: '/account/link', getAttribute() { return this.href; }, setAttribute(name, value) { this.href = value; } },
  ];
  const bareDoc = { querySelectorAll: () => bareAnchors };
  const noStorage = { getItem: () => null, setItem: () => { throw new Error('blocked'); }, removeItem: () => {} };
  assert.equal(carryTokenToAccountLinks(bareDoc, 'tok', noStorage), 0);
  assert.equal(bareAnchors[0].href, '/account/link');
});

test('transport and non-JSON failures are errors, never data', async () => {
  const down = fixture({ responder: () => { throw new Error('offline'); } });
  assert.deepEqual(await down.client.linkStatus(), { kind: 'error' });
  const garbled = fixture({ responder: () => ({ status: 200, json: async () => { throw new Error('bad'); } }) });
  assert.deepEqual(await garbled.client.purchase(), { kind: 'error' });
});

test('a state-bound fragment token is taken once and stripped from the URL', () => {
  const replaced = [];
  const storage = fakeStorage();
  const state = armFragmentHandoff(storage);
  assert.ok(state);
  const location = {
    hash: `#access_token=synthetic-token&state=${state}`,
    pathname: '/account/link', search: '',
  };
  const history = { replaceState: (...args) => replaced.push(args) };
  assert.equal(takeFragmentToken(location, history, storage), 'synthetic-token');
  assert.deepEqual(replaced, [[null, '', '/account/link']]);
  // Single-use: replaying the same link adopts nothing.
  assert.equal(takeFragmentToken(location, history, storage), null);
});

test('an unsolicited fragment token is never adopted', () => {
  const replaced = [];
  const history = { replaceState: (...args) => replaced.push(args) };
  const location = {
    hash: '#access_token=attacker-token&state=unbound-state',
    pathname: '/account/purchase', search: '',
  };
  assert.equal(takeFragmentToken(location, history, fakeStorage()), null);
  // The token is still stripped from the URL.
  assert.deepEqual(replaced, [[null, '', '/account/purchase']]);
  // A wrong state is equally rejected, and the pending nonce
  // survives so the legitimate handoff can still complete.
  const storage = fakeStorage();
  const state = armFragmentHandoff(storage);
  assert.equal(takeFragmentToken(
    { hash: '#access_token=x&state=wrong', pathname: '/', search: '' },
    history, storage), null);
  assert.equal(takeFragmentToken(
    { hash: `#access_token=x&state=${state}`, pathname: '/', search: '' },
    history, storage), 'x');
});

test('absent, duplicated or oversized fragment tokens are ignored', () => {
  const history = { replaceState: () => { throw new Error('must not strip'); } };
  assert.equal(takeFragmentToken({ hash: '', pathname: '/', search: '' }, history), null);
  assert.equal(takeFragmentToken({ hash: '#other=1', pathname: '/', search: '' }, history), null);
  const stripping = { replaceState: () => {} };
  assert.equal(takeFragmentToken({ hash: '#access_token=a&access_token=b', pathname: '/', search: '' }, stripping, fakeStorage()), null);
  assert.equal(takeFragmentToken({ hash: '#access_token=' + 'x'.repeat(8193), pathname: '/', search: '' }, stripping, fakeStorage()), null);
});

test('the token provider captures a state-bound fragment token once', async () => {
  const storage = fakeStorage();
  const state = armFragmentHandoff(storage);
  const location = {
    hash: `#access_token=once&state=${state}`,
    pathname: '/account/purchase', search: '',
  };
  const history = { replaceState: () => {} };
  const provider = createTokenProvider(location, history, storage);
  assert.equal(await provider(), 'once');
  assert.equal(await provider(), 'once');
  const empty = createTokenProvider({ hash: '', pathname: '/', search: '' }, history, fakeStorage());
  assert.equal(await empty(), null);
  // Without storage a fragment token can never be adopted.
  const bare = createTokenProvider(
    { hash: '#access_token=x&state=y', pathname: '/', search: '' }, history,
    { getItem: () => null, setItem: () => { throw new Error('blocked'); }, removeItem: () => {} });
  assert.equal(await bare(), null);
});

test('a nonce that cannot be consumed is never adopted', () => {
  const storage = fakeStorage();
  const state = armFragmentHandoff(storage);
  storage.removeItem = () => { throw new Error('storage blocked'); };
  const hash = '#' + new URLSearchParams({ access_token: 'tok', state });
  const location = { hash, pathname: '/', search: '' };
  const history = { replaceState: () => {} };
  assert.equal(takeFragmentToken(location, history, storage), null);
});

test('without crypto a fragment token is never adopted', () => {
  const storage = fakeStorage();
  const state = armFragmentHandoff(storage);
  const hash = '#' + new URLSearchParams({ access_token: 'tok', state });
  const location = { hash, pathname: '/', search: '' };
  const history = { replaceState: () => {} };
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'crypto');
  Object.defineProperty(globalThis, 'crypto', { value: undefined, configurable: true });
  try {
    assert.equal(takeFragmentToken(location, history, storage), null);
  } finally {
    Object.defineProperty(globalThis, 'crypto', descriptor);
  }
  // With crypto restored the same handoff still completes (the
  // failed attempt did not consume the nonce).
  assert.equal(takeFragmentToken(location, history, storage), 'tok');
});

test('carried links are refreshed on document restoration and just before following (R4)', async () => {
  const storage = fakeStorage();
  const anchor = {
    href: '/account/link',
    getAttribute() { return this.href; },
    setAttribute(name, value) { this.href = value; },
    closest(selector) { return selector.startsWith('a[') ? this : null; },
  };
  const listeners = {};
  const document = {
    querySelectorAll: () => [anchor],
    addEventListener: (type, fn, capture) => { listeners['doc:' + type] = { fn, capture }; },
  };
  const windowListeners = {};
  const target = {
    addEventListener: (type, fn) => { windowListeners[type] = fn; },
  };
  const refresh = installCarriedLinkRefresh({
    target, document, tokenProvider: async () => 'tok', storage,
  });
  assert.ok(refresh);
  // Initial carry: the anchor carries the first pending nonce.
  await refresh();
  const firstState = ensureFragmentHandoff(storage);
  assert.equal(anchor.href, `/account/link#access_token=tok&state=${firstState}`);
  // The destination adopts the handoff, consuming the nonce.
  const destinationHistory = { replaceState: () => {} };
  assert.equal(takeFragmentToken(
    { hash: anchor.href.slice(anchor.href.indexOf('#')), pathname: '/account/link', search: '' },
    destinationHistory, storage), 'tok');
  // A non-persisted pageshow does not rewrite anything.
  anchor.href = '/account/link';
  windowListeners.pageshow({ persisted: false });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(anchor.href, '/account/link');
  // Restoration (persisted pageshow): the carry re-runs and the
  // anchor now carries a FRESH nonce — the consumed one is never
  // replayed, and the refreshed link adopts successfully.
  windowListeners.pageshow({ persisted: true });
  await new Promise((resolve) => setTimeout(resolve, 0));
  const restoredState = ensureFragmentHandoff(storage);
  assert.notEqual(restoredState, firstState);
  assert.equal(anchor.href, `/account/link#access_token=tok&state=${restoredState}`);
  assert.equal(takeFragmentToken(
    { hash: anchor.href.slice(anchor.href.indexOf('#')), pathname: '/account/link', search: '' },
    destinationHistory, storage), 'tok');
  // Just before following: a click on the anchor rewrites it
  // with the then-current pending nonce, even if its href was
  // left stale (e.g. restored without a pageshow refresh).
  const clickState = ensureFragmentHandoff(storage);
  anchor.href = '/account/link#access_token=tok&state=stale-nonce';
  listeners['doc:click'].fn({ target: anchor });
  assert.equal(anchor.href, `/account/link#access_token=tok&state=${clickState}`);
  // The click listener is capture-phase, so the rewrite lands
  // before the browser follows the link.
  assert.equal(listeners['doc:click'].capture, true);
});
