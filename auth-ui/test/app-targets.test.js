import test from 'node:test';
import assert from 'node:assert/strict';
import {
  reconcileStaleLoad, registeredTarget, targetsFromLoad,
  withRegisteredTarget, withoutTarget, withRenamedTarget,
} from '../src/app-targets.js';

const known = [{ id: 't1', name: 'first', has_photo: true }];

test('a successful load replaces the known list', () => {
  const fresh = [{ id: 't2', name: 'second', has_photo: true }];
  const loaded = targetsFromLoad(known, { kind: 'ok', status: 200, data: { targets: fresh } });
  assert.deepEqual(loaded, { targets: fresh, failed: false });
});

test('a successful empty load is a real empty list, not a failure', () => {
  const loaded = targetsFromLoad(known, { kind: 'ok', status: 200, data: { targets: [] } });
  assert.deepEqual(loaded, { targets: [], failed: false });
});

test('failed loads keep the known list and flag the failure', () => {
  // Every failure shape the client can produce for the list fetch:
  // admission/maintenance statuses (now kind 'error'), a network
  // error, an unparsable body, and — defensively — an ok result
  // whose body is an error or lacks the targets array.
  const failures = [
    { kind: 'error', status: 503 },
    { kind: 'error', status: 429 },
    { kind: 'error', status: 500 },
    { kind: 'error' },
    { kind: 'ok', status: 200, data: { error: 'not_found' } },
    { kind: 'ok', status: 200, data: {} },
    { kind: 'ok', status: 200, data: { targets: 'nope' } },
    null,
    undefined,
  ];
  for (const result of failures) {
    const loaded = targetsFromLoad(known, result);
    assert.equal(loaded.failed, true, JSON.stringify(result));
    assert.deepEqual(loaded.targets, known, JSON.stringify(result));
  }
});

test('a login result is reported as login, with the list untouched', () => {
  const loaded = targetsFromLoad(known, { kind: 'login' });
  assert.equal(loaded.login, true);
  assert.deepEqual(loaded.targets, known);
});

test('registeredTarget reads the register response shapes', () => {
  assert.deepEqual(
    registeredTarget({ target_id: 't9' }, { name: 'new', hasPhoto2: true }),
    { id: 't9', name: 'new', has_photo: true, has_photo2: true });
  assert.deepEqual(
    registeredTarget({ target: { id: 't8', name: 'from-server' } }, { name: 'form' }),
    { id: 't8', name: 'from-server', has_photo: true, has_photo2: false });
  assert.equal(registeredTarget({}, { name: 'x' }), null);
  assert.equal(registeredTarget(null, { name: 'x' }), null);
});

test('withRegisteredTarget appends once and ignores replays', () => {
  const created = { id: 't9', name: 'new', has_photo: true, has_photo2: false };
  const once = withRegisteredTarget(known, created);
  assert.deepEqual(once, [...known, created]);
  assert.deepEqual(withRegisteredTarget(once, created), once);
  assert.deepEqual(withRegisteredTarget(known, null), known);
});

test('withoutTarget removes the row and ignores unknown ids', () => {
  assert.deepEqual(withoutTarget(known, 't1'), []);
  assert.deepEqual(withoutTarget(known, 'nope'), known);
  assert.deepEqual(withoutTarget(null, 't1'), []);
});

test('withRenamedTarget updates only the matching row', () => {
  const renamed = withRenamedTarget(known, 't1', '新しい名前');
  assert.deepEqual(renamed, [{ id: 't1', name: '新しい名前', has_photo: true }]);
  assert.deepEqual(withRenamedTarget(known, 'nope', 'x'), known);
});

test('a delete applied locally survives a follow-up failed load', () => {
  // Delete succeeds, then the refresh fetch fails: the failure-
  // preserving load must not resurrect the deleted target.
  const afterDelete = withoutTarget(known, 't1');
  const loaded = targetsFromLoad(afterDelete, { kind: 'error', status: 503 });
  assert.equal(loaded.failed, true);
  assert.deepEqual(loaded.targets, []);
});

test('a rename applied locally survives a follow-up failed load', () => {
  const afterRename = withRenamedTarget(known, 't1', '新しい名前');
  const loaded = targetsFromLoad(afterRename, { kind: 'error', status: 429 });
  assert.equal(loaded.failed, true);
  assert.equal(loaded.targets[0].name, '新しい名前');
});

// --- Stale-response races (Astra HIGH 1) -------------------------
// A list request starts while the cache is [A]; before it resolves,
// a registration lands and the cache becomes [A, B]. The late
// response predates the registration and must not discard B.

const A = { id: 't1', name: 'first', has_photo: true };
const B = { id: 't9', name: 'new', has_photo: true, has_photo2: false };

test('a late failed load does not discard a registration that landed meanwhile', () => {
  const loaded = reconcileStaleLoad([A], [A, B], { kind: 'error', status: 503 });
  assert.equal(loaded.failed, true);
  assert.deepEqual(loaded.targets, [A, B]);
});

test('a late successful load predating a registration keeps the new target', () => {
  const loaded = reconcileStaleLoad([A], [A, B], {
    kind: 'ok', status: 200, data: { targets: [A] },
  });
  assert.equal(loaded.failed, false);
  assert.deepEqual(loaded.targets.map((target) => target.id), ['t1', 't9']);
});

test('a late successful load does not resurrect a target deleted meanwhile', () => {
  const loaded = reconcileStaleLoad([A, B], [A], {
    kind: 'ok', status: 200, data: { targets: [A, B] },
  });
  assert.deepEqual(loaded.targets.map((target) => target.id), ['t1']);
});

test('a late successful load does not revert a rename applied meanwhile', () => {
  const renamed = { ...A, name: '新しい名前' };
  const loaded = reconcileStaleLoad([A], [renamed], {
    kind: 'ok', status: 200, data: { targets: [A] },
  });
  assert.equal(loaded.targets[0].name, '新しい名前');
});

test('a stale load still reports login and keeps the current cache', () => {
  const loaded = reconcileStaleLoad([A], [A, B], { kind: 'login' });
  assert.equal(loaded.login, true);
  assert.deepEqual(loaded.targets, [A, B]);
});

test('register success then a failed reload keeps the new target', () => {
  // The production failure: registration succeeded, the follow-up
  // list fetch failed (503), and the app showed "not registered".
  let targets = [...known];
  const created = registeredTarget({ target_id: 't9' }, { name: 'new' });
  targets = withRegisteredTarget(targets, created);
  const loaded = targetsFromLoad(targets, { kind: 'error', status: 503 });
  assert.equal(loaded.failed, true);
  assert.deepEqual(loaded.targets.map((target) => target.id), ['t1', 't9']);
});
