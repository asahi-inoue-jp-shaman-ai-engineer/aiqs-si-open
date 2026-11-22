import test from 'node:test';
import assert from 'node:assert/strict';
import { billingReturn } from './src/billing-return.js';

test('return never asserts payment and requires exact navigation state', () => {
  assert.match(billingReturn('?billing=success').message, /支払い確認後/);
  assert.match(billingReturn('?billing=cancel').message, /すでにお支払い/);
  for (const query of ['', '?billing=paid', '?billing=success&billing=cancel', '?code=123']) {
    assert.equal(billingReturn(query), null);
  }
  assert.deepEqual(billingReturn('?billing=success&balance=99999&paid=true'), billingReturn('?billing=success'));
});
