import test from 'node:test';
import assert from 'node:assert/strict';
import {
  IRRADIATION_REJECTED_ERRORS, IRRADIATION_REJECTED_RESPONSES,
  READING_REJECTED_ERRORS, READING_REJECTED_RESPONSES,
  REGISTRATION_REJECTED_ERRORS, SCAN_REJECTED_ERRORS,
  SCAN_REJECTED_RESPONSES,
  capturePaidOperation, capturePhotoSelection, captureRegistrationSubmission,
  captureScanSubmission, classifyMutationResult, completionModeOf, createAttemptFence,
  createGenerationTracker,
  createPaidOperationLedger, createReadingFollowerRegistry,
  isIrradiationMutationSuccess,
  isReadingMutationInProgress, isReadingMutationSuccess,
  isRegistrationMutationSuccess, isRejectedResponse, isReportedCount,
  isScanMutationSuccess, paidOperationKey, photoSelectionApplies,
  progressPercent, readingBinding, readingBindingApplies,
  readingConfirmRecalcApplies, readingConfirmSubmitEnabled,
  readingReplayPrecedence, retireRequestKey,
  scanViewForReading, submissionScreenAllowed,
  takeVerifiedStashedReadingOutcome, verifyTerminalReadingPoll,
} from '../src/app-lifecycle.js';

test('generation tracker invalidates older captures on begin', () => {
  const tracker = createGenerationTracker();
  const first = tracker.begin();
  assert.equal(tracker.isCurrent(first), true);
  const second = tracker.begin();
  assert.equal(tracker.isCurrent(first), false);
  assert.equal(tracker.isCurrent(second), true);
  assert.equal(tracker.isCurrent(undefined), false);
  assert.equal(tracker.isCurrent('1'), false);
});

test('scan submission snapshot is frozen and immune to later draft edits', () => {
  const draft = { targetId: 'target-A', targetName: 'Alice', intention: 'heal' };
  const snapshot = captureScanSubmission(draft, { affirmation: 'aff', requestKey: 'key-1' });
  draft.targetId = 'target-B';
  draft.targetName = 'Bob';
  draft.intention = 'changed';
  assert.equal(snapshot.targetId, 'target-A');
  assert.equal(snapshot.targetName, 'Alice');
  assert.equal(snapshot.intention, 'heal');
  assert.equal(snapshot.affirmation, 'aff');
  assert.equal(snapshot.requestKey, 'key-1');
  assert.throws(() => { snapshot.targetName = 'X'; }, TypeError);
  assert.equal(captureScanSubmission(null), null);
});

test('photo selection applies only for its own generation and visit', () => {
  const file = { name: 'A.jpg' };
  const selection = capturePhotoSelection(file, 3, 7);
  assert.equal(selection.fileName, 'A.jpg');
  // B selected after A: A's late result must not apply.
  assert.equal(photoSelectionApplies(selection, { generation: 4, visit: 7 }), false);
  // A new registration visit also invalidates it.
  assert.equal(photoSelectionApplies(selection, { generation: 3, visit: 8 }), false);
  assert.equal(photoSelectionApplies(selection, { generation: 3, visit: 7 }), true);
  assert.equal(capturePhotoSelection(null, 1, 1), null);
});

test('progress percent requires a reported completed count', () => {
  assert.equal(progressPercent(null, 33), null);
  assert.equal(progressPercent(undefined, 33), null);
  assert.equal(progressPercent(0, 33), 0);
  assert.equal(progressPercent(11, 33), 33);
  assert.equal(progressPercent(40, 33), 100);
  assert.equal(progressPercent(3, null), null);
  assert.equal(progressPercent(3, 0), null);
  assert.equal(isReportedCount(0), true);
  assert.equal(isReportedCount(null), false);
  assert.equal(isReportedCount(undefined), false);
});

test('reading binding applies only for the same scan and visit', () => {
  const binding = readingBinding('scan-A', 2);
  assert.equal(readingBindingApplies(binding, { scanId: 'scan-A', generation: 2 }), true);
  assert.equal(readingBindingApplies(binding, { scanId: 'scan-B', generation: 2 }), false);
  assert.equal(readingBindingApplies(binding, { scanId: 'scan-A', generation: 3 }), false);
  assert.equal(readingBinding('', 2), null);
  assert.equal(readingBindingApplies(null, { scanId: 'scan-A', generation: 2 }), false);
});

test('post-release recalc: a confirmation re-entered under a NEW binding is re-enabled (round 8)', () => {
  // The round-8 defect sequence, at the pure-decision level:
  // the recovery captured binding gen 1; while it held the
  // guard, the user re-entered the SAME scan's confirmation
  // under binding gen 2, whose button rendered disabled (guard
  // held). The old finally required equality with the CAPTURED
  // binding and skipped the re-render. The recalc decision is
  // taken from the CURRENT confirmation and CURRENT binding:
  // the two bindings deliberately differ here.
  const capturedByRecovery = readingBinding('scan-A', 1);
  const liveBinding = readingBinding('scan-A', 2);
  assert.equal(readingBindingApplies(capturedByRecovery, liveBinding), false);
  const confirmation = {
    confirmation_id: 'conf-A', scan_id: 'scan-A',
    quote: { credits: 36 }, balance_after: 120,
  };
  assert.equal(readingConfirmRecalcApplies({
    screen: 'reading-confirm', confirmation, prepared: true, liveBinding,
  }), true);
  // After the guard release nothing is in flight, so the
  // recomputation enables the submit button in place.
  assert.equal(readingConfirmSubmitEnabled({
    confirmation, prepared: true, inFlightActive: false,
  }), true);
  // While the guard was still held (the state the new screen
  // rendered under), the same confirmation was NOT enabled —
  // the recalc, not the entry render, is what frees it.
  assert.equal(readingConfirmSubmitEnabled({
    confirmation, prepared: true, inFlightActive: true,
  }), false);
});

