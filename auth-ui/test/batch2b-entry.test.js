import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  createIndependentEntry, createLocalPendingStore, createRegistrationResumer,
} from '../src/independent-entry.js';
import { createExistingEntry } from '../src/existing-entry.js';

// Batch 2b item 11 (A2): an interrupted signup must not strand a
// confirmed user without a usable SI identity. The pending
// registration is durable, construction restores it, and an
// authenticated login completes a matching sealed registration
// before any handoff.
const subject = '11111111-1111-4111-8111-111111111111';
const other = '22222222-2222-4222-8222-222222222222';
const result = (id = subject) => ({
  data: { user: { id }, session: { user: { id }, access_token: 'synthetic-token' } },
});

function memoryStorage() {
  const map = new Map();
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => map.set(key, String(value)),
    removeItem: (key) => map.delete(key),
    get length() { return map.size; },
    key: (index) => [...map.keys()][index] ?? null,
    map,
  };
}

function fixture({ store = null } = {}) {
  const calls = [], transfers = [];
  const auth = {
    signInWithPassword: async (args) => { calls.push(['login', args]); return result(); },
    signUp: async (args) => { calls.push(['signup', args]); return { data: { user: { id: subject }, session: null } }; },
    verifyOtp: async (args) => { calls.push(['verify', args]); return result(); },
    getUser: async () => ({ data: { user: { id: subject } } }),
  };
  const entry = createIndependentEntry({
    auth, origin: 'https://si.test', pendingStore: store,
    fetch: async (...args) => {
      calls.push(['complete', ...args]);
      return { ok: true, json: async () => ({ registration_complete: true }) };
    },
    handoff: async (token) => transfers.push(token),
  });
  return { entry, auth, calls, transfers };
}

test('local pending store round-trips and rejects malformed records', () => {
  const storage = memoryStorage();
  const store = createLocalPendingStore(storage);
  assert.equal(store.load(), null);
  store.save({ email: 'a@example.test', subject, referralCode: 'code-1' });
  const loaded = store.load();
  assert.equal(loaded.email, 'a@example.test');
  assert.equal(loaded.subject, subject);
  assert.equal(loaded.referralCode, 'code-1');
  // The record never carries a password or a token.
  assert.deepEqual(Object.keys(JSON.parse([...storage.map.values()][0])).sort(),
    ['email', 'referralCode', 'savedAt', 'subject']);
  store.clear(loaded);
  assert.equal(store.load(), null);
  storage.setItem('si-pending-registration-v1:' + subject + ':r-x',
                  '{"subject":"not-a-uuid"}');
  assert.equal(store.load(), null);
});

test('signup persists the pending registration; a fresh controller restores verification', async () => {
  const store = createLocalPendingStore(memoryStorage());
  const first = fixture({ store });
  await first.entry.signup({ email: 'new@example.test', password: 'synthetic' });
  const saved = store.load();
  assert.equal(saved.email, 'new@example.test');
  assert.equal(saved.subject, subject);
  assert.equal(saved.referralCode, null);
  // A page reload disposes the controller; the next one resumes at
  // the verification step instead of losing the registration.
  const second = fixture({ store });
  assert.equal(second.entry.state, 'verification');
  await second.entry.verify('123456');
  await second.entry.complete();
  assert.deepEqual(second.transfers, ['synthetic-token']);
  assert.equal(store.load(), null);
});

test('login completes a matching pending registration before handoff', async () => {
  const store = createLocalPendingStore(memoryStorage());
  store.save({ email: 'new@example.test', subject, referralCode: null });
  const f = fixture({ store });
  // Construction restored 'verification'; abandon it to reach login.
  f.entry.abandonPending();
  assert.equal(f.entry.state, 'ready');
  store.save({ email: 'new@example.test', subject, referralCode: null });
  await f.entry.login({ email: 'new@example.test', password: 'synthetic' });
  assert.deepEqual(f.calls.map((c) => c[0]), ['login', 'complete']);
  assert.equal(store.load(), null);
  assert.deepEqual(f.transfers, ['synthetic-token']);
});

