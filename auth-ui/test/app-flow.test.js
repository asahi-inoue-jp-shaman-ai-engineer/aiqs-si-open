import test from 'node:test';
import assert from 'node:assert/strict';
import {
  OMAKASE_AFFIRMATION, SCAN_STEPS, createNavigator, createScanDraft,
  draftProblem, nextScanStep, prevScanStep, resolvedAffirmation,
} from '../src/app-flow.js';

test('the scan path is one line: target, intention, affirmation, confirm, wait-method', () => {
  assert.deepEqual([...SCAN_STEPS],
    ['target', 'intention', 'affirmation', 'scan-confirm', 'wait-method']);
  assert.equal(nextScanStep('target'), 'intention');
  assert.equal(nextScanStep('intention'), 'affirmation');
  assert.equal(nextScanStep('affirmation'), 'scan-confirm');
  assert.equal(nextScanStep('scan-confirm'), 'wait-method');
  assert.equal(nextScanStep('wait-method'), null);
  assert.equal(prevScanStep('wait-method'), 'scan-confirm');
  assert.equal(prevScanStep('scan-confirm'), 'affirmation');
  assert.equal(prevScanStep('target'), null);
  assert.equal(nextScanStep('nope'), null);
  assert.equal(prevScanStep('nope'), null);
});

test('the navigator pushes, dedupes the top, pops and resets', () => {
  const nav = createNavigator('target');
  assert.equal(nav.current(), 'target');
  assert.equal(nav.canBack(), false);
  nav.go('intention');
  nav.go('affirmation');
  nav.go('affirmation');
  assert.deepEqual(nav.entries(), ['target', 'intention', 'affirmation']);
  assert.equal(nav.canBack(), true);
  assert.equal(nav.back(), 'intention');
  assert.equal(nav.back(), 'target');
  assert.equal(nav.back(), 'target');
  nav.go('target-photo');
  assert.deepEqual(nav.entries(), ['target', 'target-photo']);
  nav.reset('home');
  assert.deepEqual(nav.entries(), ['home']);
  assert.equal(nav.canBack(), false);
});

test('a fresh draft is empty and reports the target first', () => {
  const draft = createScanDraft();
  assert.equal(draft.targetId, null);
  assert.equal(draft.affirmationMode, 'custom');
  assert.equal(draftProblem(draft), 'target');
  assert.equal(draftProblem(null), 'target');
});

test('draft problems resolve in flow order', () => {
  const draft = createScanDraft();
  draft.targetId = 'target-1';
  assert.equal(draftProblem(draft), 'intention');
  draft.intention = '  ';
  assert.equal(draftProblem(draft), 'intention');
  draft.intention = '意図';
  assert.equal(draftProblem(draft), 'affirmation');
  draft.affirmation = '私の文';
  assert.equal(draftProblem(draft), null);
});

test('a custom affirmation is submitted exactly as typed', () => {
  const draft = createScanDraft();
  draft.affirmation = '私は自分を大切にしています';
  assert.equal(resolvedAffirmation(draft), '私は自分を大切にしています');
});

test('omakase submits the displayed example sentence, not an empty value', () => {
  const draft = createScanDraft();
  draft.targetId = 'target-1';
  draft.intention = '意図';
  draft.affirmationMode = 'omakase';
  // Even with the custom field filled, the delegated choice wins:
  // what the confirmation screen resolves is what gets sent.
  draft.affirmation = '別の文';
  assert.equal(resolvedAffirmation(draft), OMAKASE_AFFIRMATION);
  assert.match(OMAKASE_AFFIRMATION, /^私は/);
  assert.match(OMAKASE_AFFIRMATION, /自己愛/);
  assert.equal(draftProblem(draft), null);
  assert.equal(resolvedAffirmation(null), '');
});
