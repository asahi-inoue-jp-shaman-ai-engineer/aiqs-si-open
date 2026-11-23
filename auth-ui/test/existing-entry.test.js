import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createExistingEntry } from '../src/existing-entry.js';

const id = '11111111-1111-4111-8111-111111111111';
const other = '22222222-2222-4222-8222-222222222222';
const factor = (id = 'totp-1', type = 'totp', status = 'verified') => ({ id, factor_type: type, status });
function fixture(factors = []) {
  const calls = [], sent = [];
  let session = { access_token: 'initial-token', user: { id } };
  const user = { id, factors };
  const forbidden = () => { calls.push(['FORBIDDEN']); throw new Error('Forbidden'); };
  const auth = {
    signInWithPassword: async value => { calls.push(['login', value]); return { data: { user: { ...user }, session } }; },
    signInWithOtp: async value => { calls.push(['otp-request', value]); return { data: {} }; },
    resetPasswordForEmail: async value => { calls.push(['recover', value]); return { data: {} }; },
    verifyOtp: async value => { calls.push(['otp', value]); return { data: { user: { ...user }, session } }; },
    getUser: async token => { calls.push(['user', token]); return { data: { user: { ...user } } }; },
    getSession: async () => ({ data: { session } }),
    updateUser: async value => { calls.push(['update', value]); return { data: { user: { ...user } } }; },
    signUp: forbidden, enroll: forbidden, complete: forbidden, grantCredits: forbidden,
    mfa: {
      enroll: forbidden,
      challenge: async value => { calls.push(['challenge', value]); return { data: { id: 'challenge-1', type: 'totp', expires_at: Date.now() / 1000 + 60 } }; },
      verify: async value => {
        calls.push(['mfa', value]); session = { access_token: 'rotated-token', user: { id } };
        return { data: { access_token: 'rotated-token', user: { ...user } } };
      },
    },
  };
  const entry = createExistingEntry({ auth, handoff: async token => { sent.push(token); } });
  return { entry, auth, calls, sent, user, setSession(value) { session = value; } };
}
const login = f => f.entry.passwordLogin({ email: 'existing@example.test', password: 'synthetic' });
async function recovery(f) { await f.entry.requestRecovery('original@example.test'); return f.entry.verifyRecovery('123456'); }
async function challenged(f, recover = false) {
  await (recover ? recovery(f) : login(f));
  await f.entry.chooseAndChallengeTOTP('totp-1');
}
function deferred() { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; }

test('existing login without verified factors hands off once without forbidden operations', async () => {
  const f = fixture([factor('pending', 'phone', 'unverified')]);
  assert.deepEqual(await login(f), { kind: 'handed-off' });
  assert.deepEqual(f.sent, ['initial-token']);
  assert(f.calls.filter(c => c[0] === 'user').every(c => c[1] === 'initial-token'));
  assert(!f.calls.some(c => ['FORBIDDEN', 'update', 'challenge', 'recover'].includes(c[0])));
  await assert.rejects(login(f));
});

test('recovery freezes email and requires explicit password update before handoff', async () => {
  const f = fixture();
  await f.entry.requestRecovery('original@example.test');
  await assert.rejects(f.entry.requestRecovery('switched@example.test'));
  assert.deepEqual(await f.entry.verifyRecovery('123456'), { kind: 'password-ready' });
  assert.deepEqual(f.calls.find(c => c[0] === 'otp'), ['otp', { email: 'original@example.test', token: '123456', type: 'recovery' }]);
  assert.equal(f.sent.length, 0);
  await f.entry.changePassword('new-synthetic');
  assert.deepEqual(f.sent, ['initial-token']);
  assert.equal(f.calls.filter(c => c[0] === 'update').length, 1);
  await assert.rejects(f.entry.changePassword('again'));
  assert(!f.calls.some(c => c[0] === 'FORBIDDEN'));
});