test('login neither completes nor drops another user\'s pending record', async () => {
  const store = createLocalPendingStore(memoryStorage());
  const f = fixture({ store });
  store.save({ email: 'someone@example.test', subject: other, referralCode: null });
  await f.entry.login({ email: 'new@example.test', password: 'synthetic' });
  assert.deepEqual(f.calls.map((c) => c[0]), ['login']);
  // Subject-scoped (Astra round-2 F12/N4): the other account's
  // record is not this login's to complete or discard.
  assert.equal(store.loadFor(other).email, 'someone@example.test');
  assert.deepEqual(f.transfers, ['synthetic-token']);
});

test('existing entry resumes registration before handoff and stops when it fails', async () => {
  const seen = [];
  const auth = {
    signInWithPassword: async () => result(),
    getSession: async () => ({ data: { session: { access_token: 'synthetic-token', user: { id: subject } } } }),
    getUser: async () => ({ data: { user: { id: subject } } }),
  };
  const entry = createExistingEntry({
    auth,
    resumeRegistration: async ({ subject: s, token }) => { seen.push([s, token]); },
    handoff: async (token) => seen.push(['handoff', token]),
  });
  await entry.passwordLogin({ email: 'a@example.test', password: 'synthetic' });
  assert.deepEqual(seen, [[subject, 'synthetic-token'], ['handoff', 'synthetic-token']]);

  const blocked = [];
  const failing = createExistingEntry({
    auth,
    resumeRegistration: async () => { throw new Error('nope'); },
    handoff: async (token) => blocked.push(token),
  });
  await assert.rejects(failing.passwordLogin({ email: 'a@example.test', password: 'synthetic' }));
  assert.deepEqual(blocked, []);
});

test('registration resumer posts only for a matching record and clears it', async () => {
  const store = createLocalPendingStore(memoryStorage());
  const posts = [];
  const resume = createRegistrationResumer({
    origin: 'https://si.test', pendingStore: store,
    fetch: async (...args) => {
      posts.push(args);
      return { ok: true, json: async () => ({ registration_complete: true }) };
    },
  });
  await resume({ subject, token: 'synthetic-token' });
  assert.equal(posts.length, 0);
  store.save({ email: 'a@example.test', subject: other, referralCode: null });
  await resume({ subject, token: 'synthetic-token' });
  assert.equal(posts.length, 0);
  store.save({ email: 'a@example.test', subject, referralCode: null });
  await resume({ subject, token: 'synthetic-token' });
  assert.equal(posts.length, 1);
  assert.equal(posts[0][0], 'https://si.test/onboarding/complete');
  // Only the completed subject's record is cleared; the other
  // account's record remains under its own key.
  assert.equal(store.loadFor(subject), null);
  assert.equal(store.loadFor(other).email, 'a@example.test');
});

// --- Astra round-1 findings (F10/F11/F12) ---

import { createBrowserPendingStore } from '../src/independent-entry.js';

test('restored verification state still allows login, which completes the registration (F10)', async () => {
  const store = createLocalPendingStore(memoryStorage());
  store.save({ email: 'new@example.test', subject, referralCode: null });
  const f = fixture({ store });
  assert.equal(f.entry.state, 'verification');
  // No abandon, no manual re-save: login is accepted from the
  // restored state, completes the matching registration, and only
  // then hands off.
  await f.entry.login({ email: 'new@example.test', password: 'synthetic' });
  assert.deepEqual(f.calls.map((c) => c[0]), ['login', 'complete']);
  assert.equal(store.load(), null);
  assert.deepEqual(f.transfers, ['synthetic-token']);
});

test('browser pending store is null when localStorage is unavailable (F11)', () => {
  const had = Object.getOwnPropertyDescriptor(globalThis, 'window');
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: { get localStorage() { throw new Error('denied'); } },
  });
  try {
    assert.equal(createBrowserPendingStore(), null);
  } finally {
    if (had) Object.defineProperty(globalThis, 'window', had);
    else delete globalThis.window;
  }
  // And with a working storage it returns a functional store.
  Object.defineProperty(globalThis, 'window', {
    configurable: true, value: { localStorage: memoryStorage() },
  });
  try {
    const store = createBrowserPendingStore();
    store.save({ email: 'a@example.test', subject, referralCode: null });
    assert.equal(store.load().subject, subject);
  } finally {
    if (had) Object.defineProperty(globalThis, 'window', had);
    else delete globalThis.window;
  }
});

