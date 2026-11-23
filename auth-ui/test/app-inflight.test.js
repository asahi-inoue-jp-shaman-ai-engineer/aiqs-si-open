import test from 'node:test';
import assert from 'node:assert/strict';
import { createInFlight } from '../src/app-inflight.js';

// Astra HIGH 2: busy buttons alone cannot stop a second submission
// (the user can navigate back and re-enter while a POST is pending,
// and renderers re-enable the button). The guard lives in the
// submission path itself, checked synchronously before any await.

test('the first start wins and a concurrent start is rejected', () => {
  const guard = createInFlight();
  assert.equal(guard.tryStart('irradiation'), true);
  assert.equal(guard.tryStart('irradiation'), false);
  assert.equal(guard.isActive('irradiation'), true);
});

test('end releases the operation so a later submission can start', () => {
  const guard = createInFlight();
  assert.equal(guard.tryStart('scan'), true);
  guard.end('scan');
  assert.equal(guard.isActive('scan'), false);
  assert.equal(guard.tryStart('scan'), true);
});

test('operations are independent and ending an unknown key is a no-op', () => {
  const guard = createInFlight();
  assert.equal(guard.tryStart('target-save'), true);
  assert.equal(guard.tryStart('reading'), true);
  assert.doesNotThrow(() => guard.end('nope'));
  assert.equal(guard.isActive('target-save'), true);
});

test('two overlapping handlers run the protected work only once', async () => {
  const guard = createInFlight();
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  let runs = 0;
  const handler = async () => {
    if (!guard.tryStart('irradiation')) return 'rejected';
    try {
      runs += 1;
      await gate;
      return 'submitted';
    } finally {
      guard.end('irradiation');
    }
  };
  const first = handler();
  const second = handler();
  assert.equal(await second, 'rejected');
  release();
  assert.equal(await first, 'submitted');
  assert.equal(runs, 1);
});
