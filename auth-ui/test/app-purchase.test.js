import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  PURCHASE_RETURN_KEY, clearPurchaseReturn, packLabel,
  purchaseReturnIntent, savePurchaseReturn, shortageCredits,
  takePurchaseReturn,
} from '../src/app-purchase.js';

function fakeStorage() {
  const map = new Map();
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => { map.set(key, String(value)); },
    removeItem: (key) => { map.delete(key); },
    has: (key) => map.has(key),
  };
}

test('purchaseReturnIntent keeps a scan id and a valid count', () => {
  assert.deepEqual(purchaseReturnIntent({ scanId: 'scan-1', count: 33 }),
    { scanId: 'scan-1', count: 33 });
  assert.deepEqual(purchaseReturnIntent({ scanId: 'scan-1', count: 1 }),
    { scanId: 'scan-1', count: 1 });
  assert.deepEqual(purchaseReturnIntent({ scanId: 'scan-1', count: 99 }),
    { scanId: 'scan-1', count: 99 });
});

test('purchaseReturnIntent allows a null count (reading detour)', () => {
  assert.deepEqual(purchaseReturnIntent({ scanId: 'scan-1' }),
    { scanId: 'scan-1', count: null });
  assert.deepEqual(purchaseReturnIntent({ scanId: 'scan-1', count: null }),
    { scanId: 'scan-1', count: null });
});

test('purchaseReturnIntent rejects contexts that cannot return', () => {
  assert.equal(purchaseReturnIntent(null), null);
  assert.equal(purchaseReturnIntent({}), null);
  assert.equal(purchaseReturnIntent({ scanId: '' }), null);
  assert.equal(purchaseReturnIntent({ scanId: 42 }), null);
  assert.equal(purchaseReturnIntent({ scanId: 's', count: 0 }), null);
  assert.equal(purchaseReturnIntent({ scanId: 's', count: 100 }), null);
  assert.equal(purchaseReturnIntent({ scanId: 's', count: 3.5 }), null);
  assert.equal(purchaseReturnIntent({ scanId: 's', count: '33' }), null);
});

test('save + take round-trips the intent and consumes it once', () => {
  const storage = fakeStorage();
  assert.equal(savePurchaseReturn(storage, { scanId: 'scan-1', count: 66 }), true);
  assert.equal(storage.has(PURCHASE_RETURN_KEY), true);
  assert.deepEqual(takePurchaseReturn(storage), { scanId: 'scan-1', count: 66 });
  assert.equal(takePurchaseReturn(storage), null);
});

test('save with an unusable context writes nothing', () => {
  const storage = fakeStorage();
  assert.equal(savePurchaseReturn(storage, { count: 33 }), false);
  assert.equal(storage.has(PURCHASE_RETURN_KEY), false);
  assert.equal(savePurchaseReturn(null, { scanId: 's', count: 1 }), false);
});

test('take survives malformed stored values', () => {
  const storage = fakeStorage();
  storage.setItem(PURCHASE_RETURN_KEY, 'not-json');
  assert.equal(takePurchaseReturn(storage), null);
  storage.setItem(PURCHASE_RETURN_KEY, JSON.stringify({ scanId: 's', count: 1000 }));
  assert.equal(takePurchaseReturn(storage), null);
  storage.setItem(PURCHASE_RETURN_KEY, JSON.stringify({ count: 33 }));
  assert.equal(takePurchaseReturn(storage), null);
  assert.equal(takePurchaseReturn(null), null);
});

test('clearPurchaseReturn drops a stored intent', () => {
  const storage = fakeStorage();
  savePurchaseReturn(storage, { scanId: 'scan-1', count: 33 });
  clearPurchaseReturn(storage);
  assert.equal(takePurchaseReturn(storage), null);
});

test('shortageCredits reads only a negative numeric balance-after', () => {
  assert.equal(shortageCredits(-129), 129);
  assert.equal(shortageCredits(-1), 1);
  assert.equal(shortageCredits(0), null);
  assert.equal(shortageCredits(500), null);
  assert.equal(shortageCredits(null), null);
  assert.equal(shortageCredits(undefined), null);
  assert.equal(shortageCredits('−129'), null);
});

test('packLabel follows the purchase page format', () => {
  assert.equal(packLabel({ yen: 500, credits: 500 }), '500円 → 500クレジット');
  assert.equal(packLabel({ yen: 5000, credits: 5555 }), '5,000円 → 5,555クレジット');
  assert.equal(packLabel({ yen: 10000, credits: 12345 }), '10,000円 → 12,345クレジット');
  assert.equal(packLabel({}), '');
  assert.equal(packLabel(null), '');
  assert.equal(packLabel({ yen: '5000', credits: 5555 }), '');
});