test('all known verified unsupported factors reject explicitly, mixed factors permit explicit TOTP', async () => {
  for (const type of ['phone', 'webauthn', 'recovery_code']) {
    const f = fixture([factor('unsupported', type)]);
    assert.deepEqual(await recovery(f), { kind: 'unsupported-factor' });
    await assert.rejects(f.entry.changePassword('synthetic'));
    assert.equal(f.sent.length, 0);
    assert(!f.calls.some(c => c[0] === 'update'));
  }
  const f = fixture([factor(), factor('phone', 'phone'), factor('second', 'totp')]);
  assert.deepEqual(await login(f), { kind: 'choose-totp', factorIds: ['second', 'totp-1'] });
  assert(!f.calls.some(c => c[0] === 'challenge'));
  await assert.rejects(f.entry.chooseAndChallengeTOTP('phone'));
  await f.entry.chooseAndChallengeTOTP('totp-1');
  await f.entry.verifyTOTP('123456');
  assert.deepEqual(f.sent, ['rotated-token']);
});

test('malformed unknown and duplicate factors fail closed even among valid TOTP', async () => {
  for (const bad of [factor('unknown', 'future'), factor('bad', 'totp', 'unknown'), factor('', 'totp'), factor(), null]) {
    const f = fixture([factor(), bad]);
    await assert.rejects(login(f)); assert.equal(f.sent.length, 0);
    assert(!f.calls.some(c => c[0] === 'challenge'));
  }
  for (const malformed of [null, {}, 'totp']) {
    const f = fixture(malformed); await assert.rejects(login(f)); assert.equal(f.sent.length, 0);
  }
});

test('recovery MFA rotates token but remains password-ready until update succeeds', async () => {
  const f = fixture([factor()]); await challenged(f, true);
  await assert.rejects(f.entry.changePassword('too-early'));
  assert.deepEqual(await f.entry.verifyTOTP('123456'), { kind: 'password-ready' });
  assert.equal(f.sent.length, 0);
  await f.entry.changePassword('new-synthetic');
  assert.deepEqual(f.sent, ['rotated-token']);
  const mfaIndex = f.calls.findIndex(c => c[0] === 'mfa');
  assert(f.calls.slice(mfaIndex + 1).filter(c => c[0] === 'user').every(c => c[1] === 'rotated-token'));
});

test('UUID swaps in login and recovery result or exact bearer getUser cannot hand off', async () => {
  for (const operation of ['login', 'recovery']) {
    for (const boundary of ['result', 'getUser', 'session']) {
      const f = fixture();
      if (boundary === 'result') {
        const name = operation === 'login' ? 'signInWithPassword' : 'verifyOtp';
        f.auth[name] = async () => ({ data: { user: { id: other }, session: { user: { id }, access_token: 'initial-token' } } });
      } else if (boundary === 'getUser') f.auth.getUser = async () => ({ data: { user: { id: other, factors: [] } } });
      else f.setSession({ access_token: 'switched-token', user: { id: other } });
      if (boundary === 'session') {
        const name = operation === 'login' ? 'signInWithPassword' : 'verifyOtp';
        f.auth[name] = async () => ({ data: { user: { id }, session: { user: { id }, access_token: 'initial-token' } } });
      }
      await assert.rejects(operation === 'login' ? login(f) : recovery(f));
      assert.equal(f.sent.length, 0);
    }
  }
});

test('factor substitution before challenge or verify cannot mutate MFA', async () => {
  const f = fixture([factor()]); await login(f);
  f.user.factors = [factor('replacement')];
  await assert.rejects(f.entry.chooseAndChallengeTOTP('totp-1'));
  assert(!f.calls.some(c => c[0] === 'challenge'));
  const g = fixture([factor()]); await challenged(g);
  g.user.factors = [factor('totp-1', 'phone')];
  await assert.rejects(g.entry.verifyTOTP('123456'));
  assert(!g.calls.some(c => c[0] === 'mfa'));
});

