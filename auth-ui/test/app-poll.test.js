import test from 'node:test';
import assert from 'node:assert/strict';
import { createPoller } from '../src/app-poll.js';

// Lifecycle tests for the generation-guarded poller (Astra
// adversarial review, 2026-11-22, MEDIUM 5): clearing a timer
// used to leave an in-flight poll alive, and its late response
// rescheduled polling from the wrong screen, navigated back to
// a finished job, and could leave two timers running. The
// handler below mirrors app.js pollJob's mapping (non-terminal
// states continue, terminal states stop) so the loop's contract
// is exercised the way the app drives it.

function fakeTimers() {
  let nextId = 1;
  const pending = new Map();
  return {
    setTimeoutFn(fn) {
      const id = nextId;
      nextId += 1;
      pending.set(id, fn);
      return id;
    },
    clearTimeoutFn(id) {
      pending.delete(id);
    },
    // Runs the oldest scheduled callback; returns its promise so
    // the test can await the whole tick.
    fireNext() {
      const entry = pending.entries().next().value;
      assert.ok(entry, 'expected a scheduled timer');
      pending.delete(entry[0]);
      return entry[1]();
    },
    pendingCount() {
      return pending.size;
    },
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((res) => { resolve = res; });
  return { promise, resolve };
}

function makePoller(timers, fetchJob) {
  return createPoller({
    fetchJob,
    initialDelayMs: 800,
    intervalMs: 3000,
    setTimeoutFn: timers.setTimeoutFn,
    clearTimeoutFn: timers.clearTimeoutFn,
  });
}

// Mirrors pollJob in app.js: records updates/terminals and maps
// states to the poller's continue/stop contract.
function recordingHandler(record) {
  return (result) => {
    const view = result.data;
    if (view.state === 'queued' || view.state === 'running' || view.state === 'unknown') {
      record.updates.push(view);
      return 'continue';
    }
    record.terminals.push(view);
    return 'stop';
  };
}

test('polling continues on non-terminal views and delivers the terminal payload', async () => {
  const timers = fakeTimers();
  const responses = [deferred(), deferred()];
  let call = 0;
  const poller = makePoller(timers, () => {
    const d = responses[call];
    call += 1;
    return d.promise;
  });
  const record = { updates: [], terminals: [] };
  poller.start('job-1', recordingHandler(record));
  assert.equal(timers.pendingCount(), 1);

  const firstTick = timers.fireNext();
  responses[0].resolve({ kind: 'ok', data: { state: 'running', execution: { completed_count: 12 } } });
  await firstTick;
  assert.equal(record.updates.length, 1);
  assert.equal(timers.pendingCount(), 1, 'a running view reschedules exactly one timer');

  const secondTick = timers.fireNext();
  const terminalView = { state: 'completed', execution: { completed_count: 33, requested_count: 33 } };
  responses[1].resolve({ kind: 'ok', data: terminalView });
  await secondTick;
  // The terminal payload itself reaches the handler (the app
  // renders counts and the proof screen from it — MEDIUM 4).
  assert.deepEqual(record.terminals, [terminalView]);
  assert.equal(timers.pendingCount(), 0, 'a terminal view ends the session');
});

test('stop during an in-flight poll drops the response and reschedules nothing', async () => {
  const timers = fakeTimers();
  const response = deferred();
  const poller = makePoller(timers, () => response.promise);
  const record = { updates: [], terminals: [] };
  poller.start('job-1', recordingHandler(record));

  const tick = timers.fireNext(); // request now in flight
  poller.stop(); // the user navigates away (show() -> stopPoll)
  response.resolve({ kind: 'ok', data: { state: 'running' } });
  await tick;

  assert.equal(record.updates.length, 0, 'the late response never reaches the handler');
  assert.equal(record.terminals.length, 0);
  assert.equal(timers.pendingCount(), 0, 'no poll is rescheduled from the old screen');
});

test('a superseded session cannot interfere with the new one or double the timers', async () => {
  const timers = fakeTimers();
  const oldResponse = deferred();
  const newResponse = deferred();
  const poller = makePoller(timers, (jobId) =>
    (jobId === 'old-job' ? oldResponse.promise : newResponse.promise));
  const oldRecord = { updates: [], terminals: [] };
  const newRecord = { updates: [], terminals: [] };

  poller.start('old-job', recordingHandler(oldRecord));
  const oldTick = timers.fireNext(); // old request in flight
  // A new polling session starts before the old response lands
  // (the user opened another job).
  poller.start('new-job', recordingHandler(newRecord));
  assert.equal(timers.pendingCount(), 1, 'only the new session has a timer');

  // The old response resolves late — including a terminal state
  // that used to navigate the user back to the finished job.
  oldResponse.resolve({ kind: 'ok', data: { state: 'completed' } });
  await oldTick;
  assert.equal(oldRecord.terminals.length, 0, 'the old terminal response is dropped');
  assert.equal(timers.pendingCount(), 1, 'the new session is untouched');

  const newTick = timers.fireNext();
  newResponse.resolve({ kind: 'ok', data: { state: 'running' } });
  await newTick;
  assert.equal(newRecord.updates.length, 1);
  assert.equal(timers.pendingCount(), 1, 'exactly one timer chain survives');
});

test('a handler that navigates away (stop) is not rescheduled even when it says continue', async () => {
  const timers = fakeTimers();
  const response = deferred();
  const poller = makePoller(timers, () => response.promise);
  poller.start('job-1', () => {
    poller.stop(); // e.g. a login failure routes to another screen
    return 'continue';
  });
  const tick = timers.fireNext();
  response.resolve({ kind: 'ok', data: { state: 'running' } });
  await tick;
  assert.equal(timers.pendingCount(), 0);
});

test('a rejected fetch reaches the handler as an error result, once', async () => {
  const timers = fakeTimers();
  const poller = makePoller(timers, () => Promise.reject(new Error('offline')));
  const seen = [];
  poller.start('job-1', (result) => {
    seen.push(result.kind);
    return 'stop';
  });
  await timers.fireNext();
  assert.deepEqual(seen, ['error']);
  assert.equal(timers.pendingCount(), 0);
});
