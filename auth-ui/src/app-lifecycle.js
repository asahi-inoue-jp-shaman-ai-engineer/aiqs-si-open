// Lifecycle guards for the standalone app (Astra 2026-11-23
// review, findings #2–#5, #7). The races all share one shape: an
// async operation captures nothing about WHEN or FOR WHAT it
// started, so a late response applies itself to a newer visit,
// a newer selection, or a newer flow. These pure helpers give
// each operation an immutable binding — a generation number, a
// scan id, a selection snapshot — and an explicit check before
// any result is applied. Pure state only, no DOM, so every
// guard is unit-testable without a harness.

// A monotonic generation counter. begin() starts a new
// generation (invalidating every older one); a continuation may
// apply its result only while its captured generation is still
// current. Used for navigation (#5), reading visits (#2), scan
// submissions (#4) and photo selections (#3).
export function createGenerationTracker() {
  let generation = 0;
  return Object.freeze({
    begin() {
      generation += 1;
      return generation;
    },
    current() {
      return generation;
    },
    isCurrent(captured) {
      return typeof captured === 'number' && captured === generation;
    },
  });
}

// The completion choice a submission carries (foundation
// item 2): "notify" is the only value that asks for mail —
// anything else normalizes to "wait", the historical behaviour
// the server also assumes when the field is absent. The choice
// is part of the frozen operation bindings below, never a live
// preference: a same-key replay must present the SAME choice
// the first attempt presented, because the server records the
// first one and acts on it.
export function completionModeOf(value) {
  return value === 'notify' ? 'notify' : 'wait';
}

// The immutable snapshot a scan submission is made from (#4):
// everything the request sends and everything its response's
// metadata is labelled with comes from this copy, never from
// the live draft the user may already have edited for the next
// flow. Frozen so nothing can mutate it after capture.
export function captureScanSubmission(draft, { affirmation, requestKey, completionMode } = {}) {
  if (!draft) return null;
  return Object.freeze({
    targetId: draft.targetId ?? null,
    targetName: draft.targetName ?? '',
    intention: draft.intention ?? '',
    affirmation: affirmation ?? '',
    requestKey: requestKey ?? null,
    completionMode: completionModeOf(completionMode),
  });
}

// A photo selection in flight (#3): the file and its display
// name are captured BEFORE the async normalization starts, and
// the result applies only while both the selection generation
// and the registration visit that started it are still current.
export function capturePhotoSelection(file, generation, visit) {
  if (!file) return null;
  return Object.freeze({
    file,
    fileName: typeof file.name === 'string' ? file.name : '',
    generation,
    visit,
  });
}

export function photoSelectionApplies(selection, { generation, visit } = {}) {
  return Boolean(selection)
    && selection.generation === generation
    && selection.visit === visit;
}

// A determinate progress percentage exists only when BOTH the
// completed and the requested counts were actually reported
// (#7): a missing completed count is "unreported", not zero,
// and no percentage may be derived from it.
export function progressPercent(done, total) {
  if (done === null || done === undefined) return null;
  if (total === null || total === undefined || !(total > 0)) return null;
  return Math.min(100, Math.round((done / total) * 100));
}

