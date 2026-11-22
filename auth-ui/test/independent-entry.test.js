import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createIndependentEntry, createMemoryClient } from '../src/independent-entry.js';

const subject = '11111111-1111-4111-8111-111111111111';
const other = '22222222-2222-4222-8222-222222222222';
const result = (id = subject) => ({ data: { user: { id }, session: { user: { id }, access_token: 'synthetic-token' } } });
function fixture() {
  const calls = [], transfers = [];
  const auth = {
    signInWithPassword: async (args) => { calls.push(['login', args]); return result(); },
    signUp: async (args) => { calls.push(['signup', args]); return { data: { user: { id: subject }, session: null } }; },
    verifyOtp: async (args) => { calls.push(['verify', args]); return result(); },
    getUser: async () => ({ data: { user: { id: subject } } }),
  };
  const transport = { send: async (...args) => {
    calls.push(['complete', ...args]);
    return { ok: true, json: async () => ({ registration_complete: true }) };
  } };
  const entry = createIndependentEntry({ auth, origin: 'https://si.test',
    fetch: (...args) => transport.send(...args), handoff: async (token) => transfers.push(token) });
  return { entry, auth, calls, transfers, transport };
}
async function verified(f) {
  await f.entry.signup({ email: 'new@example.test', password: 'synthetic' });
  await f.entry.verify('123456');
}
function deferred() { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; }

test('SDK forces memory-only options and rejects untrusted-shaped configuration', () => {
  let args;
  const client = createMemoryClient((...values) => { args = values; return 'client'; },
    { providerBase: 'https://provider.test', publishableKey: 'sb_publishable_synthetic' });
  assert.equal(client, 'client');
  assert.deepEqual(args, ['https://provider.test', 'sb_publishable_synthetic', { auth: {
    persistSession: false, autoRefreshToken: false, detectSessionInUrl: false,
  } }]);
  for (const providerBase of ['http://provider.test', 'https://user@provider.test', 'https://provider.test/path', 'https://provider.test/?x=1', 'https://provider.test/#token']) {
    assert.throws(() => createMemoryClient(() => {}, { providerBase, publishableKey: 'sb_publishable_synthetic' }));
    assert.throws(() => createIndependentEntry({ auth: {}, origin: providerBase, fetch() {}, handoff() {} }));
  }
  assert.throws(() => createMemoryClient(() => {}, { providerBase: 'https://provider.test', publishableKey: 'service_role' }));
});

test('existing login hands off once without onboarding', async () => {
  const f = fixture();
  assert.deepEqual(await f.entry.login({ email: 'existing@example.test', password: 'synthetic' }), { kind: 'handed-off' });
  assert.deepEqual(f.transfers, ['synthetic-token']);
  assert.deepEqual(f.calls.map(c => c[0]), ['login']);
  await assert.rejects(f.entry.login({})); await assert.rejects(f.entry.complete());
});

test('registration freezes original email/referral and exact completion contract', async () => {
  const f = fixture();
  const input = { email: 'new@example.test', password: 'synthetic', referralCode: 'original-code' };
  await f.entry.signup(input);
  input.email = 'switched@example.test'; input.referralCode = 'changed-code';
  assert.equal(f.transfers.length, 0);
  await f.entry.verify('123456'); await f.entry.complete();
  assert.deepEqual(f.calls[0][1].options.data, { si_registration: 'v1', referral_code: 'original-code' });
  assert.deepEqual(f.calls[1], ['verify', { email: 'new@example.test', token: '123456', type: 'signup' }]);
  assert.deepEqual(f.calls[2], ['complete', 'https://si.test/onboarding/complete', {
    method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer synthetic-token' },
    body: JSON.stringify({ subject }), redirect: 'error', credentials: 'omit', cache: 'no-store',
  }]);
  assert.equal(f.transfers.length, 1);
});