test('MFA failure discards challenge and requires explicit same-factor retry', async () => {
  const f = fixture([factor(), factor('second')]); await challenged(f);
  const verify = f.auth.mfa.verify;
  f.auth.mfa.verify = async () => ({ error: new Error('private') });
  await assert.rejects(f.entry.verifyTOTP('123456'), { message: 'Authentication could not be completed' });
  await assert.rejects(f.entry.verifyTOTP('123456'));
  await assert.rejects(f.entry.chooseAndChallengeTOTP('second'));
  f.auth.mfa.verify = verify;
  await f.entry.chooseAndChallengeTOTP('totp-1'); await f.entry.verifyTOTP('123456');
  assert.deepEqual(f.sent, ['rotated-token']);
});

test('invalid OTP or challenge never advances; verify requires a genuinely rotated token', async () => {
  const f = fixture(); await f.entry.requestRecovery('original@example.test');
  await assert.rejects(f.entry.verifyRecovery('bad')); assert(!f.calls.some(c => c[0] === 'otp'));
  for (const data of [{ id: '', type: 'totp', expires_at: 9999999999 },
                     { id: 'challenge', type: 'phone', expires_at: 9999999999 },
                     { id: 'challenge', type: 'totp', expires_at: 1 }]) {
    const g = fixture([factor()]); await login(g); g.auth.mfa.challenge = async () => ({ data });
    await assert.rejects(g.entry.chooseAndChallengeTOTP('totp-1'));
    await assert.rejects(g.entry.verifyTOTP('123456')); assert.equal(g.sent.length, 0);
  }
  const g = fixture([factor()]); await challenged(g);
  g.auth.mfa.verify = async () => ({ data: { access_token: 'initial-token', user: { id } } });
  await assert.rejects(g.entry.verifyTOTP('123456')); assert.equal(g.sent.length, 0);
});

test('UUID swaps during challenge, MFA and password update are rejected', async () => {
  const f = fixture([factor()]); await login(f);
  const challenge = f.auth.mfa.challenge;
  f.auth.mfa.challenge = async args => { const result = await challenge(args); f.user.id = other; return result; };
  await assert.rejects(f.entry.chooseAndChallengeTOTP('totp-1'));
  const g = fixture([factor()]); await challenged(g);
  g.auth.mfa.verify = async () => ({ data: { access_token: 'rotated-token', user: { id: other } } });
  await assert.rejects(g.entry.verifyTOTP('123456')); assert.equal(g.sent.length, 0);
  const h = fixture(); await recovery(h);
  h.auth.updateUser = async () => ({ data: { user: { id: other } } });
  await assert.rejects(h.entry.changePassword('new'));
  assert.equal(h.entry.state, 'password-update-unknown'); assert.equal(h.sent.length, 0);
});

test('ambiguous password update is terminal: no retry and no inference from getUser', async () => {
  for (const response of ['throw', 'error']) {
    const f = fixture(); await recovery(f); let updates = 0;
    f.auth.updateUser = async () => { updates++; if (response === 'throw') throw new Error('private'); return { error: new Error('private') }; };
    await assert.rejects(f.entry.changePassword('new'), { message: 'Authentication could not be completed' });
    assert.equal(f.entry.state, 'password-update-unknown');
    await assert.rejects(f.entry.changePassword('new')); assert.equal(updates, 1); assert.equal(f.sent.length, 0);
  }
});

test('fresh post-mutation and final pre-handoff identity checks reject late UUID swaps', async () => {
  for (const swapAt of [1, 2]) {
    const f = fixture(); let reads = 0;
    f.auth.getUser = async () => ({ data: { user: { id: ++reads === swapAt ? other : id, factors: [] } } });
    await assert.rejects(login(f)); assert.equal(f.sent.length, 0);
  }
  const f = fixture([factor()]); await challenged(f);
  const verify = f.auth.mfa.verify;
  f.auth.mfa.verify = async args => { const result = await verify(args); f.user.id = other; return result; };
  await assert.rejects(f.entry.verifyTOTP('123456')); assert.equal(f.sent.length, 0);
  for (const swapAt of [1, 2]) {
    const g = fixture(); await recovery(g); let reads = 0;
    g.auth.updateUser = async () => {
      g.auth.getUser = async () => ({ data: { user: { id: ++reads === swapAt ? other : id, factors: [] } } });
      return { data: { user: { id } } };
    };
    await assert.rejects(g.entry.changePassword('new'));
    assert.equal(g.entry.state, 'password-update-unknown'); assert.equal(g.sent.length, 0);
  }
});