test('post-release recalc: an unaffordable confirmation is never enabled (round 8)', () => {
  const liveBinding = readingBinding('scan-A', 2);
  const confirmation = {
    confirmation_id: 'conf-A', scan_id: 'scan-A',
    quote: { credits: 36 }, balance_after: -4,
  };
  // The recalc itself applies (the re-render must run so the
  // shortage state is what shows), but eligibility stays off.
  assert.equal(readingConfirmRecalcApplies({
    screen: 'reading-confirm', confirmation, prepared: true, liveBinding,
  }), true);
  assert.equal(readingConfirmSubmitEnabled({
    confirmation, prepared: true, inFlightActive: false,
  }), false);
  // Exactly-affordable (balance_after 0) and unreported
  // balances are not shortages, mirroring the renderer.
  assert.equal(readingConfirmSubmitEnabled({
    confirmation: { ...confirmation, balance_after: 0 },
    prepared: true, inFlightActive: false,
  }), true);
  assert.equal(readingConfirmSubmitEnabled({
    confirmation: { ...confirmation, balance_after: null },
    prepared: true, inFlightActive: false,
  }), true);
});

test('post-release recalc: unprepared, mismatched or placeholder state never enables (round 8)', () => {
  const liveBinding = readingBinding('scan-A', 2);
  const confirmation = {
    confirmation_id: 'conf-A', scan_id: 'scan-A',
    quote: { credits: 36 }, balance_after: 120,
  };
  // Not prepared yet (the new visit's prepare is still in
  // flight): no recalc, no enable.
  assert.equal(readingConfirmRecalcApplies({
    screen: 'reading-confirm', confirmation, prepared: false, liveBinding,
  }), false);
  assert.equal(readingConfirmSubmitEnabled({
    confirmation, prepared: false, inFlightActive: false,
  }), false);
  // A different scan's confirmation under this binding: the
  // release of scan A's attempt must not touch scan B's screen.
  assert.equal(readingConfirmRecalcApplies({
    screen: 'reading-confirm',
    confirmation: { ...confirmation, scan_id: 'scan-B' },
    prepared: true, liveBinding,
  }), false);
  // Not the confirmation screen at all, and no live binding.
  assert.equal(readingConfirmRecalcApplies({
    screen: 'reading-wait', confirmation, prepared: true, liveBinding,
  }), false);
  assert.equal(readingConfirmRecalcApplies({
    screen: 'reading-confirm', confirmation, prepared: true, liveBinding: null,
  }), false);
  assert.equal(readingConfirmRecalcApplies({
    screen: 'reading-confirm', confirmation: null, prepared: true, liveBinding,
  }), false);
  // The recovery's synthetic placeholder confirmation (no
  // quote) can never be re-rendered as a confirmation.
  assert.equal(readingConfirmRecalcApplies({
    screen: 'reading-confirm',
    confirmation: { confirmation_id: 'conf-A', scan_id: 'scan-A' },
    prepared: true, liveBinding,
  }), false);
  assert.equal(readingConfirmSubmitEnabled({
    confirmation: null, prepared: true, inFlightActive: false,
  }), false);
});

test('scan view for a reading resolves by the reading own scan_id', () => {
  const reading = { scan_id: 'scan-A' };
  assert.equal(scanViewForReading(reading, { job_id: 'scan-A' })?.job_id, 'scan-A');
  assert.equal(scanViewForReading(reading, { job_id: 'scan-B' }), null);
  assert.equal(scanViewForReading(reading, null), null);
  assert.equal(scanViewForReading({}, { job_id: 'scan-A' }), null);
});

test('registration submission freezes payload and key together (R5)', () => {
  const input = { name: 'Alice', photoBase64: 'photo-A' };
  const record = captureRegistrationSubmission(input, 'key-reg-1');
  input.name = 'Bob';
  input.photoBase64 = 'photo-B';
  // Later edits never leak into the frozen attempt: recovery
  // replays Alice/photo-A under key-reg-1, never a mix.
  assert.equal(record.name, 'Alice');
  assert.equal(record.photoBase64, 'photo-A');
  assert.equal(record.requestKey, 'key-reg-1');
  assert.throws(() => { record.name = 'Bob'; }, TypeError);
  assert.equal(captureRegistrationSubmission(null, 'k'), null);
});

test('paid operation records freeze confirmation, key and scan together (R2)', () => {
  const record = capturePaidOperation({
    operation: 'reading', confirmationId: 'conf-1', requestKey: 'key-9', scanId: 'scan-A',
  });
  assert.deepEqual({ ...record }, {
    operation: 'reading', confirmationId: 'conf-1', requestKey: 'key-9', scanId: 'scan-A',
    completionMode: 'wait',
  });
  assert.throws(() => { record.requestKey = 'other'; }, TypeError);
  // An incomplete record is no record: recovery must never run
  // without the exact key/confirmation pair.
  assert.equal(capturePaidOperation({ operation: 'reading', confirmationId: 'c' }), null);
  assert.equal(capturePaidOperation({ operation: 'irradiation', requestKey: 'k' }), null);
  assert.equal(capturePaidOperation(), null);
  const noScan = capturePaidOperation({
    operation: 'irradiation', confirmationId: 'c', requestKey: 'k',
  });
  assert.equal(noScan.scanId, null);
});

test('paid operation records freeze their own count and quote when known (N3)', () => {
  const record = capturePaidOperation({
    operation: 'irradiation', confirmationId: 'conf-33', requestKey: 'key-33',
    scanId: 'scan-A', count: 33, quoteCredits: 129,
  });
  assert.equal(record.count, 33);
  assert.equal(record.quoteCredits, 129);
  assert.throws(() => { record.count = 66; }, TypeError);
  // Without a known count/quote the fields stay absent, so a
  // recovery view can tell "unknown" from a real number.
  const bare = capturePaidOperation({
    operation: 'reading', confirmationId: 'c', requestKey: 'k', scanId: 'scan-A',
  });
  assert.equal('count' in bare, false);
  assert.equal('quoteCredits' in bare, false);
});

