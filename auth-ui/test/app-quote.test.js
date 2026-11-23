import test from 'node:test';
import assert from 'node:assert/strict';
import {
  createConfirmSession, createDebouncer, createQuoteGate,
  isConfirmationCurrent, prepareDisposition, quoteMatchesCount,
} from '../src/app-quote.js';

// Reproduction of the field finding (2026-11-22 live check): on
// the irradiation confirmation screen, moving the slider left the
// PREVIOUS count's total on screen for seconds — the quote
// response for the old count was still in flight and, when it
// landed, was rendered as if it belonged to the new count. The
// gate + count match are what the confirm screen now applies
// before a quote may reach the totals or become the active
// confirmation.

test('a late quote for a previous count is rejected after the count changes', () => {
  const gate = createQuoteGate();
  // The confirm screen opens: a quote is requested for 33.
  const staleToken = gate.begin();
  // The user moves the slider to 1: a newer quote is requested.
  const currentToken = gate.begin();
  // The 33-quote response arrives late. It must not be applied:
  // its request is no longer the latest, and its count is not
  // the count now selected.
  assert.equal(gate.isCurrent(staleToken), false);
  assert.equal(quoteMatchesCount({ count: 33 }, 1), false);
  // The 1-quote response is the only one that may render.
  assert.equal(gate.isCurrent(currentToken), true);
  assert.equal(quoteMatchesCount({ count: 1 }, 1), true);
});

test('the displayed total only ever comes from the latest, matching quote', () => {
  const gate = createQuoteGate();
  // Minimal model of the confirm screen's apply rule.
  let displayedTotal = null;
  const applyQuote = (token, quote, selectedCount) => {
    if (!gate.isCurrent(token)) return false;
    if (!quoteMatchesCount(quote, selectedCount)) return false;
    displayedTotal = quote.credits;
    return true;
  };
  const forThirtyThree = gate.begin();
  const forOne = gate.begin();
  // Out-of-order arrival: the new count's quote lands first...
  assert.equal(applyQuote(forOne, { count: 1, credits: 4 }, 1), true);
  assert.equal(displayedTotal, 4);
  // ...then the stale 33 quote lands. The total must stay 4 —
  // never flash the old count's 129 as the 1-count total.
  assert.equal(applyQuote(forThirtyThree, { count: 33, credits: 129 }, 1), false);
  assert.equal(displayedTotal, 4);
});

test('quote matching is strict about the count and tolerates no quote', () => {
  assert.equal(quoteMatchesCount({ count: 33 }, 33), true);
  assert.equal(quoteMatchesCount({ count: 33 }, '33'), true);
  assert.equal(quoteMatchesCount({ count: 33 }, 34), false);
  assert.equal(quoteMatchesCount(null, 33), false);
  assert.equal(quoteMatchesCount(undefined, 33), false);
  assert.equal(quoteMatchesCount({}, 33), false);
});

test('each gate is independent', () => {
  const a = createQuoteGate();
  const b = createQuoteGate();
  const tokenA = a.begin();
  b.begin();
  assert.equal(a.isCurrent(tokenA), true);
});

// --- Confirmation generations, debounce lifecycle, dispositions
// (Astra adversarial review, 2026-11-22, MEDIUM 1-3). The gate
// above orders requests within one visit; these cover visits
// against each other.

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
    fireAll() {
      const callbacks = [...pending.values()];
      pending.clear();
      for (const fn of callbacks) fn();
    },
    pendingCount() {
      return pending.size;
    },
  };
}

test('confirm session: entering or leaving a visit stales its generation', () => {
  const session = createConfirmSession();
  const visitOne = session.enter();
  assert.equal(session.isCurrent(visitOne), true);
  session.invalidate(); // leaving the screen
  assert.equal(session.isCurrent(visitOne), false);
  const visitTwo = session.enter();
  assert.equal(session.isCurrent(visitTwo), true);
  assert.equal(session.isCurrent(visitOne), false);
});

test('debouncer fires once, and cancel or reschedule retires the previous callback', () => {
  const timers = fakeTimers();
  const debouncer = createDebouncer({
    delayMs: 350,
    setTimeoutFn: timers.setTimeoutFn,
    clearTimeoutFn: timers.clearTimeoutFn,
  });
  const ran = [];
  debouncer.schedule(() => ran.push('first'));
  debouncer.schedule(() => ran.push('second')); // replaces the first
  timers.fireAll();
  assert.deepEqual(ran, ['second']);

  debouncer.schedule(() => ran.push('cancelled'));
  debouncer.cancel();
  assert.equal(debouncer.pending(), false);
  timers.fireAll();
  assert.deepEqual(ran, ['second'], 'a cancelled callback never runs');
});