test('disposal and single flight suppress late SDK outcomes at each mutation', async () => {
  for (const operation of ['login', 'request', 'otp', 'challenge', 'mfa', 'update']) {
    const f = fixture(['challenge', 'mfa'].includes(operation) ? [factor()] : []);
    const gate = deferred(), entered = deferred(); let running;
    const pending = () => { entered.resolve(); return gate.promise; };
    if (operation === 'login') { f.auth.signInWithPassword = pending; running = login(f); }
    if (operation === 'request') { f.auth.resetPasswordForEmail = pending; running = f.entry.requestRecovery('original@example.test'); }
    if (operation === 'otp') { await f.entry.requestRecovery('original@example.test'); f.auth.verifyOtp = pending; running = f.entry.verifyRecovery('123456'); }
    if (operation === 'challenge') { await login(f); f.auth.mfa.challenge = pending; running = f.entry.chooseAndChallengeTOTP('totp-1'); }
    if (operation === 'mfa') { await challenged(f); f.auth.mfa.verify = pending; running = f.entry.verifyTOTP('123456'); }
    if (operation === 'update') { await recovery(f); f.auth.updateUser = pending; running = f.entry.changePassword('new'); }
    await entered.promise;
    await assert.rejects(f.entry.requestRecovery('second@example.test'));
    f.entry.dispose(); gate.resolve({ data: { user: { id }, session: { user: { id }, access_token: 'initial-token' }, access_token: 'rotated-token' } });
    await assert.rejects(running); assert.equal(f.entry.state, 'disposed'); assert.equal(f.sent.length, 0);
  }
});

test('handoff failure is terminal and cannot repeat', async () => {
  const f = fixture(); let count = 0;
  const entry = createExistingEntry({ auth: f.auth, handoff: async () => { count++; throw new Error('private'); } });
  await assert.rejects(entry.passwordLogin({}), { message: 'Authentication could not be completed' });
  await assert.rejects(entry.passwordLogin({})); assert.equal(count, 1);
});

test('disposal microtask after awaited read remains terminal at every helper continuation', async () => {
  for (const [operation, readNumber] of [
    ['recovery', 1], ['login', 1], ['login', 2],
    ['challenge', 1], ['challenge', 2],
    ['mfa', 1], ['mfa', 2], ['mfa', 3],
    ['update', 1], ['update', 2], ['update', 3],
  ]) {
    const f = fixture(['challenge', 'mfa'].includes(operation) ? [factor()] : []);
    if (operation === 'recovery') await f.entry.requestRecovery('original@example.test');
    if (operation === 'challenge') await login(f);
    if (operation === 'mfa') await challenged(f);
    if (operation === 'update') await recovery(f);
    const gate = deferred(), entered = deferred(); let reads = 0;
    const original = f.auth.getUser;
    f.auth.getUser = token => {
      if (++reads === readNumber) { entered.resolve(); return gate.promise; }
      return original(token);
    };
    const running = operation === 'login' ? login(f)
      : operation === 'recovery' ? f.entry.verifyRecovery('123456')
      : operation === 'challenge' ? f.entry.chooseAndChallengeTOTP('totp-1')
      : operation === 'mfa' ? f.entry.verifyTOTP('123456') : f.entry.changePassword('new');
    await entered.promise;
    let callsAtDisposal;
    // Register AFTER the getUser await. Its continuation passes active(), then
    // this microtask disposes before the enclosing helper's caller resumes.
    gate.promise.then(() => { f.entry.dispose(); callsAtDisposal = f.calls.length; });
    gate.resolve({ data: { user: { ...f.user } } });
    await assert.rejects(running, { message: 'Authentication could not be completed' });
    assert.equal(f.entry.state, 'disposed', `${operation}/${readNumber}`);
    assert.equal(f.calls.length, callsAtDisposal, `${operation}/${readNumber}: operation after disposal`);
    assert.equal(f.sent.length, 0);
  }
});