test('paid operation ledger keeps one record per (operation, scan) (S2)', () => {
  const ledger = createPaidOperationLedger('session-1');
  const a = capturePaidOperation({
    operation: 'irradiation', confirmationId: 'conf-A', requestKey: 'key-A', scanId: 'scan-A',
  });
  const b = capturePaidOperation({
    operation: 'irradiation', confirmationId: 'conf-B', requestKey: 'key-B', scanId: 'scan-B',
  });
  const readingA = capturePaidOperation({
    operation: 'reading', confirmationId: 'conf-RA', requestKey: 'key-RA', scanId: 'scan-A',
  });
  ledger.put(a);
  // Submitting scan B must NOT overwrite scan A's unresolved
  // record (the round-2 reproduction).
  ledger.put(b);
  ledger.put(readingA);
  assert.equal(ledger.get('irradiation', 'scan-A'), a);
  assert.equal(ledger.get('irradiation', 'scan-B'), b);
  assert.equal(ledger.get('reading', 'scan-A'), readingA);
  assert.equal(ledger.get('reading', 'scan-B'), null);
  assert.equal(ledger.size(), 3);
  // Re-putting the same (operation, scan) returns the ORIGINAL
  // record: a re-entry never replaces the frozen key.
  const impostor = capturePaidOperation({
    operation: 'irradiation', confirmationId: 'conf-A2', requestKey: 'key-A2', scanId: 'scan-A',
  });
  assert.equal(ledger.put(impostor), a);
  assert.equal(ledger.get('irradiation', 'scan-A').requestKey, 'key-A');
  assert.equal(ledger.put(null), null);
  assert.equal(paidOperationKey('irradiation', 'scan-A'), 'irradiation:scan-A');
  assert.equal(paidOperationKey('irradiation', null), 'irradiation:');
});

test('paid operation ledger finds another scan unresolved record to block on (S2)', () => {
  const ledger = createPaidOperationLedger('session-1');
  const a = capturePaidOperation({
    operation: 'reading', confirmationId: 'conf-A', requestKey: 'key-A', scanId: 'scan-A',
  });
  ledger.put(a);
  assert.equal(ledger.firstUnresolvedExcept('reading', 'scan-B'), a);
  assert.equal(ledger.firstUnresolvedExcept('reading', 'scan-A'), null);
  assert.equal(ledger.firstUnresolvedExcept('irradiation', 'scan-B'), null);
});

test('paid operation ledger settles by identity even when the screen moved on (S2)', () => {
  const ledger = createPaidOperationLedger('session-1');
  const a = capturePaidOperation({
    operation: 'irradiation', confirmationId: 'conf-A', requestKey: 'key-A', scanId: 'scan-A',
  });
  ledger.put(a);
  // A different record object for the same key settles nothing.
  const other = capturePaidOperation({
    operation: 'irradiation', confirmationId: 'conf-A', requestKey: 'key-A', scanId: 'scan-A',
  });
  assert.equal(ledger.settle(other), false);
  assert.equal(ledger.get('irradiation', 'scan-A'), a);
  // The matching record settles unconditionally — this is the
  // release a definitive answer performs even after navigation
  // retired the screen continuation.
  assert.equal(ledger.settle(a), true);
  assert.equal(ledger.get('irradiation', 'scan-A'), null);
  assert.equal(ledger.settle(a), false);
  assert.equal(ledger.settle(null), false);
});

test('paid operation ledger is scoped to its authenticated session (S2)', () => {
  const ledger = createPaidOperationLedger('session-1');
  const a = capturePaidOperation({
    operation: 'reading', confirmationId: 'conf-A', requestKey: 'key-A', scanId: 'scan-A',
  });
  ledger.put(a);
  ledger.setScope('session-1');
  assert.equal(ledger.get('reading', 'scan-A'), a);
  // A different session drops every record: nothing leaks
  // across accounts.
  ledger.setScope('session-2');
  assert.equal(ledger.get('reading', 'scan-A'), null);
  assert.equal(ledger.size(), 0);
  ledger.put(a);
  ledger.setScope(null);
  assert.equal(ledger.size(), 0);
  assert.equal(ledger.scope(), null);
});

test('retireRequestKey rotates only the retired attempt key (S3)', () => {
  let minted = 0;
  const mint = () => `fresh-${(minted += 1)}`;
  // The attempt definitively ended while its key is still the
  // live one: the key retires with the frozen record, so an
  // edited resubmission receives a fresh key.
  assert.equal(retireRequestKey('key-1', 'key-1', mint), 'fresh-1');
  // A newer visit already minted its own key: it is kept.
  assert.equal(retireRequestKey('key-2', 'key-1', mint), 'key-2');
  assert.equal(minted, 1);
  // No retired key, no rotation.
  assert.equal(retireRequestKey('key-1', null, mint), 'key-1');
  assert.equal(retireRequestKey('key-1', '', mint), 'key-1');
});

test('mutation classification: validated 2xx success shapes are success (round 3, T1)', () => {
  const reading = { isSuccess: isReadingMutationSuccess, rejectedErrors: READING_REJECTED_ERRORS };
  const irradiation = { isSuccess: isIrradiationMutationSuccess, rejectedErrors: IRRADIATION_REJECTED_ERRORS };
  const registration = { isSuccess: isRegistrationMutationSuccess, rejectedErrors: REGISTRATION_REJECTED_ERRORS };
  const scan = { isSuccess: isScanMutationSuccess, rejectedErrors: SCAN_REJECTED_ERRORS };
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 200, data: { reading_id: 'r-1', scan_id: 's-1', state: 'completed' } }, reading), 'success');
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 200, data: { job_id: 'j-1', state: 'queued' } }, irradiation), 'success');
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 200, data: { target_id: 't-1' } }, registration), 'success');
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 201, data: { target: { id: 't-2' } } }, registration), 'success');
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 200, data: { job_id: 'j-2', state: 'queued' } }, scan), 'success');
});

