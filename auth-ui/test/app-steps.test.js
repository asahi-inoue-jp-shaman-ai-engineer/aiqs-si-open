import test from 'node:test';
import assert from 'node:assert/strict';
import { stepLabelFor } from '../src/app-steps.js';

test('main flow screens carry their step position', () => {
  assert.match(stepLabelFor('target'), /ステップ 1 \/ 5/);
  assert.match(stepLabelFor('target-photo'), /ステップ 1 \/ 5/);
  assert.match(stepLabelFor('target-name'), /ステップ 1 \/ 5/);
  assert.match(stepLabelFor('intention'), /ステップ 2 \/ 5/);
  assert.match(stepLabelFor('affirmation'), /ステップ 2 \/ 5/);
  assert.match(stepLabelFor('scan-confirm'), /ステップ 2 \/ 5/);
  assert.match(stepLabelFor('wait'), /ステップ 3 \/ 5/);
  assert.match(stepLabelFor('ranking'), /ステップ 4 \/ 5/);
  assert.match(stepLabelFor('count'), /ステップ 5 \/ 5/);
  assert.match(stepLabelFor('confirm'), /ステップ 5 \/ 5/);
  assert.match(stepLabelFor('progress'), /ステップ 5 \/ 5/);
});

test('the shared wait-method and notify-wait screens take their step from the flow context', () => {
  // Foundation item 2: one screen serves both flows, so the
  // label follows the operation it was opened for — the scan
  // choice closes the input step, its notify-wait stands
  // where the scan wait stands, and both irradiation screens
  // belong to the irradiation step. Without a context the
  // scan position is the default.
  assert.match(stepLabelFor('wait-method', { operation: 'scan' }), /ステップ 2 \/ 5/);
  assert.match(stepLabelFor('wait-method', { operation: 'irradiation' }), /ステップ 5 \/ 5/);
  assert.match(stepLabelFor('wait-method'), /ステップ 2 \/ 5/);
  assert.match(stepLabelFor('notify-wait', { operation: 'scan' }), /ステップ 3 \/ 5/);
  assert.match(stepLabelFor('notify-wait', { operation: 'irradiation' }), /ステップ 5 \/ 5/);
});

test('the proof screen claims completion only for a completed irradiation', () => {
  assert.match(
    stepLabelFor('proof', { operation: 'irradiation', state: 'completed' }),
    /完了 — 稼働証明/);
});

test('running, queued, failed and cancelled jobs never claim completion', () => {
  for (const state of ['running', 'queued', 'failed', 'cancelled', 'unknown']) {
    const label = stepLabelFor('proof', { operation: 'irradiation', state });
    assert.ok(label, state);
    assert.doesNotMatch(label, /完了/, state);
  }
  // A completed scan's proof is a detail view, not the flow's
  // completion state; and without job context nothing is claimed.
  assert.doesNotMatch(
    stepLabelFor('proof', { operation: 'scan', state: 'completed' }), /完了/);
  assert.doesNotMatch(stepLabelFor('proof'), /完了/);
});

test('screens outside the main flow have no step label', () => {
  for (const screen of ['login', 'home', 'account', 'history', 'guide', 'error']) {
    assert.equal(stepLabelFor(screen), null, screen);
  }
  assert.equal(stepLabelFor('nope'), null);
  assert.equal(stepLabelFor(undefined), null);
});