test('resumer never touches another account\'s record (F12, subject-scoped)', async () => {
  const store = createLocalPendingStore(memoryStorage());
  const posts = [];
  const resume = createRegistrationResumer({
    origin: 'https://si.test', pendingStore: store,
    fetch: async (...args) => {
      posts.push(args);
      return { ok: true, json: async () => ({ registration_complete: true }) };
    },
  });
  store.save({ email: 'a@example.test', subject: other, referralCode: null });
  await resume({ subject, token: 'synthetic-token' });
  assert.equal(posts.length, 0);
  // The other account's record is not this flow's to complete or
  // discard: it stays, keyed under its own subject, for its owner.
  assert.equal(store.loadFor(other).email, 'a@example.test');
  assert.equal(store.loadFor(subject), null);
});

// --- Astra round-2 findings (N3/N4) ---

test('login without a durable store still completes this session\'s signup (N3)', async () => {
  const f = fixture({ store: null });
  await f.entry.signup({ email: 'new@example.test', password: 'synthetic' });
  assert.equal(f.entry.state, 'verification');
  // The pending registration exists only in this controller's
  // memory; logging in as the same user completes it before the
  // handoff instead of skipping completion entirely.
  await f.entry.login({ email: 'new@example.test', password: 'synthetic' });
  assert.deepEqual(f.calls.map((c) => c[0]), ['signup', 'login', 'complete']);
  assert.deepEqual(f.transfers, ['synthetic-token']);
});

test('resumer success clears only the completed record (N4)', async () => {
  const storage = memoryStorage();
  const store = createLocalPendingStore(storage);
  const resume = createRegistrationResumer({
    origin: 'https://si.test', pendingStore: store,
    fetch: async () => ({ ok: true, json: async () => ({ registration_complete: true }) }),
  });
  store.save({ email: 'a@example.test', subject, referralCode: null });
  // Another tab registers a different account before A's
  // completion resolves: the records live under different keys,
  // and the subject-scoped resumer finds A's record even though
  // B's is newer.
  store.save({ email: 'b@example.test', subject: other, referralCode: null });
  await resume({ subject, token: 'synthetic-token' });
  assert.equal(store.loadFor(subject), null);
  assert.equal(store.loadFor(other).email, 'b@example.test');
});

test('clearing names an exact record; other records survive (F12)', () => {
  const storeA = createLocalPendingStore(memoryStorage());
  const first = storeA.save({ email: 'a@example.test', subject, referralCode: null });
  const second = storeA.save({ email: 'a@example.test', subject, referralCode: 'newer' });
  // A stale holder of the FIRST record deletes only that record:
  // the newer one lives under a different immutable key.
  storeA.clear(first);
  assert.equal(storeA.load().referralCode, 'newer');
  storeA.clear(second);
  assert.equal(storeA.load(), null);
});

test('a pre-upgrade legacy record is migrated, not abandoned (N7)', async () => {
  const storage = memoryStorage();
  // The round-2 layout: one record under the bare key, no savedAt.
  storage.setItem('si-pending-registration-v1', JSON.stringify({
    email: 'a@example.test', subject, referralCode: 'code-9',
  }));
  const store = createLocalPendingStore(storage);
  const posts = [];
  const resume = createRegistrationResumer({
    origin: 'https://si.test', pendingStore: store,
    fetch: async (...args) => {
      posts.push(args);
      return { ok: true, json: async () => ({ registration_complete: true }) };
    },
  });
  await resume({ subject, token: 'synthetic-token' });
  assert.equal(posts.length, 1);
  // The mutable source is never deleted (Astra round-8 N20);
  // the completed identity is retired by its tombstone, so no
  // load path returns it again.
  assert.equal(store.load(), null);
  assert.equal(store.loadFor(subject), null);
});

// --- Astra round-4 findings (F12 save race, N10) ---