test('mutation classification: 4xx with a known business code is rejected (round 3, T1)', () => {
  const reading = { isSuccess: isReadingMutationSuccess, rejectedErrors: READING_REJECTED_ERRORS };
  const irradiation = { isSuccess: isIrradiationMutationSuccess, rejectedErrors: IRRADIATION_REJECTED_ERRORS };
  const registration = { isSuccess: isRegistrationMutationSuccess, rejectedErrors: REGISTRATION_REJECTED_ERRORS };
  const scan = { isSuccess: isScanMutationSuccess, rejectedErrors: SCAN_REJECTED_ERRORS };
  // quote_changed is the shared definitive rejection: the
  // server explicitly did NOT execute, for both paid types.
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 400, data: { error: 'quote_changed' } }, reading), 'rejected');
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 400, data: { error: 'quote_changed' } }, irradiation), 'rejected');
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 400, data: { error: 'confirmation_already_used' } }, irradiation), 'rejected');
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 400, data: { error: 'reading_daily_cap' } }, reading), 'rejected');
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 400, data: { error: 'target_rejected' } }, registration), 'rejected');
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 400, data: { error: 'scan_rejected' } }, scan), 'rejected');
  // The typed scan intake codes (batch 2b item 7): the server
  // answers them at HTTP 400 before creating or recording any
  // job, so each is a definitive rejection for scans — and
  // stays ambiguous for every other operation.
  for (const code of ['scan_quota_exhausted', 'scan_budget_exhausted', 'scan_unavailable']) {
    assert.equal(classifyMutationResult(
      { kind: 'ok', status: 400, data: { error: code } }, scan), 'rejected');
    assert.equal(classifyMutationResult(
      { kind: 'ok', status: 400, data: { error: code } }, reading), 'ambiguous');
  }
  // A code known for one endpoint is NOT a rejection for
  // another: the sets are per-operation.
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 400, data: { error: 'reading_daily_cap' } }, irradiation), 'ambiguous');
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 400, data: { error: 'target_rejected' } }, reading), 'ambiguous');
});

test('mutation classification: unknown codes, 5xx and transport failures are ambiguous (round 3, T1)', () => {
  const reading = { isSuccess: isReadingMutationSuccess, rejectedErrors: READING_REJECTED_ERRORS };
  const registration = { isSuccess: isRegistrationMutationSuccess, rejectedErrors: REGISTRATION_REJECTED_ERRORS };
  // 400 with an UNRECOGNIZED code: not a definitive rejection.
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 400, data: { error: 'some_future_code' } }, reading), 'ambiguous');
  // The round-3 reproduction: HTTP 500 with a generic JSON
  // error body is kind 'ok' from the API client, but the
  // outcome is unknown — records and keys must be kept.
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 500, data: { error: 'internal_server_error' } }, reading), 'ambiguous');
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 500, data: { error: 'internal_server_error' } }, registration), 'ambiguous');
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 503, data: { error: 'quote_changed' } }, reading), 'ambiguous');
  // Transport / parse failure never classifies as definitive.
  assert.equal(classifyMutationResult({ kind: 'error' }, reading), 'ambiguous');
  assert.equal(classifyMutationResult({ kind: 'error', status: 500 }, registration), 'ambiguous');
  assert.equal(classifyMutationResult(null, reading), 'ambiguous');
  assert.equal(classifyMutationResult(undefined, reading), 'ambiguous');
});

test('mutation classification: misshapen or error-carrying 2xx is ambiguous (round 3, T1)', () => {
  const reading = { isSuccess: isReadingMutationSuccess, rejectedErrors: READING_REJECTED_ERRORS };
  const irradiation = { isSuccess: isIrradiationMutationSuccess, rejectedErrors: IRRADIATION_REJECTED_ERRORS };
  const registration = { isSuccess: isRegistrationMutationSuccess, rejectedErrors: REGISTRATION_REJECTED_ERRORS };
  // 2xx without the operation's success identifier.
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 200, data: { scan_id: 's-1' } }, reading), 'ambiguous');
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 200, data: { state: 'queued' } }, irradiation), 'ambiguous');
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 200, data: {} }, registration), 'ambiguous');
  // 2xx carrying an error field is not a success shape.
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 200, data: { reading_id: 'r-1', error: 'quote_changed' } }, reading), 'ambiguous');
  // Missing status or body cannot prove anything either.
  assert.equal(classifyMutationResult(
    { kind: 'ok', data: { reading_id: 'r-1' } }, reading), 'ambiguous');
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 200, data: null }, reading), 'ambiguous');
});

test('reading success requires the completed state (round 4, N1)', () => {
  // The server's replay of a still-generating reading returns
  // its reading_view: an id and a non-terminal state. Only the
  // completed view is a terminal success.
  assert.equal(isReadingMutationSuccess(
    { reading_id: 'r-1', state: 'completed', text: '本文' }), true);
  assert.equal(isReadingMutationSuccess(
    { reading_id: 'r-1', state: 'reserved', text: null }), false);
  assert.equal(isReadingMutationSuccess(
    { reading_id: 'r-1', state: 'failed', text: null }), false);
  // No state at all cannot prove completion either.
  assert.equal(isReadingMutationSuccess({ reading_id: 'r-1' }), false);
  assert.equal(isReadingMutationSuccess(
    { reading_id: 'r-1', state: 'completed', error: 'x' }), false);
  assert.equal(isReadingMutationSuccess(null), false);
});

test('reading in-progress predicate matches only non-terminal views (round 4, N1)', () => {
  assert.equal(isReadingMutationInProgress(
    { reading_id: 'r-1', state: 'reserved', text: null, text_expired: false }), true);
  assert.equal(isReadingMutationInProgress(
    { reading_id: 'r-1', state: 'completed', text: '本文' }), false);
  assert.equal(isReadingMutationInProgress(
    { reading_id: 'r-1', state: 'failed', text: null }), false);
  // A missing state is not evidence of in-progress work.
  assert.equal(isReadingMutationInProgress({ reading_id: 'r-1' }), false);
  assert.equal(isReadingMutationInProgress({ state: 'reserved' }), false);
  assert.equal(isReadingMutationInProgress(
    { reading_id: 'r-1', state: 'reserved', error: 'x' }), false);
  assert.equal(isReadingMutationInProgress(null), false);
});

test('mutation classification: an unfinished reading replay is accepted, not success (round 4, N1)', () => {
  const reading = {
    isSuccess: isReadingMutationSuccess,
    isAccepted: isReadingMutationInProgress,
    rejectedErrors: READING_REJECTED_ERRORS,
    rejectedResponses: READING_REJECTED_RESPONSES,
  };
  // The round-4 reproduction: ambiguous failure, then a replay
  // answering 200 with the generating reading's view.
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 200, data: { reading_id: 'r-1', scan_id: 's-1', state: 'reserved', text: null, text_expired: false } },
    reading), 'accepted');
  // The completed replay of the same key is the terminal success.
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 200, data: { reading_id: 'r-1', scan_id: 's-1', state: 'completed', text: '本文', text_expired: false } },
    reading), 'success');
  // A failed-state view is neither success nor accepted: the
  // server answers failures with an error body, so a bare
  // failed view proves nothing and stays ambiguous.
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 200, data: { reading_id: 'r-1', state: 'failed', text: null } },
    reading), 'ambiguous');
  // Without the isAccepted predicate wired, an unfinished view
  // is ambiguous — never silently a success.
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 200, data: { reading_id: 'r-1', state: 'reserved', text: null } },
    { isSuccess: isReadingMutationSuccess, rejectedErrors: READING_REJECTED_ERRORS }), 'ambiguous');
});

