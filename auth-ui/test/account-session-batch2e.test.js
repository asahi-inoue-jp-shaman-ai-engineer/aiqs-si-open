import test from 'node:test';
import assert from 'node:assert/strict';
import {
  armFragmentHandoff, createFragmentSession, createTokenProvider,
} from '../src/account-session.js';

// Batch 2e regressions (Astra glass round 2, R1): the account
// pages join the shared-session protocol. A fragment token
// captured at page load is bound to the app session's lineage
// (sid): a replacement lineage retires it, a disappearance
// invalidates it, a record appearing after a recordless
// capture retires it — while a same-lineage rotation (the app
// tab's routine refresh) leaves it valid.

function fakeStorage() {
  const map = new Map();
  return {
    getItem: (k) => map.get(k) ?? null,
    setItem: (k, v) => map.set(k, v),
    removeItem: (k) => map.delete(k),
  };
}

function capture(persistent, { token = 'frag-token' } = {}) {
  const storage = fakeStorage();
  const state = armFragmentHandoff(storage);
  const location = {
    hash: `#access_token=${token}&state=${state}`,
    pathname: '/account', search: '',
  };
  const history = { replaceState: () => {} };
  return createFragmentSession({
    location, history, storage, persistentStorage: persistent,
  });
}

function sharedRecord(persistent, { refreshToken = 'r1', rev, sid }) {
  persistent.setItem('si-app-session-v1', JSON.stringify({
    refreshToken, rev, owner: 'app-tab', sid, savedAt: 1,
  }));
}

test('batch2e R1-account: a same-lineage rotation keeps the page token valid', async () => {
  const persistent = fakeStorage();
  sharedRecord(persistent, { rev: 'rev-1', sid: 'sid-a' });
  const session = capture(persistent);
  assert.equal(await session.getToken(), 'frag-token');
  // The app tab rotates: new revision, SAME lineage.
  sharedRecord(persistent, { refreshToken: 'r2', rev: 'rev-2', sid: 'sid-a' });
  assert.equal(await session.getToken(), 'frag-token');
  let dispatched = null;
  session.withToken((token) => { dispatched = token; });
  assert.equal(dispatched, 'frag-token');
});

test('batch2e R1-account: a replacement lineage retires the page token', async () => {
  const persistent = fakeStorage();
  sharedRecord(persistent, { rev: 'rev-1', sid: 'sid-a' });
  const session = capture(persistent);
  assert.equal(await session.getToken(), 'frag-token');
  // Another tab signs in as a different account: new lineage.
  sharedRecord(persistent, { refreshToken: 'r9', rev: 'rev-9', sid: 'sid-b' });
  assert.equal(await session.getToken(), null);
  let dispatched = 'unset';
  session.withToken((token) => { dispatched = token; });
  assert.equal(dispatched, null);
});

test('batch2e R1-account: a sign-out tombstone (new lineage) retires the page token', async () => {
  const persistent = fakeStorage();
  sharedRecord(persistent, { rev: 'rev-1', sid: 'sid-a' });
  const session = capture(persistent);
  persistent.removeItem('si-app-session-v1');
  persistent.setItem('si-app-session-epoch-v1', JSON.stringify({
    rev: 'rev-2', owner: 'app-tab', kind: 'clear', sid: 'sid-cleared', at: 2,
  }));
  assert.equal(await session.getToken(), null);
});

test('batch2e R1-account: disappearance of the record invalidates the page token', async () => {
  const persistent = fakeStorage();
  sharedRecord(persistent, { rev: 'rev-1', sid: 'sid-a' });
  const session = capture(persistent);
  persistent.removeItem('si-app-session-v1');
  assert.equal(await session.getToken(), null);
});

test('batch2e R1-account: a record appearing after a recordless capture retires the token', async () => {
  const persistent = fakeStorage();
  const session = capture(persistent);
  assert.equal(await session.getToken(), 'frag-token');
  sharedRecord(persistent, { rev: 'rev-1', sid: 'sid-a' });
  assert.equal(await session.getToken(), null);
});

test('batch2e R1-account: createTokenProvider keeps its shape and follows the lineage', async () => {
  const persistent = fakeStorage();
  sharedRecord(persistent, { rev: 'rev-1', sid: 'sid-a' });
  const storage = fakeStorage();
  const state = armFragmentHandoff(storage);
  const location = {
    hash: `#access_token=tok&state=${state}`, pathname: '/account', search: '',
  };
  const provider = createTokenProvider(
    location, { replaceState: () => {} }, storage, persistent);
  assert.equal(await provider(), 'tok');
  sharedRecord(persistent, { refreshToken: 'r9', rev: 'rev-9', sid: 'sid-b' });
  assert.equal(await provider(), null);
});
