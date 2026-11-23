// Browser client for the standalone app API (/api/app/*).
// The access token comes from an injected provider and is sent only
// as a Bearer header to this same origin; nothing is persisted here.
// Results mirror the account client contract: { kind: 'login' } when
// there is no usable token (none supplied, or the server answered
// 401), { kind: 'error' } when the call itself failed, and
// { kind: 'ok', status, data } with the server's response dictionary
// — including its fixed "error" code on 400s, which the app maps to
// its template screens. The one exception is the target list:
// targets() passes requireSuccess, because a 503/429/500 JSON body
// there is an admission/maintenance failure, not data — treating it
// as ok made the app render a failed fetch as "no targets".
import { createAttemptFence } from './app-lifecycle.js';

// The deadline for a reading submission, response body included
// (delta review round 7; rationale CORRECTED in round 8 — an
// earlier version of this comment claimed the endpoint reserves
// and answers while generation runs asynchronously, which is
// wrong). The 20s is a limit on how long the CLIENT waits for
// the response, NOT a limit on server processing: on a first
// submission the server (app_api.py readings_create) reserves
// the reading and then AWAITS generation before answering, so
// a legitimate first generation can take longer than 20s, and
// server-side generation may continue after the client has
// stopped waiting. Cutting the wait at 20s is nevertheless
// harmless because of the server's request_key idempotency
// (verified in app_api.py): a same-key retry is answered from
// the stored record for that key — the completed view if it
// finished, the failed outcome if it failed, or the
// still-generating view if it is reserved — and never charges
// or generates twice, so the retry rejoins the SAME reading
// and the client converges through its existing ambiguous /
// same-key recovery / follower / stash reconciliation. The
// bound exists because an UNBOUNDED replay POST could hang
// forever while a superseded follower's verified terminal
// outcome sat stashed with nobody left running to consume it:
// the submission's continuation was the only consumer, and it
// never arrived.
// A call that passes its deadline resolves as
// { kind: 'error', timeout: true } — the same transport-failure
// shape as a lost response — so the submission flows into its
// existing ambiguous handling, which consumes the stash FIRST
// and releases the in-flight guard from its finally block.
export const READING_SUBMISSION_TIMEOUT_MS = 20000;

export function createAppClient({ fetch, getToken, readingTimeoutMs = READING_SUBMISSION_TIMEOUT_MS }) {
  if (typeof fetch !== 'function' || typeof getToken !== 'function') {
    throw new Error('App client unavailable');
  }
  async function call(path, options = {}) {
    const token = await getToken();
    if (!token) return { kind: 'login' };
    const timeoutMs = typeof options.timeoutMs === 'number' && options.timeoutMs > 0
      ? options.timeoutMs : null;
    const controller = timeoutMs && typeof AbortController === 'function'
      ? new AbortController() : null;
    // The whole request — fetch AND the response-body read —
    // runs as one unit of work, so the deadline below bounds
    // both, never just the headers.
    const work = (async () => {
      let response;
      try {
        response = await fetch(path, {
          method: options.method || 'GET',
          headers: {
            authorization: 'Bearer ' + token,
            ...(options.body ? { 'content-type': 'application/json' } : {}),
          },
          body: options.body ? JSON.stringify(options.body) : undefined,
          ...(controller ? { signal: controller.signal } : {}),
        });
      } catch {
        return { kind: 'error' };
      }
      if (response.status === 401) return { kind: 'login' };
      let data = null;
      try { data = await response.json(); } catch { data = null; }
      if (!data || typeof data !== 'object') return { kind: 'error' };
      if (options.requireSuccess && (response.status < 200 || response.status >= 300)) {
        return { kind: 'error', status: response.status };
      }
      return { kind: 'ok', status: response.status, data };
    })();
    if (!timeoutMs) return work;
    // First-wins fence (delta review round 7): whichever of
    // the work and the deadline claims the attempt first IS
    // the call's result. When the deadline wins, the request
    // is aborted where the platform supports it, and the real
    // response — whenever it eventually lands — reaches only
    // the dropped branch below: its claim fails and it applies
    // nothing, so no late continuation can settle a record,
    // retire a key, paint a screen or offer recovery.
    return new Promise((resolve) => {
      const fence = createAttemptFence();
      const timer = setTimeout(() => {
        if (!fence.claim()) return;
        controller?.abort();
        resolve({ kind: 'error', timeout: true });
      }, timeoutMs);
      work.then(
        (value) => {
          if (!fence.claim()) return;
          clearTimeout(timer);
          resolve(value);
        },
        () => {
          if (!fence.claim()) return;
          clearTimeout(timer);
          resolve({ kind: 'error' });
        },
      );
    });
  }
  return Object.freeze({
    session: () => call('/api/app/session'),
    targets: () => call('/api/app/targets', { requireSuccess: true }),
    registerTarget: (body) => call('/api/app/targets', { method: 'POST', body }),
    renameTarget: (targetId, name) => call('/api/app/targets/rename',
      { method: 'POST', body: { target_id: targetId, name } }),
    deleteTarget: (targetId) => call('/api/app/targets/delete',
      { method: 'POST', body: { target_id: targetId } }),
    scan: (body) => call('/api/app/jobs/scan', { method: 'POST', body }),
    prepareIrradiation: (scanId, count) => call('/api/app/jobs/irradiate/prepare',
      { method: 'POST', body: { scan_id: scanId, count } }),
    // The completion choice (foundation item 2) rides along
    // only when the caller passes one: the frozen operation
    // record always does, so every attempt of an operation —
    // first submission and same-key recovery alike — presents
    // the same choice the server recorded first.
    irradiate: (confirmationId, requestKey, completionMode) => call('/api/app/jobs/irradiate',
      {
        method: 'POST',
        body: {
          confirmation_id: confirmationId, request_key: requestKey,
          ...(completionMode ? { completion: { mode: completionMode } } : {}),
        },
      }),
    // Redeem a completion-mail resume token (foundation
    // item 2): the token is a locator, not a credential — the
    // call rides the caller's own session and answers only
    // which job to open ({ job_id, operation }).
    resume: (token) => call('/api/app/resume',
      { method: 'POST', body: { token } }),
    prepareReading: (scanId) => call(
      '/api/app/jobs/' + encodeURIComponent(scanId) + '/reading/prepare',
      { method: 'POST', body: {} }),
    createReading: (confirmationId, requestKey) => call('/api/app/readings',
      {
        method: 'POST',
        body: { confirmation_id: confirmationId, request_key: requestKey },
        timeoutMs: readingTimeoutMs,
      }),
    reading: (readingId) => call('/api/app/readings/' + encodeURIComponent(readingId)),
    job: (jobId) => call('/api/app/jobs/' + encodeURIComponent(jobId)),
    cancelJob: (jobId) => call('/api/app/jobs/' + encodeURIComponent(jobId) + '/cancel',
      { method: 'POST', body: {} }),
    history: () => call('/api/app/history'),
    referral: () => call('/api/app/referral'),
    redeemReferral: (code) => call('/api/app/referral/redeem',
      { method: 'POST', body: { code } }),
    markReferralGiftsSeen: (ids) => call('/api/app/referral/gifts/seen',
      { method: 'POST', body: { ids } }),
  });
}
