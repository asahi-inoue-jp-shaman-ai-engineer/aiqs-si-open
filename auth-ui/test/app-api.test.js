import test from 'node:test';
import assert from 'node:assert/strict';
import { createAppClient } from '../src/app-api.js';

function fixture({ token = 'synthetic-token', responder, readingTimeoutMs } = {}) {
  const calls = [];
  const fetch = async (url, options) => {
    calls.push([url, options]);
    return responder(url, options);
  };
  const client = createAppClient({
    fetch, getToken: async () => token,
    ...(readingTimeoutMs !== undefined ? { readingTimeoutMs } : {}),
  });
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

test('irradiate carries the completion choice only when one is passed (foundation item 2)', async () => {
  const { calls, client } = fixture({ responder: () => jsonResponse(200, {}) });
  await client.irradiate('c1', 'k2', 'notify');
  await client.irradiate('c1', 'k2', 'wait');
  await client.irradiate('c1', 'k2');
  assert.deepEqual(JSON.parse(calls[0][1].body), {
    confirmation_id: 'c1', request_key: 'k2', completion: { mode: 'notify' },
  });
  assert.deepEqual(JSON.parse(calls[1][1].body), {
    confirmation_id: 'c1', request_key: 'k2', completion: { mode: 'wait' },
  });
  // No choice passed: the body stays exactly the historical
  // shape (the server treats an absent choice as "wait").
  assert.deepEqual(JSON.parse(calls[2][1].body),
    { confirmation_id: 'c1', request_key: 'k2' });
});

test('resume posts the token to the resume route under the caller session', async () => {
  const { calls, client } = fixture({
    responder: () => jsonResponse(200, { job_id: 'j-9', operation: 'scan' }),
  });
  const result = await client.resume('resume-token-value');
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], '/api/app/resume');
  assert.equal(calls[0][1].method, 'POST');
  assert.equal(calls[0][1].headers.authorization, 'Bearer ' + 'synthetic-token');
  assert.deepEqual(JSON.parse(calls[0][1].body), { token: 'resume-token-value' });
  assert.deepEqual(result, {
    kind: 'ok', status: 200, data: { job_id: 'j-9', operation: 'scan' },
  });
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

test('target list failures are errors, never ok data', async () => {
  // A 503/429/500 with a JSON body is an admission or maintenance
  // failure, not an (empty) target list. Before this, targets()
  // returned kind 'ok' for these and the app rendered the failure
  // as "no targets registered".
  for (const status of [429, 500, 503]) {
    const { client } = fixture({
      responder: () => jsonResponse(status, { error: 'temporarily_unavailable' }),
    });
    const result = await client.targets();
    assert.equal(result.kind, 'error', `status ${status}`);
    assert.equal(result.status, status);
  }
});

test('a successful target list still returns its data', async () => {
  const { client } = fixture({
    responder: () => jsonResponse(200, { targets: [{ id: 't1', name: 'n' }] }),
  });
  const result = await client.targets();
  assert.equal(result.kind, 'ok');
  assert.deepEqual(result.data.targets, [{ id: 't1', name: 'n' }]);
});

test('registering a target keeps the 400 error-code contract', async () => {
  // requireSuccess is scoped to the list fetch only: the register
  // POST still surfaces its fixed error code for template mapping.
  const { client } = fixture({
    responder: () => jsonResponse(400, { error: 'target_rejected' }),
  });
  const result = await client.registerTarget({ request_key: 'k', name: 'n' });
  assert.equal(result.kind, 'ok');
  assert.equal(result.data.error, 'target_rejected');
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

test('a stalled reading POST resolves as a timeout at its deadline (round 7)', async () => {
  // The fetch never answers: without a bound the submission
  // (and the in-flight guard around it) would hang forever.
  const { calls, client } = fixture({
    readingTimeoutMs: 25,
    responder: () => new Promise(() => {}),
  });
  const result = await client.createReading('conf-1', 'rk-1');
  assert.deepEqual(result, { kind: 'error', timeout: true });
  assert.equal(calls.length, 1);
  // The bounded call carries an abort signal; unbounded calls
  // carry none.
  assert.ok(calls[0][1].signal instanceof AbortSignal);
  const plain = fixture({ responder: () => jsonResponse(200, {}) });
  await plain.client.session();
  assert.equal(plain.calls[0][1].signal, undefined);
});

test('the reading deadline covers the response body read (round 7)', async () => {
  // Headers arrive at once but the body never finishes: the
  // deadline bounds the WHOLE request, body included.
  const { client } = fixture({
    readingTimeoutMs: 25,
    responder: () => ({ status: 200, json: () => new Promise(() => {}) }),
  });
  const result = await client.createReading('conf-1', 'rk-1');
  assert.deepEqual(result, { kind: 'error', timeout: true });
});

test('a reading response landing after the deadline is fenced out (round 7)', async () => {
  let capturedSignal;
  const { client } = fixture({
    readingTimeoutMs: 20,
    responder: async (url, options) => {
      capturedSignal = options.signal;
      await new Promise((resolve) => setTimeout(resolve, 60));
      return jsonResponse(200, { reading_id: 'reading-1', state: 'completed' });
    },
  });
  const result = await client.createReading('conf-1', 'rk-1');
  // The deadline won the attempt: the caller sees the timeout,
  // never the late success.
  assert.deepEqual(result, { kind: 'error', timeout: true });
  // The request was aborted at the deadline, and the late
  // answer landing afterwards changes nothing observable.
  assert.equal(capturedSignal.aborted, true);
  await new Promise((resolve) => setTimeout(resolve, 80));
  assert.deepEqual(result, { kind: 'error', timeout: true });
});

test('a reading response inside the deadline returns normally (round 7)', async () => {
  const { client } = fixture({
    readingTimeoutMs: 1000,
    responder: async () => {
      await new Promise((resolve) => setTimeout(resolve, 10));
      return jsonResponse(200, { reading_id: 'reading-1', state: 'reserved' });
    },
  });
  const result = await client.createReading('conf-1', 'rk-1');
  assert.equal(result.kind, 'ok');
  assert.equal(result.status, 200);
  assert.equal(result.data.reading_id, 'reading-1');
});