function stubNow(start) {
  let now = start; const original = Date.now;
  Date.now = () => now;
  return { advance(ms) { now += ms; }, restore() { Date.now = original; } };
}
const otpLogin = async f => { await f.entry.requestOtpLogin('existing@example.test'); return f.entry.verifyOtpLogin('123456'); };

test('email OTP login requests without user creation and hands off once', async () => {
  const f = fixture([factor('pending', 'phone', 'unverified')]);
  assert.deepEqual(await f.entry.requestOtpLogin('existing@example.test'), { kind: 'otp-code-required' });
  assert.equal(f.entry.state, 'otp-code');
  assert.deepEqual(f.calls.find(c => c[0] === 'otp-request'),
    ['otp-request', { email: 'existing@example.test', options: { shouldCreateUser: false } }]);
  assert.equal(f.entry.otp.attemptsLeft, 5);
  assert.equal(f.entry.otp.sendsLeft, 4);
  assert(f.entry.otp.resendWaitMs > 0 && f.entry.otp.resendWaitMs <= 60000);
  assert.deepEqual(await f.entry.verifyOtpLogin('123456'), { kind: 'handed-off' });
  assert.deepEqual(f.calls.find(c => c[0] === 'otp'),
    ['otp', { email: 'existing@example.test', token: '123456', type: 'email' }]);
  assert.deepEqual(f.sent, ['initial-token']);
  assert(!f.calls.some(c => ['FORBIDDEN', 'login', 'update', 'recover'].includes(c[0])));
  await assert.rejects(f.entry.verifyOtpLogin('123456'));
});

test('email OTP request failure returns to ready, spends the send, and a corrected address succeeds after the cooldown', async () => {
  const clock = stubNow(1000000000000);
  try {
    const f = fixture();
    f.auth.signInWithOtp = async () => ({ error: new Error('private') });
    await assert.rejects(f.entry.requestOtpLogin('unknown@example.test'));
    assert.equal(f.entry.state, 'ready');
    // The dispatched request spends the send even though it failed:
    // the provider may have accepted it and lost the response.
    assert.equal(f.entry.otp.sendsLeft, 4);
    // The cooldown binds plain re-requests too, not only resends.
    await assert.rejects(f.entry.requestOtpLogin('existing@example.test'));
    assert.equal(f.entry.otp.sendsLeft, 4);
    clock.advance(60000);
    f.auth.signInWithOtp = async value => { f.calls.push(['otp-request', value]); return { data: {} }; };
    assert.deepEqual(await f.entry.requestOtpLogin('existing@example.test'), { kind: 'otp-code-required' });
    assert.equal(f.entry.otp.sendsLeft, 3);
    assert.equal(f.sent.length, 0);
  } finally { clock.restore(); }
});

test('email OTP ambiguous dispatch still spends the send and starts the cooldown', async () => {
  const clock = stubNow(1000000000000);
  try {
    const f = fixture();
    let dispatches = 0;
    f.auth.signInWithOtp = async () => { dispatches += 1; throw new Error('response lost'); };
    await assert.rejects(f.entry.requestOtpLogin('existing@example.test'));
    assert.equal(f.entry.state, 'ready');
    assert.equal(f.entry.otp.sendsLeft, 4);
    assert(f.entry.otp.resendWaitMs > 0);
    await assert.rejects(f.entry.requestOtpLogin('existing@example.test'));
    assert.equal(dispatches, 1);
    clock.advance(60000);
    await assert.rejects(f.entry.requestOtpLogin('existing@example.test'));
    assert.equal(dispatches, 2);
    assert.equal(f.entry.otp.sendsLeft, 3);
  } finally { clock.restore(); }
});

