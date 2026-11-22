import { test } from 'node:test';
import assert from 'node:assert/strict';
import { authorizationId, callbackUrl, createFlow, safeRedirect } from '../src/flow.js';

function fixture(overrides = {}) {
  const calls = [];
  const auth = {
    getUser: async () => ({ data: { user: { id: 'user' } } }),
    oauth: {
      getAuthorizationDetails: async () => ({ data: { authorization_id: 'id', user: { id: 'user' },
        client: { name: '<script>untrusted</script>' }, scope: 'openid aiqs:use', redirect_uri: 'https://client.test/callback', ...overrides } }),
      approveAuthorization: async (...args) => { calls.push(['approve', ...args]); return { data: { redirect_url: 'https://client.test/callback?code=synthetic' } }; },
      denyAuthorization: async (...args) => { calls.push(['deny', ...args]); return { data: { redirect_url: 'https://client.test/callback?error=access_denied' } }; }
    }
  };
  return { auth, calls, flow: createFlow(auth, 'id') };
}
test('request ID preserves only one opaque ID, never a redirect URL', () => {
  assert.equal(authorizationId('?authorization_id=abc-123'), 'abc-123');
  assert.equal(authorizationId(''), null);
  for (const s of ['?authorization_id=', '?authorization_id=a&authorization_id=b', '?authorization_id=../../x']) assert.throws(() => authorizationId(s));
  assert.equal(callbackUrl('https://si.test', 'abc'), 'https://si.test/oauth/consent?authorization_id=abc');
});
test('reject active, credential-bearing and remote insecure redirect URLs', () => {
  for (const u of ['javascript:alert(1)', 'data:text/html,x', 'http://remote.test', 'https://user:pass@client.test']) assert.throws(() => safeRedirect(u));
  assert.equal(safeRedirect('http://127.0.0.1:9000/callback'), 'http://127.0.0.1:9000/callback');
});
test('unauthenticated user cannot approve; consent is never automatic', async () => {
  const { auth, calls, flow } = fixture();
  auth.getUser = async () => ({ error: new Error('No session') });
  assert.deepEqual(await flow.load(), { kind: 'login' });
  await assert.rejects(flow.decide(true));
  assert.equal(calls.length, 0);
});
test('verified details precede explicit approval, replay cannot approve again', async () => {
  const { calls, flow } = fixture();
  assert.equal((await flow.load()).kind, 'consent');
  assert.equal(calls.length, 0);
  await flow.decide(true);
  await assert.rejects(flow.decide(true));
  assert.deepEqual(calls, [['approve', 'id', { skipBrowserRedirect: true }]]);
});
test('denial uses provider denial, not approval', async () => {
  const { calls, flow } = fixture();
  await flow.load();
  assert.match(await flow.decide(false), /access_denied/);
  assert.equal(calls[0][0], 'deny');
});
test('mismatched request or identity fails closed', async () => {
  for (const change of [{ authorization_id: 'another' }, { user: { id: 'another' } }]) {
    const { flow, calls } = fixture(change);
    await assert.rejects(flow.load());
    await assert.rejects(flow.decide(true));
    assert.equal(calls.length, 0);
  }
});
test('already-consented provider response supports safe return', async () => {
  const { flow } = fixture({ redirect_url: 'https://client.test/callback?code=synthetic' });
  assert.equal((await flow.load()).kind, 'redirect');
});