test('a save never deletes a record published after it began (F12)', () => {
  const base = memoryStorage();
  let nested = null;
  const storage = {
    getItem: (k) => base.getItem(k),
    removeItem: (k) => base.removeItem(k),
    get length() { return base.length; },
    key: (i) => base.key(i),
    setItem: (k, v) => {
      // While B's save is publishing, tab A publishes its own
      // newer record for the same subject.
      if (nested) { const run = nested; nested = null; run(); }
      base.setItem(k, v);
    },
  };
  const storeA = createLocalPendingStore(storage);
  const storeB = createLocalPendingStore(storage);
  storeA.save({ email: 'a@example.test', subject, referralCode: null });
  let aNewest = null;
  nested = () => {
    aNewest = storeA.save({ email: 'a@example.test', subject, referralCode: 'A2' });
  };
  storeB.save({ email: 'a@example.test', subject, referralCode: 'B' });
  // A's record, published during B's save, survived B's cleanup:
  // its immutable key is still in storage. (Which record loadFor
  // calls newest depends on cross-instance savedAt ordering and
  // is not what this test pins.)
  assert.ok(base.getItem(aNewest.key));
  assert.equal(JSON.parse(base.getItem(aNewest.key)).referralCode, 'A2');
});

test('a round-3 per-subject record is migrated for loadFor (N10)', async () => {
  const storage = memoryStorage();
  // The round-3 layout: per-subject key, no record-id segment.
  storage.setItem('si-pending-registration-v1:' + subject, JSON.stringify({
    email: 'a@example.test', subject, referralCode: null, savedAt: 5,
  }));
  const store = createLocalPendingStore(storage);
  const posts = [];
  const resume = createRegistrationResumer({
    origin: 'https://si.test', pendingStore: store,
    fetch: async (...args) => {
      posts.push(args);
      return { ok: true, json: async () => ({ registration_complete: true }) };
    },
  });
  await resume({ subject, token: 'synthetic-token' });
  assert.equal(posts.length, 1);
  assert.equal(store.load(), null);
});

// --- Astra round-5 findings (N7/N10 fallbacks) ---

test('migration fallback stays usable when writes fail (N7/N10)', async () => {
  const base = memoryStorage();
  const storage = {
    getItem: (k) => base.getItem(k),
    removeItem: (k) => base.removeItem(k),
    get length() { return base.length; },
    key: (i) => base.key(i),
    // Migration destination writes fail; other writes (the
    // completion tombstone) succeed.
    setItem: (k, v) => {
      if (k.startsWith('si-pending-registration-v1:')) {
        throw new Error('quota');
      }
      base.setItem(k, v);
    },
  };
  base.setItem('si-pending-registration-v1', JSON.stringify({
    email: 'a@example.test', subject, referralCode: null,
  }));
  base.setItem('si-pending-registration-v1:' + other, JSON.stringify({
    email: 'b@example.test', subject: other, referralCode: null, savedAt: 5,
  }));
  const store = createLocalPendingStore(storage);
  // load() restores a fallback even though no copy could be
  // written, and each subject's fallback survives independently.
  assert.equal(store.loadFor(subject).email, 'a@example.test');
  assert.equal(store.loadFor(other).email, 'b@example.test');
  assert.ok(store.load());
  // The resumer completes the fallback record. Historical
  // sources are never deleted (they are mutable — Astra round-7
  // N17/N19): an immutable tombstone retires the identity, and
  // every later load suppresses the record.
  const resume = createRegistrationResumer({
    origin: 'https://si.test', pendingStore: store,
    fetch: async () => ({ ok: true, json: async () => ({ registration_complete: true }) }),
  });
  await resume({ subject, token: 'synthetic-token' });
  assert.equal(store.loadFor(subject), null);
  assert.ok([...base.map.keys()].some(
    (key) => key.startsWith('si-completed-registration-v1:')));
});

// --- Astra round-10 findings, answered in round 11 ---

const PENDING = 'si-pending-registration-v1';
function putRecord(storage, key, record) {
  storage.setItem(key, JSON.stringify(record));
}
function immutableKey(id) {
  return PENDING + ':' + subject + ':' + id;
}