test('email OTP failed resend keeps the code step, spends the send, and the old code still verifies', async () => {
  const clock = stubNow(1000000000000);
  try {
    const f = fixture();
    await f.entry.requestOtpLogin('existing@example.test');
    clock.advance(60000);
    f.auth.signInWithOtp = async () => ({ error: new Error('private') });
    await assert.rejects(f.entry.resendOtpLogin());
    assert.equal(f.entry.state, 'otp-code');
    assert.equal(f.entry.otp.sendsLeft, 3);
    await assert.rejects(f.entry.resendOtpLogin());
    assert.equal(f.entry.otp.sendsLeft, 3);
    assert.deepEqual(await f.entry.verifyOtpLogin('123456'), { kind: 'handed-off' });
  } finally { clock.restore(); }
});

test('email OTP rejected resend preserves the remaining attempts on the current code', async () => {
  const clock = stubNow(1000000000000);
  try {
    const f = fixture();
    await f.entry.requestOtpLogin('existing@example.test');
    f.auth.verifyOtp = async () => ({ error: new Error('private') });
    for (let i = 0; i < 4; i++) await assert.rejects(f.entry.verifyOtpLogin('000000'));
    assert.equal(f.entry.otp.attemptsLeft, 1);
    clock.advance(60000);
    f.auth.signInWithOtp = async () => ({ error: new Error('private') });
    await assert.rejects(f.entry.resendOtpLogin());
    assert.equal(f.entry.state, 'otp-code');
    assert.equal(f.entry.otp.attemptsLeft, 1);
    // The preserved budget is real: one more wrong code spends it.
    await assert.rejects(f.entry.verifyOtpLogin('000000'));
    assert.equal(f.entry.state, 'ready');
    assert.equal(f.sent.length, 0);
  } finally { clock.restore(); }
});

test('email OTP post-verification boundary failure is terminal, never a code retry', async () => {
  const f = fixture();
  await f.entry.requestOtpLogin('existing@example.test');
  f.auth.getUser = async () => ({ error: new Error('private') });
  await assert.rejects(f.entry.verifyOtpLogin('123456'));
  assert.equal(f.entry.state, 'restart-required');
  await assert.rejects(f.entry.verifyOtpLogin('123456'));
  await assert.rejects(f.entry.resendOtpLogin());
  assert.equal(f.sent.length, 0);
});

test('email OTP wrong codes stay on the code step until the attempt budget is spent', async () => {
  const clock = stubNow(1000000000000);
  try {
  const f = fixture();
  await f.entry.requestOtpLogin('existing@example.test');
  f.auth.verifyOtp = async value => { f.calls.push(['otp', value]); return { error: new Error('private') }; };
  for (let spent = 1; spent <= 4; spent++) {
    await assert.rejects(f.entry.verifyOtpLogin('000000'));
    assert.equal(f.entry.state, 'otp-code');
    assert.equal(f.entry.otp.attemptsLeft, 5 - spent);
  }
  await assert.rejects(f.entry.verifyOtpLogin('000000'));
  assert.equal(f.entry.state, 'ready');
  assert.equal(f.sent.length, 0);
  // A fresh code can be requested and used after the budget reset
  // (the resend cooldown still applies to the new request).
  clock.advance(60000);
  await f.entry.requestOtpLogin('existing@example.test');
  f.auth.verifyOtp = async value => {
    f.calls.push(['otp', value]);
    return { data: { user: { ...f.user }, session: { access_token: 'initial-token', user: { id } } } };
  };
  assert.deepEqual(await f.entry.verifyOtpLogin('123456'), { kind: 'handed-off' });
  assert.deepEqual(f.sent, ['initial-token']);
  } finally { clock.restore(); }
});

test('email OTP malformed code spends no attempt and never reaches the provider', async () => {
  const f = fixture();
  await f.entry.requestOtpLogin('existing@example.test');
  for (const bad of ['12345', '1234567', 'abcdef', 123456, null]) {
    await assert.rejects(f.entry.verifyOtpLogin(bad));
  }
  assert.equal(f.entry.otp.attemptsLeft, 5);
  assert(!f.calls.some(c => c[0] === 'otp'));
  assert.equal(f.entry.state, 'otp-code');
});