test('mutation classification: documented missing-resource pairs are rejected (round 4, N2)', () => {
  const scan = {
    isSuccess: isScanMutationSuccess, rejectedErrors: SCAN_REJECTED_ERRORS,
    rejectedResponses: SCAN_REJECTED_RESPONSES,
  };
  const irradiation = {
    isSuccess: isIrradiationMutationSuccess, rejectedErrors: IRRADIATION_REJECTED_ERRORS,
    rejectedResponses: IRRADIATION_REJECTED_RESPONSES,
  };
  const reading = {
    isSuccess: isReadingMutationSuccess, isAccepted: isReadingMutationInProgress,
    rejectedErrors: READING_REJECTED_ERRORS, rejectedResponses: READING_REJECTED_RESPONSES,
  };
  // The three documented pairs from app_api.py.
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 404, data: { error: 'Target not found' } }, scan), 'rejected');
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 404, data: { error: 'Scan not found' } }, irradiation), 'rejected');
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 404, data: { error: 'Scan not found' } }, reading), 'rejected');
  // The pair is (status, error) TOGETHER: the same free text at
  // another status is not a documented rejection.
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 400, data: { error: 'Target not found' } }, scan), 'ambiguous');
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 400, data: { error: 'Scan not found' } }, irradiation), 'ambiguous');
  // A 404 with any other text stays ambiguous — free text is
  // never blanket-classified.
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 404, data: { error: 'Reading not found' } }, reading), 'ambiguous');
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 404, data: { error: 'Job not found' } }, irradiation), 'ambiguous');
  // Pairs are per-operation: the scan pair does not reject a
  // reading, and vice versa.
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 404, data: { error: 'Target not found' } }, reading), 'ambiguous');
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 404, data: { error: 'Scan not found' } }, scan), 'ambiguous');
  // The 409 request-key conflict stays conservative.
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 409, data: { error: 'request_key already used' } }, irradiation), 'ambiguous');
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 409, data: { error: 'request_key already used' } }, scan), 'ambiguous');
});

test('isRejectedResponse matches only the exact documented pair (round 4, N2)', () => {
  assert.equal(isRejectedResponse(
    { kind: 'ok', status: 404, data: { error: 'Target not found' } }, SCAN_REJECTED_RESPONSES), true);
  assert.equal(isRejectedResponse(
    { kind: 'ok', status: 404, data: { error: 'Scan not found' } }, IRRADIATION_REJECTED_RESPONSES), true);
  assert.equal(isRejectedResponse(
    { kind: 'ok', status: 404, data: { error: 'Scan not found' } }, READING_REJECTED_RESPONSES), true);
  assert.equal(isRejectedResponse(
    { kind: 'ok', status: 400, data: { error: 'Target not found' } }, SCAN_REJECTED_RESPONSES), false);
  assert.equal(isRejectedResponse(
    { kind: 'ok', status: 404, data: { error: 'Target not found' } }, READING_REJECTED_RESPONSES), false);
  assert.equal(isRejectedResponse(
    { kind: 'ok', status: 404, data: {} }, SCAN_REJECTED_RESPONSES), false);
  assert.equal(isRejectedResponse(
    { kind: 'error', status: 404, data: { error: 'Target not found' } }, SCAN_REJECTED_RESPONSES), false);
  assert.equal(isRejectedResponse(null, SCAN_REJECTED_RESPONSES), false);
  assert.equal(isRejectedResponse(
    { kind: 'ok', status: 404, data: { error: 'Target not found' } }, null), false);
});

test('poll verification accepts only the matching terminal view on 2xx (round 5, N4)', () => {
  const ids = { readingId: 'reading-A', scanId: 'scan-A' };
  const completed = {
    kind: 'ok', status: 200,
    data: { reading_id: 'reading-A', scan_id: 'scan-A', state: 'completed', text: '…' },
  };
  assert.equal(verifyTerminalReadingPoll(completed, ids), completed.data);
  const failed = {
    kind: 'ok', status: 200,
    data: { reading_id: 'reading-A', scan_id: 'scan-A', state: 'failed', error_code: 'reading_unavailable' },
  };
  assert.equal(verifyTerminalReadingPoll(failed, ids), failed.data);
});

test('poll verification proves nothing for non-2xx, mismatched or incomplete answers (round 5, N4)', () => {
  const ids = { readingId: 'reading-A', scanId: 'scan-A' };
  const view = { reading_id: 'reading-A', scan_id: 'scan-A', state: 'completed' };
  // A completed view riding an HTTP 500 proves nothing.
  assert.equal(verifyTerminalReadingPoll({ kind: 'ok', status: 500, data: view }, ids), null);
  // A failed view riding an HTTP 503 proves nothing.
  assert.equal(verifyTerminalReadingPoll(
    { kind: 'ok', status: 503, data: { ...view, state: 'failed' } }, ids), null);
  // A bare state with no identity is not a settle basis.
  assert.equal(verifyTerminalReadingPoll(
    { kind: 'ok', status: 200, data: { state: 'completed' } }, ids), null);
  // Another reading's completed view must not settle this record.
  assert.equal(verifyTerminalReadingPoll(
    { kind: 'ok', status: 200, data: { ...view, reading_id: 'reading-B' } }, ids), null);
  // A view for another scan must not settle this record.
  assert.equal(verifyTerminalReadingPoll(
    { kind: 'ok', status: 200, data: { ...view, scan_id: 'scan-B' } }, ids), null);
  // Non-terminal states keep the follow-up going.
  assert.equal(verifyTerminalReadingPoll(
    { kind: 'ok', status: 200, data: { ...view, state: 'reserved' } }, ids), null);
  // Transport failure, login routing and error-carrying bodies prove nothing.
  assert.equal(verifyTerminalReadingPoll({ kind: 'error' }, ids), null);
  assert.equal(verifyTerminalReadingPoll({ kind: 'login' }, ids), null);
  assert.equal(verifyTerminalReadingPoll(
    { kind: 'ok', status: 200, data: { ...view, error: 'internal' } }, ids), null);
  assert.equal(verifyTerminalReadingPoll(null, ids), null);
  assert.equal(verifyTerminalReadingPoll({ kind: 'ok', status: 200, data: null }, ids), null);
  assert.equal(verifyTerminalReadingPoll({ kind: 'ok', data: view }, ids), null);
});