test('loadFor filters tombstoned records before ranking (R10-N1)', () => {
  const storage = memoryStorage();
  const store = createLocalPendingStore(storage);
  // A live registration saved earlier, and a NEWER record for
  // the same subject whose identity was completed (tombstoned).
  // Choosing the maximum first and filtering afterwards hid the
  // live record behind the retired one: loadFor returned null
  // while load() returned the live record.
  putRecord(storage, immutableKey('r-live'), {
    email: 'live@example.test', subject, referralCode: null, savedAt: 100,
  });
  putRecord(storage, immutableKey('r-done'), {
    email: 'done@example.test', subject, referralCode: null, savedAt: 200,
  });
  store.clear({ key: PENDING, subject, email: 'done@example.test',
                savedAt: 200, referralCode: null });
  assert.equal(store.load().email, 'live@example.test');
  assert.equal(store.loadFor(subject).email, 'live@example.test');
});

test('in-flight migration cannot resurrect a completed identity (N7/N10)', () => {
  for (const layout of ['bare', 'per-subject']) {
    const base = memoryStorage();
    const sourceKey = layout === 'bare' ? PENDING : PENDING + ':' + subject;
    putRecord(base, sourceKey, {
      email: 'a@example.test', subject, referralCode: null, savedAt: 100,
    });
    // Tab A pauses immediately before publishing its immutable
    // migration copy; while it is paused, tab B (on the raw
    // storage) migrates and completes the identity, writing its
    // tombstone. Tab A then publishes its delayed copy.
    let armed = true;
    const storageA = {
      getItem: (k) => base.getItem(k),
      removeItem: (k) => base.removeItem(k),
      get length() { return base.length; },
      key: (i) => base.key(i),
      setItem: (k, v) => {
        if (armed && k.startsWith(PENDING + ':') &&
            k.split(':').length > 2) {
          armed = false;
          const storeB = createLocalPendingStore(base);
          const record = storeB.loadFor(subject);
          assert.ok(record, layout + ': tab B sees the registration');
          storeB.clear(record);
        }
        base.setItem(k, v);
      },
    };
    const storeA = createLocalPendingStore(storageA);
    // The initiating tab's own return value must not be the
    // completed record, and neither may a fresh store's loads.
    assert.equal(storeA.loadFor(subject), null, layout + ': in-flight');
    const fresh = createLocalPendingStore(base);
    assert.equal(fresh.load(), null, layout + ': fresh load');
    assert.equal(fresh.loadFor(subject), null, layout + ': fresh loadFor');
  }
});

test('newest live record wins across both historical layouts (N23)', () => {
  for (const [bareAt, perSubjectAt, winner] of [
    [100, 200, 'per@example.test'],
    [200, 100, 'bare@example.test'],
  ]) {
    const storage = memoryStorage();
    putRecord(storage, PENDING, {
      email: 'bare@example.test', subject, referralCode: null, savedAt: bareAt,
    });
    putRecord(storage, PENDING + ':' + subject, {
      email: 'per@example.test', subject, referralCode: null,
      savedAt: perSubjectAt,
    });
    const store = createLocalPendingStore(storage);
    assert.equal(store.loadFor(subject).email, winner);
    assert.equal(store.load().email, winner);
  }
});

test('load and loadFor agree when a migration copy cannot be written (N23)', () => {
  const base = memoryStorage();
  putRecord(base, immutableKey('r-copy'), {
    email: 'copy@example.test', subject, referralCode: null, savedAt: 100,
  });
  putRecord(base, PENDING, {
    email: 'source@example.test', subject, referralCode: null, savedAt: 200,
  });
  const storage = {
    getItem: (k) => base.getItem(k),
    removeItem: (k) => base.removeItem(k),
    get length() { return base.length; },
    key: (i) => base.key(i),
    setItem: (k, v) => {
      // Immutable destination writes fail; tombstones succeed.
      if (k.startsWith(PENDING + ':') && k.split(':').length > 2) {
        throw new Error('quota');
      }
      base.setItem(k, v);
    },
  };
  const store = createLocalPendingStore(storage);
  // The unwritable newer source survives as a fallback; both
  // loads must return it — previously load() returned the older
  // stored copy while loadFor() returned the fallback.
  assert.equal(store.loadFor(subject).email, 'source@example.test');
  assert.equal(store.load().email, 'source@example.test');
});

