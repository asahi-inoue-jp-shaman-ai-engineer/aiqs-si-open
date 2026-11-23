import { test } from 'node:test';
import assert from 'node:assert/strict';
import { otpGuidanceLines } from '../src/otp-guidance.js';

// The guidance renders ONLY the controller snapshot's real
// values: every number in a line must come from the snapshot
// field of the same name, and absent/non-finite fields drop
// their line instead of being guessed.

test('a null or non-object snapshot yields no lines', () => {
  assert.deepEqual(otpGuidanceLines(null), []);
  assert.deepEqual(otpGuidanceLines(undefined), []);
  assert.deepEqual(otpGuidanceLines('otp'), []);
});

test('a fresh code step shows attempts and an available resend', () => {
  const lines = otpGuidanceLines({ attemptsLeft: 5, sendsLeft: 4, resendWaitMs: 0 });
  assert.equal(lines.length, 2);
  assert.match(lines[0], /あと5回まで入力できます/);
  assert.match(lines[1], /再送できます/);
  assert.match(lines[1], /あと4回まで送れます/);
});

test('a running resend wait shows the ceiling seconds from the snapshot', () => {
  const lines = otpGuidanceLines({ attemptsLeft: 3, sendsLeft: 2, resendWaitMs: 42500 });
  assert.match(lines[0], /あと3回まで入力できます/);
  assert.match(lines[1], /あと約43秒待ってからできます/);
  assert.match(lines[1], /あと2回まで送れます/);
});

test('an exhausted send budget says so and offers no resend', () => {
  const lines = otpGuidanceLines({ attemptsLeft: 2, sendsLeft: 0, resendWaitMs: 0 });
  assert.equal(lines.length, 2);
  assert.match(lines[1], /上限に達しました/);
  assert.doesNotMatch(lines[1], /再送できます/);
});

test('zero attempts left is stated, never rounded up', () => {
  const lines = otpGuidanceLines({ attemptsLeft: 0, sendsLeft: 1, resendWaitMs: 0 });
  assert.match(lines[0], /入力回数の上限に達しました/);
});

test('missing or non-finite fields drop only their own line', () => {
  assert.deepEqual(otpGuidanceLines({ sendsLeft: 1, resendWaitMs: 0 }).length, 1);
  assert.deepEqual(otpGuidanceLines({ attemptsLeft: 4 }).length, 1);
  assert.deepEqual(
    otpGuidanceLines({ attemptsLeft: Number.NaN, sendsLeft: Number.POSITIVE_INFINITY, resendWaitMs: 0 }),
    [],
  );
  // A sends line without a readable wait falls back to the
  // no-wait wording rather than inventing a countdown.
  const lines = otpGuidanceLines({ attemptsLeft: 1, sendsLeft: 3 });
  assert.match(lines[1], /再送できます/);
});