test('email OTP resend honors the cooldown, resets attempts and stops at the send cap', async () => {
  const clock = stubNow(1000000000000);
  try {
    const f = fixture();
    await f.entry.requestOtpLogin('existing@example.test');
    await assert.rejects(f.entry.resendOtpLogin());
    assert.equal(f.entry.otp.sendsLeft, 4);
    f.auth.verifyOtp = async () => ({ error: new Error('private') });
    await assert.rejects(f.entry.verifyOtpLogin('000000'));
    await assert.rejects(f.entry.verifyOtpLogin('000000'));
    assert.equal(f.entry.otp.attemptsLeft, 3);
    clock.advance(60000);
    assert.deepEqual(await f.entry.resendOtpLogin(), { kind: 'otp-code-required' });
    assert.equal(f.entry.otp.attemptsLeft, 5);
    assert.equal(f.entry.otp.sendsLeft, 3);
    for (const left of [2, 1, 0]) {
      clock.advance(60000);
      await f.entry.resendOtpLogin();
      assert.equal(f.entry.otp.sendsLeft, left);
    }
    clock.advance(60000);
    await assert.rejects(f.entry.resendOtpLogin());
    assert.equal(f.entry.state, 'restart-required');
    await assert.rejects(f.entry.requestOtpLogin('existing@example.test'));
    assert.equal(f.calls.filter(c => c[0] === 'otp-request').length, 5);
  } finally { clock.restore(); }
});

test('back to email keeps the send budget across the round trip', async () => {
  const clock = stubNow(1000000000000);
  try {
    const f = fixture();
    for (let sends = 1; sends <= 4; sends++) {
      await f.entry.requestOtpLogin('existing@example.test');
      assert.equal(f.entry.otp.sendsLeft, 5 - sends);
      assert.deepEqual(await f.entry.backToEmail(), { kind: 'email-required' });
      assert.equal(f.entry.state, 'ready');
      clock.advance(60000);
    }
    await f.entry.requestOtpLogin('existing@example.test');
    assert.equal(f.entry.otp.sendsLeft, 0);
    await f.entry.backToEmail();
    await assert.rejects(f.entry.requestOtpLogin('existing@example.test'));
    assert.equal(f.entry.state, 'restart-required');
    assert.equal(f.calls.filter(c => c[0] === 'otp-request').length, 5);
  } finally { clock.restore(); }
});

test('email OTP login composes with TOTP factors and the rotated token', async () => {
  const f = fixture([factor()]);
  await f.entry.requestOtpLogin('existing@example.test');
  assert.deepEqual(await f.entry.verifyOtpLogin('123456'), { kind: 'choose-totp', factorIds: ['totp-1'] });
  assert.equal(f.sent.length, 0);
  await f.entry.chooseAndChallengeTOTP('totp-1');
  await f.entry.verifyTOTP('123456');
  assert.deepEqual(f.sent, ['rotated-token']);
});

test('disposal during email OTP verification prevents handoff', async () => {
  const f = fixture();
  await f.entry.requestOtpLogin('existing@example.test');
  const gate = deferred();
  f.auth.verifyOtp = () => gate.promise;
  const running = f.entry.verifyOtpLogin('123456');
  f.entry.dispose();
  gate.resolve({ data: { user: { ...f.user }, session: { access_token: 'initial-token', user: { id } } } });
  await assert.rejects(running);
  assert.equal(f.entry.state, 'disposed');
  assert.equal(f.sent.length, 0);
});

test('email OTP login completes a pending registration resumption before handoff', async () => {
  const f = fixture();
  const events = [];
  const entry = createExistingEntry({
    auth: f.auth,
    handoff: async token => { events.push(['handoff', token]); },
    resumeRegistration: async ({ subject, token }) => { events.push(['resume', subject, token]); },
  });
  await entry.requestOtpLogin('existing@example.test');
  await entry.verifyOtpLogin('123456');
  assert.deepEqual(events, [['resume', id, 'initial-token'], ['handoff', 'initial-token']]);
});
