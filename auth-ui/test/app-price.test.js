import test from 'node:test';
import assert from 'node:assert/strict';
import { sessionChargeCredits } from '../src/app-price.js';

// The count screen's immediate total must agree with the
// server's one charge computation (pricing.py
// session_charge_credits) on every count, or the estimate the
// user approves at selection time would drift from the quote
// that follows. The expected values are the server's own
// documented examples at the standard rate of 39 tenths
// (3.9 credits/shot).
test('session charge matches the server computation at the standard rate', () => {
  assert.equal(sessionChargeCredits(1, 39), 4);
  assert.equal(sessionChargeCredits(9, 39), 35);
  assert.equal(sessionChargeCredits(33, 39), 129);
  assert.equal(sessionChargeCredits(66, 39), 257);
  assert.equal(sessionChargeCredits(99, 39), 386);
  assert.equal(sessionChargeCredits(0, 39), 0);
});

test('session charge rounds half-up in integer tenths', () => {
  // 5 shots at 3.9 = 19.5 → 20 (half-up, not banker's rounding);
  // a whole-credit rate stays exact.
  assert.equal(sessionChargeCredits(5, 39), 20);
  assert.equal(sessionChargeCredits(2, 39), 8);
  assert.equal(sessionChargeCredits(33, 50), 165);
  assert.equal(sessionChargeCredits(99, 10), 99);
});

test('unusable inputs yield null instead of a computed fiction', () => {
  // A missing or malformed session rate must surface as "no
  // estimate" (the screen shows —), never as a total computed
  // from an invented rate.
  assert.equal(sessionChargeCredits(33, null), null);
  assert.equal(sessionChargeCredits(33, undefined), null);
  assert.equal(sessionChargeCredits(33, 0), null);
  assert.equal(sessionChargeCredits(33, 3.9), null);
  assert.equal(sessionChargeCredits(-1, 39), null);
  assert.equal(sessionChargeCredits(1.5, 39), null);
  assert.equal(sessionChargeCredits('33', 39), null);
});