test('follower registry: only the latest registration is current (round 5, N3)', () => {
  const registry = createReadingFollowerRegistry();
  const record = capturePaidOperation({
    operation: 'reading', confirmationId: 'conf-A', requestKey: 'key-A', scanId: 'scan-A',
  });
  const otherScan = capturePaidOperation({
    operation: 'reading', confirmationId: 'conf-B', requestKey: 'key-B', scanId: 'scan-B',
  });
  const otherOperation = capturePaidOperation({
    operation: 'irradiation', confirmationId: 'conf-C', requestKey: 'key-C', scanId: 'scan-A',
  });
  const first = registry.register(record);
  assert.equal(registry.isCurrent(record, first), true);
  const second = registry.register(record);
  assert.equal(registry.isCurrent(record, first), false);
  assert.equal(registry.isCurrent(record, second), true);
  // Slots are independent per (operation, scan): another
  // record's registration never makes this one current.
  assert.equal(registry.isCurrent(otherScan, second), false);
  assert.equal(registry.isCurrent(otherOperation, second), false);
  // Clearing on settle drops the registration entirely.
  registry.clear(record);
  assert.equal(registry.isCurrent(record, second), false);
});

test('follower registry: a stashed outcome survives re-registration and is consumed once (round 5, N3)', () => {
  const registry = createReadingFollowerRegistry();
  const record = capturePaidOperation({
    operation: 'reading', confirmationId: 'conf-A', requestKey: 'key-A', scanId: 'scan-A',
  });
  registry.register(record);
  const outcome = { kind: 'ok', status: 200, data: { reading_id: 'reading-A' } };
  assert.equal(registry.stashOutcome(record, outcome), true);
  // A newer follower of the SAME record registering must not
  // lose the pending outcome.
  registry.register(record);
  assert.equal(registry.takeOutcome(record), outcome);
  assert.equal(registry.takeOutcome(record), null);
  // Nothing to stash into once the record was cleared (settled).
  registry.clear(record);
  assert.equal(registry.stashOutcome(record, outcome), false);
  assert.equal(registry.takeOutcome(record), null);
});

test('overlapping followers: the superseded one stashes, the current one settles (round 5, N3)', () => {
  const ledger = createPaidOperationLedger();
  const record = capturePaidOperation({
    operation: 'reading', confirmationId: 'conf-A', requestKey: 'key-A', scanId: 'scan-A',
  });
  ledger.put(record);
  const registry = createReadingFollowerRegistry();
  const ids = { readingId: 'reading-A', scanId: 'scan-A' };
  const oldFollower = registry.register(record);
  const newFollower = registry.register(record);
  // The old follower's in-flight poll resolves with a verified
  // terminal view AFTER it was superseded: it must NOT settle.
  const poll = {
    kind: 'ok', status: 200,
    data: { reading_id: 'reading-A', scan_id: 'scan-A', state: 'completed' },
  };
  const view = verifyTerminalReadingPoll(poll, ids);
  assert.ok(view);
  assert.equal(registry.isCurrent(record, oldFollower), false);
  registry.stashOutcome(record, poll);
  assert.equal(ledger.get('reading', 'scan-A'), record);
  // The current follower consumes the stash, re-verifies it and
  // only then settles — exactly once, by identity.
  const stashed = registry.takeOutcome(record);
  assert.equal(verifyTerminalReadingPoll(stashed, ids), view);
  assert.equal(registry.isCurrent(record, newFollower), true);
  assert.equal(ledger.settle(record), true);
  registry.clear(record);
  assert.equal(ledger.size(), 0);
});

test('replay pre-registration: the old follower stashes and the record survives (round 6, N3 residual)', () => {
  const ledger = createPaidOperationLedger();
  const record = capturePaidOperation({
    operation: 'reading', confirmationId: 'conf-A', requestKey: 'key-A', scanId: 'scan-A',
  });
  ledger.put(record);
  const registry = createReadingFollowerRegistry();
  const ids = { readingId: 'reading-A', scanId: 'scan-A' };
  // Follower of the original attempt, poll in flight.
  const oldFollower = registry.register(record);
  // The replay submission pre-registers its successor
  // generation BEFORE its POST is awaited (app.js wiring):
  // from here the old follower is no longer current.
  const successor = registry.register(record);
  assert.equal(registry.isCurrent(record, oldFollower), false);
  assert.equal(registry.isCurrent(record, successor), true);
  // The old follower's held poll now resolves completed. It
  // verifies the view, sees it is superseded and STASHES —
  // the record must NOT settle (the round-5 defect settled
  // here because no successor existed yet at this moment).
  const poll = {
    kind: 'ok', status: 200,
    data: { reading_id: 'reading-A', scan_id: 'scan-A', state: 'completed' },
  };
  assert.ok(verifyTerminalReadingPoll(poll, ids));
  registry.stashOutcome(record, poll);
  assert.equal(ledger.get('reading', 'scan-A'), record);
  assert.equal(ledger.size(), 1);
  assert.equal(registry.size(), 1);
});