test('prepare disposition: stale failures drop, login always routes, current applies', () => {
  // Astra MEDIUM 2: the original quote request failing late —
  // after a newer quote succeeded and an irradiation was
  // accepted — must not be applied to the current screen.
  assert.equal(
    prepareDisposition({ kind: 'error', status: 503 }, { current: false }), 'drop');
  assert.equal(
    prepareDisposition({ kind: 'ok', data: { error: 'scan_expired' } }, { current: false }),
    'drop');
  assert.equal(
    prepareDisposition({ kind: 'ok', data: { quote: { count: 33 } } }, { current: false }),
    'drop');
  // Authentication expiry is global, not visit-scoped.
  assert.equal(prepareDisposition({ kind: 'login' }, { current: false }), 'login');
  assert.equal(prepareDisposition({ kind: 'login' }, { current: true }), 'login');
  // A current request keeps the existing behavior: errors and
  // successes alike are applied by the caller.
  assert.equal(
    prepareDisposition({ kind: 'error', status: 503 }, { current: true }), 'apply');
  assert.equal(
    prepareDisposition({ kind: 'ok', data: { quote: { count: 33 } } }, { current: true }),
    'apply');
});

test('a debounce from the previous visit cannot strand the next visit (MEDIUM 1 timeline)', () => {
  const timers = fakeTimers();
  const session = createConfirmSession();
  const gate = createQuoteGate();
  const debouncer = createDebouncer({
    delayMs: 350,
    setTimeoutFn: timers.setTimeoutFn,
    clearTimeoutFn: timers.clearTimeoutFn,
  });

  // A minimal model of the confirmation screen wired exactly as
  // app.js wires it: prepare() captures the gate token and the
  // session generation, and a response applies only through the
  // disposition + gate + count + slider checks.
  let selectedCount = 33;
  let appliedQuote = null;
  const begunCounts = [];
  const prepare = (count) => {
    begunCounts.push(count);
    const token = gate.begin();
    const generation = session.current();
    return (quote) => {
      const disposition = prepareDisposition(
        { kind: 'ok', data: { quote } },
        { current: session.isCurrent(generation) });
      if (disposition !== 'apply') return false;
      if (!gate.isCurrent(token)) return false;
      if (!quoteMatchesCount(quote, count)) return false;
      if (selectedCount !== count) return false;
      appliedQuote = quote;
      return true;
    };
  };
  const queuePrepare = (count) => {
    const generation = session.current();
    debouncer.schedule(() => {
      if (!session.isCurrent(generation)) return;
      prepare(count);
    });
  };

  // Visit 1: open at 33, quote lands.
  session.enter();
  const applyVisitOne33 = prepare(33);
  assert.equal(applyVisitOne33({ count: 33, credits: 129 }), true);
  // The user selects 99: a debounce is scheduled for it.
  selectedCount = 99;
  queuePrepare(99);
  assert.equal(timers.pendingCount(), 1);

  // Before it fires, the user returns to the ranking and
  // re-opens confirmation. Entry cancels the pending timer and
  // starts a new generation; the slider resets to 33.
  debouncer.cancel();
  session.enter();
  selectedCount = 33;
  const applyVisitTwo33 = prepare(33);

  // Any timer the old visit left behind fires now: its work must
  // not begin (no 99 request advancing the gate past visit 2's).
  timers.fireAll();
  assert.deepEqual(begunCounts, [33, 33], 'the stale debounce never began a 99 prepare');

  // Visit 2's own quote applies — the screen is not stranded at
  // "計算中…" the way the reproduction left it.
  assert.equal(applyVisitTwo33({ count: 33, credits: 129 }), true);
  assert.deepEqual(appliedQuote, { count: 33, credits: 129 });
});

test('isConfirmationCurrent requires the same visit, scan and count', () => {
  const parked = {
    confirmation: { confirmation_id: 'c1' },
    confirmationScanId: 'scan-1',
    confirmationGeneration: 3,
    preparedCount: 33,
  };
  const context = { scanId: 'scan-1', generation: 3, count: 33 };
  assert.equal(isConfirmationCurrent(parked, context), true);
  assert.equal(isConfirmationCurrent(parked, { ...context, count: 1 }), false,
    'the slider moved during submission: the parked totals are stale');
  assert.equal(isConfirmationCurrent(parked, { ...context, generation: 4 }), false,
    'a later visit must not repaint an earlier visit’s confirmation');
  assert.equal(isConfirmationCurrent(parked, { ...context, scanId: 'scan-2' }), false);
  assert.equal(
    isConfirmationCurrent({ ...parked, confirmation: null }, context), false);
  assert.equal(isConfirmationCurrent({}, context), false);
});
