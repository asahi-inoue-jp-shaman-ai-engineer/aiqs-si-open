import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createClient } from '@supabase/supabase-js';

test('email signup verifier is available to a new SDK instance using shared browser storage', async () => {
  const items = new Map();
  const storage = { getItem: k => items.get(k) ?? null, setItem: (k, v) => items.set(k, v), removeItem: k => items.delete(k) };
  let verifier;
  const user = { id: 'synthetic-user', aud: 'authenticated', created_at: new Date().toISOString() };
  const fetch = async (url, options) => {
    const body = JSON.parse(options.body);
    if (new URL(url).pathname.endsWith('/signup')) {
      assert.equal(body.code_challenge_method, 's256');
      assert.ok(body.code_challenge);
      return new Response(JSON.stringify({ user }), { status: 200 });
    }
    assert.equal(new URL(url).searchParams.get('grant_type'), 'pkce');
    verifier = body.code_verifier;
    const claims = Buffer.from(JSON.stringify({ sub: user.id, exp: Math.floor(Date.now() / 1000) + 3600 })).toString('base64url');
    return new Response(JSON.stringify({ user, access_token: `eyJhbGciOiJIUzI1NiJ9.${claims}.c3ludGhldGlj`,
      refresh_token: 'synthetic', token_type: 'bearer', expires_in: 3600 }), { status: 200 });
  };
  const options = { auth: { storage, storageKey: 'aiqs-si-auth', flowType: 'pkce', autoRefreshToken: false, detectSessionInUrl: false }, global: { fetch } };
  const firstTab = createClient('https://auth.example.test', 'sb_publishable_synthetic', options);
  const signup = await firstTab.auth.signUp({ email: 'synthetic@example.test', password: 'synthetic-test-only' });
  assert.equal(signup.error, null);
  assert.ok(items.has('aiqs-si-auth-code-verifier'));
  const newTab = createClient('https://auth.example.test', 'sb_publishable_synthetic', options);
  const result = await newTab.auth.exchangeCodeForSession('synthetic-code');
  assert.equal(result.error, null);
  assert.ok(verifier.length >= 43);
  assert.equal(items.has('aiqs-si-auth-code-verifier'), false);
});