test('pre-registered successor consumes the stashed outcome and settles (round 6, N3 residual)', () => {
  const ledger = createPaidOperationLedger();
  const record = capturePaidOperation({
    operation: 'reading', confirmationId: 'conf-A', requestKey: 'key-A', scanId: 'scan-A',
  });
  ledger.put(record);
  const registry = createReadingFollowerRegistry();
  registry.register(record); // old follower
  const successor = registry.register(record); // replay pre-registration
  const poll = {
    kind: 'ok', status: 200,
    data: { reading_id: 'reading-A', scan_id: 'scan-A', state: 'failed' },
  };
  registry.stashOutcome(record, poll);
  // The successor follower (carrying the pre-registered token,
  // still current) consumes the stash: verified view back,
  // consumed exactly once, then settle + clear empties both
  // the ledger and the registry — nothing is left following.
  assert.equal(registry.isCurrent(record, successor), true);
  const view = takeVerifiedStashedReadingOutcome(registry, { record, readingId: 'reading-A' });
  assert.equal(view, poll.data);
  assert.equal(takeVerifiedStashedReadingOutcome(registry, { record, readingId: 'reading-A' }), null);
  assert.equal(ledger.settle(record), true);
  registry.clear(record);
  assert.equal(ledger.size(), 0);
  assert.equal(registry.size(), 0);
  // An unverifiable stash (another reading's view) proves
  // nothing and is dropped, not settled on.
  const registry2 = createReadingFollowerRegistry();
  registry2.register(record);
  registry2.stashOutcome(record, {
    kind: 'ok', status: 200,
    data: { reading_id: 'reading-B', scan_id: 'scan-A', state: 'completed' },
  });
  assert.equal(takeVerifiedStashedReadingOutcome(registry2, { record, readingId: 'reading-A' }), null);
  assert.equal(registry2.takeOutcome(record), null);
});

test('stashed terminal outcome takes precedence over accepted and ambiguous replay answers (round 6, N3 residual)', () => {
  const registry = createReadingFollowerRegistry();
  const record = capturePaidOperation({
    operation: 'reading', confirmationId: 'conf-A', requestKey: 'key-A', scanId: 'scan-A',
  });
  registry.register(record);
  registry.register(record); // replay pre-registration
  registry.stashOutcome(record, {
    kind: 'ok', status: 200,
    data: { reading_id: 'reading-A', scan_id: 'scan-A', state: 'completed' },
  });
  // Ambiguous replay answer: no reading id is known, so the
  // stash is verified against its OWN reading id — ownership
  // is already proven by the record binding, and the verified
  // terminal outcome still wins over the recovery branch, so
  // no recovery is offered for a settled record.
  const stashedView = takeVerifiedStashedReadingOutcome(registry, { record });
  assert.equal(stashedView.state, 'completed');
  assert.equal(
    readingReplayPrecedence({ outcome: 'ambiguous', stashedView }), 'stashed-terminal');
  assert.equal(
    readingReplayPrecedence({ outcome: 'accepted', stashedView }), 'stashed-terminal');
  // Without a verified stash the branches stand as classified.
  assert.equal(
    readingReplayPrecedence({ outcome: 'ambiguous', stashedView: null }), 'ambiguous');
  assert.equal(
    readingReplayPrecedence({ outcome: 'accepted', stashedView: null }), 'accepted');
  // The replay POST's own terminal answers keep precedence:
  // they settle directly and never consult the stash branch.
  assert.equal(
    readingReplayPrecedence({ outcome: 'success', stashedView }), 'success');
  assert.equal(
    readingReplayPrecedence({ outcome: 'rejected', stashedView }), 'rejected');
  assert.equal(readingReplayPrecedence({}), null);
});

test('record binding: a settled reading A cannot settle the next reading B of the same scan (round 7)', () => {
  const ledger = createPaidOperationLedger();
  const registry = createReadingFollowerRegistry();
  const recordA = capturePaidOperation({
    operation: 'reading', confirmationId: 'conf-A', requestKey: 'key-A', scanId: 'scan-A',
  });
  const recordB = capturePaidOperation({
    operation: 'reading', confirmationId: 'conf-B', requestKey: 'key-B', scanId: 'scan-A',
  });
  assert.notEqual(recordA.requestKey, recordB.requestKey);
  // ① A's follower registers; its GET is held in flight.
  ledger.put(recordA);
  const followerA = registry.register(recordA);
  // ② A settles through a terminal replay and the slot is
  // cleaned, exactly as the submission's terminal path does.
  assert.equal(ledger.settle(recordA), true);
  registry.clear(recordA);
  // ③ B is submitted for the SAME scan under a fresh key;
  // its follower registers and B owns the slot now.
  ledger.put(recordB);
  const followerB = registry.register(recordB);
  assert.equal(registry.isCurrent(recordB, followerB), true);
  assert.equal(registry.isCurrent(recordA, followerA), false);
  // ④ A's held GET finally resolves with A's verified
  // terminal view. The follower-side discipline (verified
  // view + superseded) would stash it — but the slot belongs
  // to B's record, so the stash is REFUSED.
  const pollA = {
    kind: 'ok', status: 200,
    data: { reading_id: 'reading-A', scan_id: 'scan-A', state: 'completed' },
  };
  assert.ok(verifyTerminalReadingPoll(pollA, { readingId: 'reading-A', scanId: 'scan-A' }));
  assert.equal(registry.stashOutcome(recordA, pollA), false);
  // ⑤ B re-enters and its replay answers ambiguously. The
  // consumption consults ONLY B's own record: A's outcome is
  // invisible, nothing settles, and B's record and follower
  // registration both survive for B's own follow-up.
  assert.equal(takeVerifiedStashedReadingOutcome(registry, { record: recordB }), null);
  assert.equal(registry.takeOutcome(recordB), null);
  assert.equal(ledger.get('reading', 'scan-A'), recordB);
  assert.equal(ledger.size(), 1);
  assert.equal(registry.isCurrent(recordB, followerB), true);
  // And a clear carrying A's record cannot evict B's slot.
  assert.equal(registry.clear(recordA), false);
  assert.equal(registry.size(), 1);
  assert.equal(registry.isCurrent(recordB, followerB), true);
});

