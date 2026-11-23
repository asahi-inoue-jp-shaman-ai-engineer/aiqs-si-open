import test from 'node:test';
import assert from 'node:assert/strict';
import { setBusy } from '../src/app-busy.js';

function fakeButton(text = 'スキャンする（無料）') {
  return { textContent: text, disabled: false, dataset: {} };
}

test('setBusy disables the button and swaps in the busy label', () => {
  const button = fakeButton();
  setBusy(button, 'スキャンを受け付けています…');
  assert.equal(button.disabled, true);
  assert.equal(button.textContent, 'スキャンを受け付けています…');
});

test('restore brings back the original label and enabled state', () => {
  const button = fakeButton();
  const restore = setBusy(button, '送信しています…');
  restore();
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, 'スキャンする（無料）');
  // A second restore is a no-op (finally blocks may overlap).
  restore();
  assert.equal(button.textContent, 'スキャンする（無料）');
});

test('a nested setBusy cannot strand the busy label', () => {
  const button = fakeButton();
  const restoreOuter = setBusy(button, '外側の処理中…');
  const restoreInner = setBusy(button, '内側の処理中…');
  restoreInner();
  restoreOuter();
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, 'スキャンする（無料）');
});

test('a button that was already disabled stays disabled after restore', () => {
  const button = fakeButton('この内容で照射する');
  button.disabled = true;
  const restore = setBusy(button, '照射を受け付けています…');
  restore();
  assert.equal(button.disabled, true);
});

test('restore runs the re-render callback after restoring the snapshot', () => {
  // Astra MEDIUM 3: while a submission was in flight, a refreshed
  // confirmation can make the button ineligible (insufficient
  // balance). The caller re-renders from that fresh state in the
  // restore callback, and it must win over the stale snapshot.
  const button = fakeButton('リーディングする');
  let calls = 0;
  const restore = setBusy(button, '受け付けています…', () => {
    calls += 1;
    button.disabled = true;
  });
  restore();
  assert.equal(calls, 1);
  assert.equal(button.disabled, true);
  assert.equal(button.textContent, 'リーディングする');
  // Still idempotent: a second restore does not re-run the callback.
  restore();
  assert.equal(calls, 1);
});

test('setBusy tolerates a missing button', () => {
  const restore = setBusy(null, '処理中…');
  assert.doesNotThrow(() => restore());
});
