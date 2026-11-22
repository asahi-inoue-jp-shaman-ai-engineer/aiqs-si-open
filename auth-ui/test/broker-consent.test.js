import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createBrokerConsent } from '../src/broker-consent.js';
const config = { clientId: '11111111-1111-4111-8111-111111111111', callback: 'https://authority.test/realms/si/broker/upstream/endpoint' };
const granted = config.callback + '?code=synthetic&state=opaque';
function fixture(changes = {}) {
  const calls = [];
  const details = { authorization_id: 'request', user: { id: 'user' }, client: { id: config.clientId }, scope: 'openid email', redirect_uri: config.callback, ...changes };
  const auth = { getUser: async () => ({ data: { user: { id: 'user' } } }), oauth: {
    getAuthorizationDetails: async () => ({ data: details }),
    approveAuthorization: async (...args) => { calls.push(args); return { data: { redirect_url: granted } }; },
    denyAuthorization: async (...args) => { calls.push(args); return { data: { redirect_url: config.callback + '?error=access_denied&state=opaque' } }; }
  } };
  return { auth, calls, flow: createBrokerConsent(auth, 'request', config) };
}
test('broker-only explicit consent and single decision', async () => {
  const { flow, calls } = fixture();
  assert.equal((await flow.load()).kind, 'consent'); assert.equal(calls.length, 0);
  assert.equal(await flow.decide(true), granted);
  await assert.rejects(flow.decide(true)); await assert.rejects(flow.load());
  assert.deepEqual(calls, [['request', { skipBrowserRedirect: true }]]);
});
test('wrong client, user, scope, request or callback cannot authorize', async () => {
  for (const change of [{client:{id:'other'}}, {user:{id:'other'}}, {scope:'openid aiqs:use'}, {scope:'email'},
    {authorization_id:'other'}, {redirect_uri:'https://other.test/cb'}]) {
    const { flow, calls } = fixture(change); await assert.rejects(flow.load());
    await assert.rejects(flow.decide(true)); assert.equal(calls.length, 0);
  }
});
test('missing login and account switching never decide', async () => {
  const { flow, auth, calls } = fixture(); await flow.load();
  auth.getUser = async () => ({ data: { user: { id: 'other' } } });
  await assert.rejects(flow.decide(true)); assert.equal(calls.length, 0);
  auth.getUser = async () => ({ error: new Error('missing') });
  assert.equal((await flow.load()).kind, 'login'); await assert.rejects(flow.decide(false));
});
test('redirect-only and decision result enforce exact broker callback', async () => {
  const invalid = ['https://evil.test/cb?code=x&state=s', config.callback+'?code=x&state=s&access_token=x',
    config.callback+'?code=x&state=s#token', config.callback+'?code=x&state=s&state=t',
    config.callback+'?code=x&error=denied&state=s', config.callback+'?code=x', config.callback+'?code=&state=s',
    'https://user@authority.test/realms/si/broker/upstream/endpoint?code=x&state=s'];
  for (const url of invalid) {
    await assert.rejects(fixture({redirect_url:url}).flow.load());
    const { flow, auth } = fixture(); await flow.load();
    auth.oauth.approveAuthorization = async () => ({ data: { redirect_url:url } });
    await assert.rejects(flow.decide(true));
  }
  assert.deepEqual(await fixture({redirect_url:granted}).flow.load(), {kind:'redirect',url:granted});
});
test('denial and concurrent decision use one provider call', async () => {
  const { flow, calls } = fixture(); await flow.load();
  const first = flow.decide(false); await assert.rejects(flow.decide(true));
  assert.ok((await first).includes('error=access_denied')); assert.equal(calls.length, 1);
});