test('record binding: a different record taking the slot drops the previous stash (round 7)', () => {
  const registry = createReadingFollowerRegistry();
  const recordA = capturePaidOperation({
    operation: 'reading', confirmationId: 'conf-A', requestKey: 'key-A', scanId: 'scan-A',
  });
  const recordB = capturePaidOperation({
    operation: 'reading', confirmationId: 'conf-B', requestKey: 'key-B', scanId: 'scan-A',
  });
  registry.register(recordA);
  const pollA = {
    kind: 'ok', status: 200,
    data: { reading_id: 'reading-A', scan_id: 'scan-A', state: 'failed' },
  };
  assert.equal(registry.stashOutcome(recordA, pollA), true);
  // B registering for the same (operation, scan) rebinds the
  // slot: A's pending outcome is dropped, not inherited, and
  // neither record can take the other's outcome afterwards.
  registry.register(recordB);
  assert.equal(registry.takeOutcome(recordB), null);
  assert.equal(registry.takeOutcome(recordA), null);
  assert.equal(
    takeVerifiedStashedReadingOutcome(registry, { record: recordB }), null);
  // A stash placed under B is B's alone: A cannot consume it
  // even though the scan matches.
  const pollB = {
    kind: 'ok', status: 200,
    data: { reading_id: 'reading-B', scan_id: 'scan-A', state: 'completed' },
  };
  assert.equal(registry.stashOutcome(recordB, pollB), true);
  assert.equal(registry.takeOutcome(recordA), null);
  assert.equal(registry.takeOutcome(recordB), pollB);
});

test('attempt fence: exactly one side claims the attempt (round 7)', () => {
  const fence = createAttemptFence();
  assert.equal(fence.isClaimed(), false);
  // The work settling first claims the attempt; the deadline
  // arriving afterwards claims nothing and applies nothing.
  assert.equal(fence.claim(), true);
  assert.equal(fence.isClaimed(), true);
  assert.equal(fence.claim(), false);
  // Symmetrically, a deadline that fires first fences out
  // the real response whenever it eventually lands.
  const deadlineFirst = createAttemptFence();
  assert.equal(deadlineFirst.claim(), true);
  assert.equal(deadlineFirst.claim(), false);
  assert.equal(deadlineFirst.isClaimed(), true);
});

test('a timed-out reading POST classifies ambiguous and reaches the stash first (round 7)', () => {
  // The client deadline resolves a stalled POST as a
  // transport failure carrying the timeout marker; the
  // mutation classifier must treat it exactly like a lost
  // response — ambiguous — so the submission keeps its
  // record and flows into the stash-first consumption
  // instead of settling or failing outright.
  const outcome = classifyMutationResult(
    { kind: 'error', timeout: true },
    {
      isSuccess: isReadingMutationSuccess,
      isAccepted: isReadingMutationInProgress,
      rejectedErrors: READING_REJECTED_ERRORS,
      rejectedResponses: READING_REJECTED_RESPONSES,
    });
  assert.equal(outcome, 'ambiguous');
  // With a verified terminal outcome stashed under the SAME
  // record while the POST was stalled, the precedence helper
  // routes the timed-out (ambiguous) submission to the
  // stashed terminal view — the consumption that releases
  // the record and, through the caller's finally, the
  // in-flight guard.
  const registry = createReadingFollowerRegistry();
  const record = capturePaidOperation({
    operation: 'reading', confirmationId: 'conf-A', requestKey: 'key-A', scanId: 'scan-A',
  });
  registry.register(record); // the replay's pre-registration
  registry.stashOutcome(record, {
    kind: 'ok', status: 200,
    data: { reading_id: 'reading-A', scan_id: 'scan-A', state: 'completed' },
  });
  const stashedView = takeVerifiedStashedReadingOutcome(registry, { record });
  assert.equal(stashedView.state, 'completed');
  assert.equal(
    readingReplayPrecedence({ outcome, stashedView }), 'stashed-terminal');
});

test('submission screen permission needs navigation AND binding ownership (S1)', () => {
  assert.equal(submissionScreenAllowed({ navCurrent: true, bindingCurrent: true }), true);
  assert.equal(submissionScreenAllowed({ navCurrent: false, bindingCurrent: true }), false);
  assert.equal(submissionScreenAllowed({ navCurrent: true, bindingCurrent: false }), false);
  assert.equal(submissionScreenAllowed({ navCurrent: false, bindingCurrent: false }), false);
  assert.equal(submissionScreenAllowed({}), false);
  assert.equal(submissionScreenAllowed(), false);
});

test('completion mode: only the exact "notify" asks for mail (foundation item 2)', () => {
  assert.equal(completionModeOf('notify'), 'notify');
  assert.equal(completionModeOf('wait'), 'wait');
  assert.equal(completionModeOf('NOTIFY'), 'wait');
  assert.equal(completionModeOf(''), 'wait');
  assert.equal(completionModeOf(null), 'wait');
  assert.equal(completionModeOf(undefined), 'wait');
  assert.equal(completionModeOf({ mode: 'notify' }), 'wait');
});

test('the completion choice is frozen into the scan snapshot and the paid record', () => {
  const draft = { targetId: 'target-A', targetName: 'Alice', intention: 'heal' };
  const notified = captureScanSubmission(draft, {
    affirmation: 'aff', requestKey: 'key-1', completionMode: 'notify',
  });
  assert.equal(notified.completionMode, 'notify');
  // No choice presented (older callers) records "wait", the
  // server's own absent-field default.
  const waited = captureScanSubmission(draft, { affirmation: 'aff', requestKey: 'key-2' });
  assert.equal(waited.completionMode, 'wait');
  const record = capturePaidOperation({
    operation: 'irradiation', confirmationId: 'conf-1', requestKey: 'key-3',
    scanId: 'scan-A', completionMode: 'notify',
  });
  assert.equal(record.completionMode, 'notify');
  assert.throws(() => { record.completionMode = 'wait'; }, TypeError);
});

test('mutation classification: notify_unavailable is a definitive rejection for scan and irradiation', () => {
  const irradiation = { isSuccess: isIrradiationMutationSuccess, rejectedErrors: IRRADIATION_REJECTED_ERRORS };
  const scan = { isSuccess: isScanMutationSuccess, rejectedErrors: SCAN_REJECTED_ERRORS };
  const reading = { isSuccess: isReadingMutationSuccess, rejectedErrors: READING_REJECTED_ERRORS };
  // Foundation item 2: the fresh notify submission is refused
  // before anything is consumed, so the answer is definitive
  // for the two job submissions — and meaningless (ambiguous)
  // for readings, which carry no completion choice.
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 400, data: { error: 'notify_unavailable' } }, scan), 'rejected');
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 400, data: { error: 'notify_unavailable' } }, irradiation), 'rejected');
  assert.equal(classifyMutationResult(
    { kind: 'ok', status: 400, data: { error: 'notify_unavailable' } }, reading), 'ambiguous');
});