test('absent referral stays absent and signup response is never admission', async () => {
  const f = fixture();
  f.auth.signUp = async args => { f.calls.push(['signup', args]); return result(); };
  await f.entry.signup({ email: 'old@example.test', password: 'synthetic' });
  assert.equal(f.calls[0][1].options.data.referral_code, null);
  assert.equal(f.entry.state, 'verification'); assert.equal(f.transfers.length, 0);
  await assert.rejects(f.entry.complete());
  f.auth.verifyOtp = async () => result(other);
  await assert.rejects(f.entry.verify('123456'));
  assert.equal(f.transfers.length, 0);
});

test('ambiguous completion retries frozen identity/token without another signup or OTP', async () => {
  const f = fixture(); await verified(f);
  const send = f.transport.send;
  f.transport.send = async (...args) => { await send(...args); throw new Error('private upstream details'); };
  await assert.rejects(f.entry.complete(), { message: 'Authentication could not be completed' });
  await assert.rejects(f.entry.signup({})); await assert.rejects(f.entry.verify('123456'));
  f.transport.send = send;
  await f.entry.complete();
  assert.deepEqual(f.calls.map(c => c[0]), ['signup', 'verify', 'complete', 'complete']);
  assert.deepEqual(f.calls[2], f.calls[3]); assert.equal(f.transfers.length, 1);
});

test('server completion must return exact true object', async () => {
  for (const body of [{ registration_complete: 'true' }, { registration_complete: false }, {}, { registration_complete: true, extra: 1 }]) {
    const f = fixture(); await verified(f);
    f.transport.send = async () => ({ ok: true, json: async () => body });
    await assert.rejects(f.entry.complete()); assert.equal(f.transfers.length, 0);
  }
});

test('account switch before or during completion cannot hand off', async () => {
  const f = fixture(); await verified(f);
  f.auth.getUser = async () => ({ data: { user: { id: other } } });
  await assert.rejects(f.entry.complete()); assert.equal(f.calls.length, 2);
  f.auth.getUser = async () => ({ data: { user: { id: subject } } });
  f.transport.send = async () => {
    f.auth.getUser = async () => ({ data: { user: { id: other } } });
    return { ok: true, json: async () => ({ registration_complete: true }) };
  };
  await assert.rejects(f.entry.complete()); assert.equal(f.transfers.length, 0);
});

test('single flight excludes competing actions and repeated handoff', async () => {
  const f = fixture(), gate = deferred();
  f.auth.signInWithPassword = () => gate.promise;
  const first = f.entry.login({});
  await assert.rejects(f.entry.login({})); await assert.rejects(f.entry.signup({}));
  gate.resolve(result()); await first; assert.equal(f.transfers.length, 1);
  const g = fixture(); await verified(g); const completion = deferred();
  g.transport.send = () => completion.promise;
  const running = g.entry.complete();
  await assert.rejects(g.entry.complete());
  completion.resolve({ ok: true, json: async () => ({ registration_complete: true }) });
  await running; assert.equal(g.transfers.length, 1);
});

test('disposal while SDK or completion is pending prevents late handoff', async () => {
  const f = fixture(), gate = deferred();
  f.auth.signInWithPassword = () => gate.promise;
  const running = f.entry.login({}); f.entry.dispose(); gate.resolve(result());
  await assert.rejects(running); assert.equal(f.entry.state, 'disposed'); assert.equal(f.transfers.length, 0);
  const g = fixture(); await verified(g); const completion = deferred();
  g.transport.send = () => completion.promise;
  const finalizing = g.entry.complete(); await Promise.resolve(); await Promise.resolve();
  g.entry.dispose(); completion.resolve({ ok: true, json: async () => ({ registration_complete: true }) });
  await assert.rejects(finalizing); assert.equal(g.transfers.length, 0);
});

test('ambiguous handoff failure is terminal and SDK errors stay generic', async () => {
  let handed = 0;
  const f = fixture();
  const entry = createIndependentEntry({ auth: f.auth, origin: 'https://si.test', fetch() {},
    handoff: async () => { handed++; throw new Error('private callback'); } });
  await assert.rejects(entry.login({}), { message: 'Authentication could not be completed' });
  await assert.rejects(entry.login({})); assert.equal(handed, 1);
  f.auth.signInWithPassword = async () => { throw new Error('private SDK error'); };
  await assert.rejects(f.entry.login({}), { message: 'Authentication could not be completed' });
});
