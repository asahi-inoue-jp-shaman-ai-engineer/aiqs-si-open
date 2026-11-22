import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseEntryRuntimeConfig } from '../src/entry-runtime-config.js';
import { createMemoryClient } from '../src/independent-entry.js';

const pageOrigin = 'https://authority.example';
const settings = () => ({ runtimeMode: 'configured', providerBase: 'https://upstream.example',
  publishableKey: 'sb_publishable_public', authorityOrigin: pageOrigin, cspConnectOrigin: 'https://upstream.example' });

test('legacy template retains exact synthetic defaults only', () => {
  const legacy = { providerBase: 'https://localhost:9420', publishableKey: 'sb_publishable_si_local_synthetic' };
  assert.deepEqual(parseEntryRuntimeConfig(legacy, pageOrigin), legacy);
  for (const change of [{ providerBase: 'https://other.example' }, { publishableKey: 'sb_publishable_other' },
    { runtimeMode: '' }, { runtimeMode: null }, { runtimeMode: 'synthetic' }, { authorityOrigin: pageOrigin }, { cspConnectOrigin: legacy.providerBase }]) {
    assert.throws(() => parseEntryRuntimeConfig({ ...legacy, ...change }, pageOrigin));
  }
});

test('configured settings are frozen and SDK options remain memory-only', () => {
  const input = settings(); const config = parseEntryRuntimeConfig(input, pageOrigin);
  input.providerBase = 'https://changed.example';
  assert.equal(config.providerBase, 'https://upstream.example');
  assert.throws(() => { config.providerBase = 'https://changed.example'; }, TypeError);
  let called;
  createMemoryClient((...args) => { called = args; }, config);
  assert.deepEqual(called, ['https://upstream.example', 'sb_publishable_public', { auth: {
    persistSession: false, autoRefreshToken: false, detectSessionInUrl: false,
  } }]);
});

test('missing settings, mismatched origins and unknown modes fail before SDK construction', () => {
  const invalid = [];
  for (const field of Object.keys(settings())) { const value = settings(); delete value[field]; invalid.push(value); }
  invalid.push({ ...settings(), runtimeMode: 'unknown' }, { ...settings(), runtimeMode: '' },
    { ...settings(), authorityOrigin: 'https://other.example' },
    { ...settings(), cspConnectOrigin: 'https://other.example' });
  let creations = 0;
  for (const value of invalid) {
    assert.throws(() => createMemoryClient(() => { creations++; }, parseEntryRuntimeConfig(value, pageOrigin)),
      { message: 'Authentication configuration unavailable' });
  }
  assert.equal(creations, 0);
});

test('canonical HTTPS contract rejects ambiguous origins across every origin field', () => {
  const bad = ['http://example.test', 'HTTPS://example.test', 'https://EXAMPLE.test',
    'https://example.test/', 'https://example.test/path', 'https://example.test?', 'https://example.test#',
    'https://user:private@example.test', 'https://example.test:443', 'https://example.test:0444',
    'https://example.test:0', 'https://example.test:65536', 'https://example.test.', 'https://a..test',
    'https://a_.test', 'https://-a.test', 'https://%65xample.test', 'https://127.1', 'https://0x7f000001',
    'https://[::01]', 'https://xn--a.example', 'https://xn--bcher-kva.example', 'https://例.test',
    ' https://example.test', 'https://example.test\n', 'https://example.test\\path'];
  for (const value of bad) {
    for (const field of ['providerBase', 'authorityOrigin', 'cspConnectOrigin']) {
      assert.throws(() => parseEntryRuntimeConfig({ ...settings(), [field]: value }, pageOrigin), field + ': ' + value);
    }
    assert.throws(() => parseEntryRuntimeConfig(settings(), value));
  }
  for (const origin of ['https://localhost:9420', 'https://127.0.0.1:8443', 'https://[::1]:8443']) {
    assert.equal(parseEntryRuntimeConfig({ ...settings(), providerBase: origin, cspConnectOrigin: origin }, pageOrigin).providerBase, origin);
  }
});

test('only bounded publishable keys accepted, no secret or legacy JWT keys', () => {
  for (const publishableKey of [null, '', 'sb_secret_private', 'service_role', 'eyJhbGciOiJIUzI1NiJ9.synthetic.signature',
    'sb_publishable_', 'sb_publishable_a\n', 'sb_publishable_a b', 'sb_publishable_' + 'a'.repeat(513)]) {
    assert.throws(() => parseEntryRuntimeConfig({ ...settings(), publishableKey }, pageOrigin));
  }
  assert.equal(parseEntryRuntimeConfig({ ...settings(), publishableKey: 'sb_publishable_rotated' }, pageOrigin).providerBase, settings().providerBase);
});
