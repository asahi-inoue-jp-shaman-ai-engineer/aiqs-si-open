import test from 'node:test';
import assert from 'node:assert/strict';
import { createAppClient } from '../src/app-api.js';

function fixture({ token = 'synthetic-token', responder } = {}) {
  const calls = [];
  const fetch = async (url, options) => {
    calls.push([url, options]);
    return responder(url, options);
  };
  const client = createAppClient({ fetch, getToken: async () => token });
  return { calls, client };
}

const jsonResponse = (status, data) => ({ status, json: async () => data });

test('without a token the client never calls the API', async () => {
  const { calls, client } = fixture({ token: null, responder: () => jsonResponse(200, {}) });
  assert.deepEqual(await client.session(), { kind: 'login' });
  assert.deepEqual(await client.history(), { kind: 'login' });
  assert.equal(calls.length, 0);
});

test('a 401 answer means the session is gone', async () => {
  const { client } = fixture({ responder: () => jsonResponse(401, { error: 'unauthorized' }) });
  assert.deepEqual(await client.session(), { kind: 'login' });
});

test('fixed error codes on 400 stay visible for template mapping', async () => {
  const { client } = fixture({
    responder: () => jsonResponse(400, { error: 'scan_expired' }),
  });
  const result = await client.prepareIrradiation('scan-1', 33);
  assert.equal(result.kind, 'ok');
  assert.equal(result.status, 400);
  assert.equal(result.data.error, 'scan_expired');
});

test('app calls hit the exact routes with the bearer token', async () => {
  const { calls, client } = fixture({ responder: () => jsonResponse(200, {}) });
  await client.session();
  await client.targets();
  await client.registerTarget({ request_key: 'k', name: 'n' });
  await client.renameTarget('t1', 'new');
  await client.deleteTarget('t1');
  await client.scan({ request_key: 'k' });
  await client.prepareIrradiation('s1', 33);
  await client.irradiate('c1', 'k2');
  await client.job('j 1');
  await client.cancelJob('j1');
  await client.history();
  assert.deepEqual(calls.map(([url]) => url), [
    '/api/app/session',
    '/api/app/targets',
    '/api/app/targets',
    '/api/app/targets/rename',
    '/api/app/targets/delete',
    '/api/app/jobs/scan',
    '/api/app/jobs/irradiate/prepare',
    '/api/app/jobs/irradiate',
    '/api/app/jobs/j%201',
    '/api/app/jobs/j1/cancel',
    '/api/app/history',
  ]);
  for (const [, options] of calls) {
    assert.equal(options.headers.authorization, 'Bearer synthetic-token');
  }
  assert.deepEqual(JSON.parse(calls[7][1].body),
    { confirmation_id: 'c1', request_key: 'k2' });
});

test('reading calls hit the reading routes with the decided bodies', async () => {
  const { calls, client } = fixture({ responder: () => jsonResponse(200, {}) });
  await client.prepareReading('scan 1');
  await client.createReading('conf-1', 'rk-1');
  await client.reading('read 1');
  assert.deepEqual(calls.map(([url]) => url), [
    '/api/app/jobs/scan%201/reading/prepare',
    '/api/app/readings',
    '/api/app/readings/read%201',
  ]);
  assert.deepEqual(JSON.parse(calls[1][1].body),
    { confirmation_id: 'conf-1', request_key: 'rk-1' });
  for (const [, options] of calls) {
    assert.equal(options.headers.authorization, 'Bearer ' + 'synthetic-token');
  }
});

test('referral calls hit the referral routes with the decided bodies', async () => {
  const { calls, client } = fixture({ responder: () => jsonResponse(200, {}) });
  await client.referral();
  await client.redeemReferral('ABC234');
  await client.markReferralGiftsSeen(['gift-1', 'gift-2']);
  assert.deepEqual(calls.map(([url]) => url), [
    '/api/app/referral',
    '/api/app/referral/redeem',
    '/api/app/referral/gifts/seen',
  ]);
  assert.deepEqual(JSON.parse(calls[1][1].body), { code: 'ABC234' });
  assert.deepEqual(JSON.parse(calls[2][1].body), { ids: ['gift-1', 'gift-2'] });
  for (const [, options] of calls) {
    assert.equal(options.headers.authorization, 'Bearer ' + 'synthetic-token');
  }
});

test('referral error codes stay visible for template mapping', async () => {
  const { client } = fixture({
    responder: () => jsonResponse(409, { error: 'already_redeemed' }),
  });
  const result = await client.redeemReferral('ABC234');
  assert.equal(result.kind, 'ok');
  assert.equal(result.status, 409);
  assert.equal(result.data.error, 'already_redeemed');
});

test('reading failure codes stay visible for template mapping', async () => {
  const { client } = fixture({
    responder: () => jsonResponse(400, { error: 'reading_unavailable' }),
  });
  const result = await client.createReading('conf-1', 'rk-1');
  assert.equal(result.kind, 'ok');
  assert.equal(result.status, 400);
  assert.equal(result.data.error, 'reading_unavailable');
});