test('tombstone identity encoding separates null and empty referral (R10-N2)', () => {
  const storage = memoryStorage();
  const store = createLocalPendingStore(storage);
  putRecord(storage, immutableKey('r-empty'), {
    email: 'a@example.test', subject, referralCode: '', savedAt: 100,
  });
  // A tombstone for the SAME subject/savedAt/email but a null
  // referral code names a different identity: the ':'-joined
  // encoding used to map both to one key and suppress this
  // record.
  store.clear({ key: PENDING, subject, email: 'a@example.test',
                savedAt: 100, referralCode: null });
  assert.equal(store.loadFor(subject).email, 'a@example.test');
  // And the null-referral identity is suppressed by its own
  // tombstone, exactly as before.
  putRecord(storage, immutableKey('r-null'), {
    email: 'a@example.test', subject, referralCode: null, savedAt: 100,
  });
  assert.equal(store.loadFor(subject).email, 'a@example.test');
  store.clear({ key: PENDING, subject, email: 'a@example.test',
                savedAt: 100, referralCode: '' });
  assert.equal(store.loadFor(subject), null);
});

test('both historical copies failing migration keep the newest (round-11 N23)', () => {
  // Astra round-11 counterexample 1: bare savedAt=200 and
  // per-subject savedAt=100, with every immutable write failing.
  // The second failed migration used to overwrite the newer
  // fallback with the older record, and the bare key was
  // invisible to load()'s enumeration — both APIs regressed to
  // the older record. One enumeration + per-identity fallbacks
  // keep the newest.
  const base = memoryStorage();
  putRecord(base, PENDING, {
    email: 'bare-new@example.test', subject, referralCode: null, savedAt: 200,
  });
  putRecord(base, PENDING + ':' + subject, {
    email: 'per-old@example.test', subject, referralCode: null, savedAt: 100,
  });
  const storage = {
    getItem: (k) => base.getItem(k),
    removeItem: (k) => base.removeItem(k),
    get length() { return base.length; },
    key: (i) => base.key(i),
    setItem: (k, v) => {
      if (k.startsWith(PENDING + ':') && k.split(':').length > 2) {
        throw new Error('quota');
      }
      base.setItem(k, v);
    },
  };
  const store = createLocalPendingStore(storage);
  assert.equal(store.loadFor(subject).email, 'bare-new@example.test');
  assert.equal(store.load().email, 'bare-new@example.test');
});

test('equal timestamps resolve identically in both APIs and seed orders (round-11 N23)', () => {
  // Astra round-11 counterexample 2: an immutable record and a
  // per-subject historical record with the SAME savedAt but
  // different identities. The winner must be one deterministic
  // record — the same for load() and loadFor(), and the same
  // whichever order the keys were inserted in.
  const winners = [];
  for (const order of [0, 1]) {
    const storage = memoryStorage();
    const entries = [
      [immutableKey('r-tie'), { email: 'aaa@example.test', subject,
                                referralCode: null, savedAt: 100 }],
      [PENDING + ':' + subject, { email: 'bbb@example.test', subject,
                                  referralCode: null, savedAt: 100 }],
    ];
    if (order) entries.reverse();
    for (const [key, record] of entries) putRecord(storage, key, record);
    const store = createLocalPendingStore(storage);
    const viaFor = store.loadFor(subject);
    const viaLoad = store.load();
    assert.equal(viaLoad.email, viaFor.email);
    winners.push(viaFor.email);
  }
  assert.equal(winners[0], winners[1]);
});

test('one malformed record never suppresses valid registrations (round-12 R12-N1)', () => {
  // Corrupt JSON under the bare historical key, beside a valid
  // immutable record: both APIs must still find the valid one.
  const storage = memoryStorage();
  storage.setItem(PENDING, '{not json');
  putRecord(storage, immutableKey('r-valid'), {
    email: 'valid@example.test', subject, referralCode: null, savedAt: 100,
  });
  const store = createLocalPendingStore(storage);
  assert.equal(store.loadFor(subject).email, 'valid@example.test');
  assert.equal(store.load().email, 'valid@example.test');
  // Corrupt JSON under ANOTHER subject's key must not affect
  // this subject's scoped load either.
  const storage2 = memoryStorage();
  storage2.setItem(PENDING + ':' + other + ':r-bad', '{broken');
  putRecord(storage2, immutableKey('r-valid'), {
    email: 'valid@example.test', subject, referralCode: null, savedAt: 100,
  });
  const store2 = createLocalPendingStore(storage2);
  assert.equal(store2.loadFor(subject).email, 'valid@example.test');
});