// Whether a count value was reported at all. Callers keep the
// "—" rendering for unreported counts through progress, proof
// and copy instead of coercing them to 0 (#7).
export function isReportedCount(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

// The binding a reading visit runs under (#2, #10): the scan it
// belongs to plus the visit generation. Preparation, submission,
// retries and results all carry this binding; a step applies
// only while the live binding still equals it.
export function readingBinding(scanId, generation) {
  if (!scanId || typeof generation !== 'number') return null;
  return Object.freeze({ scanId, generation });
}

export function readingBindingApplies(binding, live) {
  return Boolean(binding) && Boolean(live)
    && binding.scanId === live.scanId
    && binding.generation === live.generation;
}

// Whether the reading confirmation's submit action is enabled
// for a PREPARED confirmation (delta review round 8): the
// confirmation must exist and be prepared, no reading submission
// may be in flight, and the quote must be affordable — a
// balance_after that is a negative number means the quote left
// the account short. A missing/unreported balance_after is not
// evidence of a shortage (the renderer shows it as
// unconfirmable), mirroring the renderer's original inline
// check exactly. This is the single eligibility computation the
// confirmation renderer uses, so a post-release recomputation
// can never drift from the render-time one.
export function readingConfirmSubmitEnabled({ confirmation, prepared, inFlightActive } = {}) {
  if (!confirmation || prepared !== true) return false;
  if (inFlightActive) return false;
  const insufficient = typeof confirmation.balance_after === 'number'
    && confirmation.balance_after < 0;
  return !insufficient;
}

// Whether a completed submission attempt's guard release must
// recompute the CURRENT confirmation's eligibility (delta
// review round 8). The defect: a same-key recovery held the
// guard while the user re-entered the confirmation screen under
// a NEW visit binding; the new screen rendered its submit
// button disabled (the guard was still held), and when the old
// recovery then returned early — before consuming the follower
// stash — its finally released the guard but skipped the
// re-render because the CURRENT binding no longer equalled the
// binding the recovery had captured. The recomputation therefore
// deliberately does NOT require the releasing attempt's binding
// to match: it applies to whatever confirmation is CURRENTLY
// prepared, judged against the CURRENT live binding. The guards
// that remain are the ones that protect correctness: the
// current screen must be the reading confirmation, a
// confirmation must be prepared, it must carry a real quote
// (a recovery's synthetic placeholder confirmation has none and
// can never be rendered as a confirmation), and its scan must
// be the live binding's scan — a different scan's confirmation
// is never re-rendered by another scan's release.
export function readingConfirmRecalcApplies({ screen, confirmation, prepared, liveBinding } = {}) {
  return screen === 'reading-confirm'
    && Boolean(confirmation)
    && prepared === true
    && Boolean(liveBinding)
    && typeof confirmation.scan_id === 'string'
    && confirmation.scan_id === liveBinding.scanId
    && typeof confirmation.quote?.credits === 'number';
}

// The immutable registration submission (delta review R5):
// name, photo and request key are frozen TOGETHER at the first
// send. While that attempt's outcome is unknown (its response
// was lost), recovery replays exactly this record — edited
// inputs must never ride the same key with a different payload,
// and they become a separate operation only once this attempt's
// outcome is known. Mirrors captureScanSubmission (#4).
export function captureRegistrationSubmission(input, requestKey) {
  if (!input) return null;
  return Object.freeze({
    name: input.name ?? '',
    photoBase64: input.photoBase64 ?? '',
    requestKey: requestKey ?? null,
  });
}

// The immutable record of a PAID operation in flight (delta
// review R2): confirmation id, request key and scan, frozen at
// submission. A transport failure leaves the outcome UNKNOWN —
// the server may already have charged and executed — so
// recovery reconciles/replays with this same key until a
// terminal answer (success or an explicit server error) is
// known. Only then is the record released; a retry after an
// explicit terminal failure is a separate operation with a
// fresh key, minted by the normal entry paths.
export function capturePaidOperation({
  operation, confirmationId, requestKey, scanId, count, quoteCredits,
  completionMode,
} = {}) {
  if (!operation || !confirmationId || !requestKey) return null;
  const record = {
    operation, confirmationId, requestKey, scanId: scanId ?? null,
    // The wait/notify choice is frozen with the operation
    // (foundation item 2): recovery replays present the same
    // choice, matching the server's first-record-wins intent.
    completionMode: completionModeOf(completionMode),
  };
  // The operation's own count/quote (delta review round 2, N3):
  // a recovery view and the progress total must describe THIS
  // operation, never a newer confirmation the user prepared
  // afterwards, so they are frozen onto the record when known.
  if (typeof count === 'number') record.count = count;
  if (typeof quoteCredits === 'number') record.quoteCredits = quoteCredits;
  return Object.freeze(record);
}

// The ledger of unresolved paid operations (delta review
// round 2, S2): records are kept per (operation, scan) — NOT
// one per operation — so an unresolved attempt for scan A is
// never overwritten when scan B is submitted. The ledger is
// scoped to one authenticated session: setScope with a new
// value drops every record, so nothing leaks across accounts.
// settle() is identity-based and unconditional on screen
// ownership: a definitive server answer releases its record
// even when the screen continuation that carried it is stale
// (reconciliation is separate from permission to paint).
export function paidOperationKey(operation, scanId) {
  return `${operation ?? ''}:${scanId ?? ''}`;
}

export function createPaidOperationLedger(initialScope = null) {
  let scope = initialScope ?? null;
  const records = new Map();
  return Object.freeze({
    setScope(next) {
      const value = next ?? null;
      if (value !== scope) {
        scope = value;
        records.clear();
      }
    },
    scope() {
      return scope;
    },
    put(record) {
      if (!record) return null;
      const key = paidOperationKey(record.operation, record.scanId);
      const existing = records.get(key);
      if (existing) return existing;
      records.set(key, record);
      return record;
    },
    get(operation, scanId) {
      return records.get(paidOperationKey(operation, scanId)) ?? null;
    },
    // Another scan's unresolved record for the same operation:
    // the caller must reconcile (or block on) it before a new
    // paid operation of that type may start.
    firstUnresolvedExcept(operation, scanId) {
      for (const record of records.values()) {
        if (record.operation === operation
            && record.scanId !== (scanId ?? null)) return record;
      }
      return null;
    },
    settle(record) {
      if (!record) return false;
      const key = paidOperationKey(record.operation, record.scanId);
      if (records.get(key) !== record) return false;
      records.delete(key);
      return true;
    },
    size() {
      return records.size;
    },
  });
}

// Retiring a request key (delta review round 2, S3): once an
// attempt has DEFINITIVELY ended, its key must never carry a
// different (edited) payload. The current key is rotated only
// while it still IS the retired attempt's key — a newer visit
// that already minted its own key keeps it.
export function retireRequestKey(currentKey, retiredKey, mintKey) {
  if (retiredKey && currentKey === retiredKey && typeof mintKey === 'function') {
    return mintKey();
  }
  return currentKey;
}

// The definitive-outcome classification for MUTATION results
// (delta review round 3, T1: the shared S2/S3 defect). The API
// client returns kind 'ok' for ANY JSON body, HTTP 500
// included, so 'ok' alone never proves an attempt ended. A
// mutation attempt is over — its frozen record may be released
// and its request key retired — only when the answer is:
//   success:   kind 'ok', a 2xx status, no error field, and
//              the operation's own success shape (the
//              identifier its handler acts on);
//   rejected:  kind 'ok', a 4xx status, and an error code that
//              is a KNOWN business rejection for that endpoint
//              (the server explicitly did not execute).
// Everything else is ambiguous — kind 'error', any 5xx, an
// unrecognized error code, a success status with a mismatched
// shape: the server may already have executed, so the record
// and its key are KEPT and recovery reconciles with the same
// key. kind 'login' never reaches this helper: callers route
// it first, and it neither releases records nor retires keys.
//
// Two server-contract gaps closed in delta review round 4
// (T1 follow-up):
//   accepted:  a reading replay can answer 200 with the
//              reading_view of a reading that is still being
//              generated (state neither completed nor failed).
//              That is NOT a terminal success: the record and
//              its key stay, and the caller follows the reading
//              by id until it terminates. Only a COMPLETED
//              reading is a reading success.
//   rejected (missing resource): three endpoints answer a
//              documented free-text 404 — scan submit "Target
//              not found", irradiation and reading submit
//              "Scan not found" — for a resource that does not
//              exist; the server definitively did not execute
//              (irradiation has already released its claim).
//              These are matched as exact (status, error)
//              PAIRS, never by adding the free text to the
//              code sets, so the same text at any other status
//              — and any other 404 text — stays ambiguous.
export const READING_REJECTED_ERRORS = new Set([
  'quote_changed', 'confirmation_expired', 'confirmation_not_found',
  'confirmation_already_used', 'reading_insufficient_balance',
  'reading_daily_cap', 'reading_unavailable', 'reading_invalid_output',
  'scan_expired', 'scan_not_completed', 'scan_has_no_entries',
]);

export const IRRADIATION_REJECTED_ERRORS = new Set([
  'quote_changed', 'confirmation_expired', 'confirmation_not_found',
  'confirmation_already_used', 'invalid_count', 'irradiation_rejected',
  'scan_expired', 'scan_not_completed', 'scan_has_no_entries',
  // Foundation item 2: a fresh notify submission the service
  // cannot honour is refused BEFORE the confirmation is
  // claimed — nothing is consumed and the attempt's intent is
  // discarded, so the refusal is definitive, never ambiguous.
  'notify_unavailable',
]);

export const REGISTRATION_REJECTED_ERRORS = new Set([
  'target_rejected',
]);

export const SCAN_REJECTED_ERRORS = new Set([
  'scan_rejected',
  // Typed intake rejections (batch 2b item 7, ported with the
  // foundation merge): app_api.py jobs_scan maps the service's
  // fixed failure prefixes through scan_rejections.py to these
  // codes and answers HTTP 400 BEFORE any job is created or
  // recorded under the request key — quota exhaustion, the
  // global exposure-budget stop and a missing configuration are
  // definitive rejections with their own user-facing facts, not
  // ambiguous outcomes and not malformed input.
  'scan_quota_exhausted',
  'scan_budget_exhausted',
  'scan_unavailable',
  // Foundation item 2: a fresh notify submission the service
  // cannot honour is refused BEFORE any job is created and the
  // attempt's intent is discarded — a definitive rejection the
  // UI answers by offering the wait choice under the same key.
  'notify_unavailable',
]);

// Documented missing-resource rejections (delta review
// round 4, N2): exact "status:error" pairs, matched only as a
// pair — the free-text error alone, or at another status, is
// NOT a definitive rejection. Sources: app_api.py jobs_scan
// (404 "Target not found"), jobs_irradiate (404 "Scan not
// found", claim already released) and readings_create via
// validated_scan (404 "Scan not found", before reservation).
export const SCAN_REJECTED_RESPONSES = new Set([
  '404:Target not found',
]);

export const IRRADIATION_REJECTED_RESPONSES = new Set([
  '404:Scan not found',
]);

export const READING_REJECTED_RESPONSES = new Set([
  '404:Scan not found',
]);

// Whether a raw mutation result IS one of the documented
// (status, error) rejection pairs in the given set. Callers
// use this to route a classified rejection to its factual
// recovery path (the resource is gone) instead of the generic
// rejection handling for the operation.
export function isRejectedResponse(result, rejectedResponses) {
  if (!result || result.kind !== 'ok') return false;
  if (typeof result.status !== 'number') return false;
  if (typeof result.data?.error !== 'string') return false;
  return Boolean(rejectedResponses?.has(`${result.status}:${result.data.error}`));
}

function hasId(data, field) {
  return Boolean(data) && !data.error
    && typeof data[field] === 'string' && data[field].length > 0;
}

// A reading mutation succeeds TERMINALLY only when the
// reading is completed (delta review round 4, N1): the
// server's request-key replay answers 200 with the existing
// reading_view for a reading that is still being generated,
// and that response carries a reading_id too — treating it as
// success released the ledger record and rendered a result
// (with a false expiry notice) for work that had not finished.
export function isReadingMutationSuccess(data) {
  return hasId(data, 'reading_id') && data.state === 'completed';
}

// A reading response that was ACCEPTED but has not terminated:
// it carries the reading_id and a non-terminal state (the
// server's states are reserved/completed/failed). The caller
// keeps the record and follows this id to its terminal state.
export function isReadingMutationInProgress(data) {
  return hasId(data, 'reading_id')
    && typeof data.state === 'string'
    && data.state !== 'completed' && data.state !== 'failed';
}

export function isIrradiationMutationSuccess(data) {
  return hasId(data, 'job_id');
}

export function isScanMutationSuccess(data) {
  return hasId(data, 'job_id');
}

export function isRegistrationMutationSuccess(data) {
  if (!data || data.error) return false;
  if (typeof data.target_id === 'string' && data.target_id) return true;
  return typeof data.target?.id === 'string' && Boolean(data.target.id);
}

export function classifyMutationResult(result, {
  isSuccess, isAccepted, rejectedErrors, rejectedResponses,
} = {}) {
  if (!result || result.kind !== 'ok') return 'ambiguous';
  const { status, data } = result;
  if (!data || typeof data !== 'object' || typeof status !== 'number') {
    return 'ambiguous';
  }
  if (status >= 200 && status < 300) {
    if (data.error) return 'ambiguous';
    if (typeof isSuccess === 'function' && isSuccess(data)) return 'success';
    // Accepted but not terminal (round 4, N1): the attempt is
    // still running server-side, so neither 'success' (which
    // would settle the record) nor 'ambiguous' (which would
    // park a same-key recovery over live work) is true.
    if (typeof isAccepted === 'function' && isAccepted(data)) return 'accepted';
    return 'ambiguous';
  }
  if (status >= 400 && status < 500) {
    if (typeof data.error !== 'string') return 'ambiguous';
    if (Boolean(rejectedErrors?.has(data.error))) return 'rejected';
    // Documented missing-resource pairs (round 4, N2): the
    // exact (status, error) pair must match — free text is
    // never promoted into the code sets above.
    if (Boolean(rejectedResponses?.has(`${status}:${data.error}`))) return 'rejected';
    return 'ambiguous';
  }
  return 'ambiguous';
}

// Whether a submission continuation may touch the screen
// (delta review round 2, S1): it needs BOTH its operation-
// specific binding AND the navigation generation it started
// under. Reconciliation (settling records, caching results)
// may proceed without it; painting may not.
export function submissionScreenAllowed({ navCurrent, bindingCurrent } = {}) {
  return Boolean(navCurrent) && Boolean(bindingCurrent);
}

// Resolves the scan view a reading's actions (copy, back,
// irradiate) must use (#10): the reading's OWN scan_id decides,
// never whatever scan happens to be parked in state. Returns
// null when the parked view belongs to a different scan, so the
// caller can fetch the right one (or disable the action)
// instead of silently using the wrong context.
export function scanViewForReading(reading, parkedView) {
  if (!reading?.scan_id) return null;
  if (!parkedView || parkedView.job_id !== reading.scan_id) return null;
  return parkedView;
}

// Whether a reading POLL answer proves a terminal outcome
// (delta review round 5, N4). The poll client returns kind
// 'ok' for ANY JSON body, HTTP 500 included, so the answer
// settles the paid record ONLY when it is a 2xx response whose
// body is a complete terminal reading_view OF THE READING
// BEING FOLLOWED: its reading_id equals the polled id and its
// scan_id equals the followed record's scan. Everything else
// — non-2xx, an identity mismatch, a state-only fragment, an
// error-carrying body, a non-terminal state — proves NOTHING:
// the caller keeps ownership and keeps polling. This mirrors
// classifyMutationResult's discipline for the read channel.
// Returns the verified view, or null.
export function verifyTerminalReadingPoll(result, { readingId, scanId } = {}) {
  if (!result || result.kind !== 'ok') return null;
  const { status, data } = result;
  if (typeof status !== 'number' || status < 200 || status >= 300) return null;
  if (!data || typeof data !== 'object' || data.error) return null;
  if (!readingId || data.reading_id !== readingId) return null;
  if (data.scan_id !== (scanId ?? null)) return null;
  if (data.state !== 'completed' && data.state !== 'failed') return null;
  return data;
}

// A first-wins claim for ONE attempt (delta review round 7).
// An attempt whose deadline loses the race must apply NOTHING
// when it eventually finishes — no settlement, no key
// operation, no painting, no recovery prompt. claim() returns
// true exactly once, for whichever side (the work or the
// deadline) reaches it first; every later claim returns false
// and the caller drops its result on the floor. Pure state,
// so the fence itself is unit-testable; the API client's
// timeout race is built on it.
export function createAttemptFence() {
  let claimed = false;
  return Object.freeze({
    claim() {
      if (claimed) return false;
      claimed = true;
      return true;
    },
    isClaimed() {
      return claimed;
    },
  });
}

// Follower coordination for in-progress readings (delta
// review round 5, N3). Re-entering a scan whose reading is
// still being generated starts a SECOND follower for the same
// (operation, scan) identity while the first follower's poll
// may still be in flight. Without coordination the older
// follower settled the shared ledger record when its late
// answer landed and then declined to paint (its own screen
// transition was stale), leaving the newer follower with no
// record to follow and the user on the wait screen forever.
// The registry makes registration generational: only the
// LATEST follower for an identity may settle its record or
// retire its key. A superseded follower that observes a
// verified terminal view stashes it as the identity's pending
// outcome instead of settling, and the current follower
// consumes the stash on its next cycle (re-verifying it
// through verifyTerminalReadingPoll before settling). The
// registry is session-scoped memory, like the ledger: it is
// cleared per identity when that identity settles.
//
// RECORD BINDING (delta review round 7): the slot is still
// keyed by (operation, scan), but every entry is BOUND to the
// one immutable ledger record that registered it, and every
// operation — register, stash, take, clear, isCurrent —
// requires that record. Round 6 keyed the stash by scan
// alone, so after reading A settled and a NEW reading B (a
// different record, a different request_key) was submitted
// for the same scan, A's superseded follower could stash A's
// terminal view into B's slot, and B's ambiguous replay then
// consumed it — verified only against the stash's OWN
// reading_id — and settled B with A's outcome. With record
// binding, B's registration replaces the slot's binding (and
// drops any outcome stashed for A), a stash attempt carrying
// A's record is refused while the slot belongs to B, and B's
// consumption can only ever see outcomes stashed under B's
// own record. The record object itself is the binding: the
// ledger's records are frozen at capture and settle() is
// already identity-based, so reference equality is exact.
export function createReadingFollowerRegistry() {
  const entries = new Map();
  let generation = 0;
  const keyOf = (record) => (record
    ? paidOperationKey(record.operation, record.scanId)
    : null);
  return Object.freeze({
    register(record) {
      if (!record) return 0;
      const key = keyOf(record);
      generation += 1;
      const existing = entries.get(key);
      entries.set(key, {
        generation,
        record,
        // A pending outcome survives re-registration only
        // within the SAME record: it was stashed FOR that
        // record's current follower, whoever that is. A
        // different record taking the slot starts empty —
        // the previous record's outcome is not its to consume.
        outcome: existing && existing.record === record
          ? (existing.outcome ?? null)
          : null,
      });
      return generation;
    },
    isCurrent(record, captured) {
      if (!record) return false;
      const entry = entries.get(keyOf(record));
      return Boolean(entry) && entry.record === record
        && entry.generation === captured;
    },
    stashOutcome(record, outcome) {
      if (!record || !outcome) return false;
      const entry = entries.get(keyOf(record));
      if (!entry || entry.record !== record) return false;
      entry.outcome = outcome;
      return true;
    },
    takeOutcome(record) {
      if (!record) return null;
      const entry = entries.get(keyOf(record));
      if (!entry || entry.record !== record || !entry.outcome) return null;
      const outcome = entry.outcome;
      entry.outcome = null;
      return outcome;
    },
    clear(record) {
      if (!record) return false;
      const key = keyOf(record);
      const entry = entries.get(key);
      if (!entry || entry.record !== record) return false;
      entries.delete(key);
      return true;
    },
    size() {
      return entries.size;
    },
  });
}

// Consume the pending outcome a superseded follower stashed
// for the caller's OWN record and return it ONLY as a
// verified terminal view (delta review round 6, N3 residual;
// record binding delta review round 7). The stash is consumed
// exactly once either way: an unverifiable stash proves
// nothing and is dropped, mirroring the follower loop's
// discipline. Ownership is established by the registry's
// record binding — takeOutcome returns a stash only when it
// was placed under THIS record, by a follower of THIS record
// (which verified it against the reading it was following
// before stashing). That binding REPLACES the round-6
// self-anchor check, under which the stash's own reading_id
// alone vouched for it and a previous record's outcome could
// settle a different reading of the same scan. When the
// caller knows the reading id (an accepted replay answer
// carries one), verification still requires the stash to be
// THAT reading; only when the caller does not know it yet
// (an ambiguous replay answer carries none) does the
// verification fall back to the stashed view's own
// reading_id, now safe because the stash provably belongs to
// this record. The scan identity, 2xx status, terminal state
// and shape are re-checked here regardless.
export function takeVerifiedStashedReadingOutcome(registry, { record, readingId } = {}) {
  if (!registry || !record) return null;
  const stashed = registry.takeOutcome(record);
  if (!stashed) return null;
  const effectiveReadingId = readingId ?? stashed.data?.reading_id ?? null;
  return verifyTerminalReadingPoll(stashed, {
    readingId: effectiveReadingId, scanId: record.scanId,
  });
}

// Which branch a replayed reading submission must take
// (delta review round 6, N3 residual): a VERIFIED terminal
// outcome stashed by the superseded follower takes precedence
// over BOTH non-terminal replay branches — 'accepted' (the
// delayed reserved view would start a follower over a record
// the stash already ends) and 'ambiguous' (the recovery
// prompt would offer a same-key retry for a record that no
// longer exists). Terminal classifications ('success' /
// 'rejected') keep their own precedence: the replay POST's
// own definitive answer settles directly, stash or not.
export function readingReplayPrecedence({ outcome, stashedView } = {}) {
  if (stashedView && (outcome === 'accepted' || outcome === 'ambiguous')) {
    return 'stashed-terminal';
  }
  return outcome ?? null;
}
