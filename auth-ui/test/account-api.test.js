import test from 'node:test';
import assert from 'node:assert/strict';
import { createAccountClient } from '../src/account-api.js';
import { createTokenProvider, takeFragmentToken } from '../src/account-session.js';

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

test('transport and non-JSON failures are errors, never data', async () => {
  const down = fixture({ responder: () => { throw new Error('offline'); } });
  assert.deepEqual(await down.client.linkStatus(), { kind: 'error' });
  const garbled = fixture({ responder: () => ({ status: 200, json: async () => { throw new Error('bad'); } }) });
  assert.deepEqual(await garbled.client.purchase(), { kind: 'error' });
});

test('the fragment token is taken once and stripped from the URL', () => {
  const replaced = [];
  const location = { hash: '#access_token=synthetic-token&token_type=Bearer', pathname: '/account/link', search: '' };
  const history = { replaceState: (...args) => replaced.push(args) };
  assert.equal(takeFragmentToken(location, history), 'synthetic-token');
  assert.deepEqual(replaced, [[null, '', '/account/link']]);
});

test('absent, duplicated or oversized fragment tokens are ignored', () => {
  const history = { replaceState: () => { throw new Error('must not strip'); } };
  assert.equal(takeFragmentToken({ hash: '', pathname: '/', search: '' }, history), null);
  assert.equal(takeFragmentToken({ hash: '#other=1', pathname: '/', search: '' }, history), null);
  assert.equal(takeFragmentToken({ hash: '#access_token=a&access_token=b', pathname: '/', search: '' }, history), null);
  assert.equal(takeFragmentToken({ hash: '#access_token=' + 'x'.repeat(8193), pathname: '/', search: '' }, history), null);
});

test('the token provider captures the fragment token once', async () => {
  const location = { hash: '#access_token=once', pathname: '/account/purchase', search: '' };
  const history = { replaceState: () => {} };
  const provider = createTokenProvider(location, history);
  assert.equal(await provider(), 'once');
  assert.equal(await provider(), 'once');
  const empty = createTokenProvider({ hash: '', pathname: '/', search: '' }, history);
  assert.equal(await empty(), null);
});