test('a superseded fallback cannot resurrect after its replacement completes (round-12 R12-N2)', () => {
  // Two store instances over one storage = two tabs. Immutable
  // writes fail throughout, so migration works via fallbacks.
  const base = memoryStorage();
  putRecord(base, PENDING, {
    email: 'e@example.test', subject, referralCode: 'old', savedAt: 100,
  });
  const storage = {
    getItem: (k) => base.getItem(k),
    removeItem: (k) => base.removeItem(k),
    get length() { return base.length; },
    key: (i) => base.key(i),
    setItem: (k, v) => {
      if (k.startsWith(PENDING + ':') && k.split(':').length > 2) {
        throw new Error('quota');
      }
      base.setItem(k, v);
    },
  };
  const tabA = createLocalPendingStore(storage);
  assert.equal(tabA.loadFor(subject).referralCode, 'old');
  // The source is replaced by a newer registration (same
  // email, newer savedAt, different referral).
  putRecord(base, PENDING, {
    email: 'e@example.test', subject, referralCode: 'new', savedAt: 200,
  });
  const current = tabA.loadFor(subject);
  assert.equal(current.referralCode, 'new');
  // Tab B completes the replacement; its tombstone retires it.
  const tabB = createLocalPendingStore(storage);
  tabB.clear(tabB.loadFor(subject));
  // Tab A must NOT resurrect the obsolete 'old' fallback: its
  // source no longer equals the cached record in full.
  assert.equal(tabA.loadFor(subject), null);
  assert.equal(tabA.load(), null);
});

test('malformed bare source cannot block retirement of the per-subject source (round-13 R13-N1)', () => {
  // Astra round-13 reproduction: malformed JSON under the bare
  // key, a valid registration under the per-subject key.
  // clear() used to parse both sources inside one try — the
  // bare source's parse threw, the per-subject source never
  // got its tombstone, and the completed registration
  // resurrected on the next load.
  const storage = memoryStorage();
  storage.setItem(PENDING, '{malformed');
  putRecord(storage, PENDING + ':' + subject, {
    email: 'valid@example.test', subject, referralCode: null, savedAt: 100,
  });
  const store = createLocalPendingStore(storage);
  const copy = store.loadFor(subject);
  assert.equal(copy.email, 'valid@example.test');
  store.clear(copy);
  assert.equal(store.loadFor(subject), null);
  assert.equal(store.load(), null);
});

test('transient enumeration failure during migration cannot suppress selection (round-14 R14-N2)', () => {
  // storage.key() throws ONCE (during migration's
  // enumeration) and then works: selection reads the records
  // independently and must still return the valid record.
  const inner = memoryStorage();
  putRecord(inner, immutableKey(subject, 100), {
    email: 'stable@example.test', subject, referralCode: null, savedAt: 100,
  });
  let thrown = false;
  const flaky = {
    getItem: (k) => inner.getItem(k),
    setItem: (k, v) => inner.setItem(k, v),
    removeItem: (k) => inner.removeItem(k),
    get length() { return inner.length; },
    key: (i) => {
      if (!thrown) { thrown = true; throw new Error('transient'); }
      return inner.key(i);
    },
  };
  const store = createLocalPendingStore(flaky);
  assert.equal(store.load().email, 'stable@example.test');
  assert.equal(store.loadFor(subject).email, 'stable@example.test');
});

test('non-finite savedAt records are invisible to selection (round-11 R10-N2)', () => {
  // JSON.parse turns the valid literal 1e400 into Infinity;
  // JSON.stringify would turn it back into null inside a
  // tombstone key, colliding distinct identities. Such records
  // are rejected at parse time, before migration, selection,
  // or tombstoning.
  const storage = memoryStorage();
  storage.setItem(PENDING + ':' + subject,
    '{"subject":"' + subject + '","email":"inf@example.test",'
    + '"referralCode":null,"savedAt":1e400}');
  putRecord(storage, immutableKey('r-real'), {
    email: 'real@example.test', subject, referralCode: null, savedAt: 100,
  });
  const store = createLocalPendingStore(storage);
  assert.equal(store.loadFor(subject).email, 'real@example.test');
  assert.equal(store.load().email, 'real@example.test');
});
