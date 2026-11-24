// Resume-link helpers for the standalone app (foundation
// item 2: completion mail + resume link, 2026-11-23).
//
// The completion mail carries {base}/app#resume=<token>. The
// token is a LOCATOR, not a credential: redemption
// (POST /api/app/resume) rides the user's own authenticated
// session and answers only { job_id, operation }. The job's
// data itself is then loaded through the ordinary job view,
// so nothing about the job is ever taken from the link.
//
// Two moments matter:
//  - Arrival: the token is read from the fragment ONCE, at
//    app start, and stashed in session storage. The stash is
//    what lets the landing survive the login round-trip (the
//    sign-in flow navigates away to the identity provider and
//    back; session storage is the same-tab memory that
//    survives it — the same discipline as the purchase return
//    in app-purchase.js). A fragment carrying an access_token
//    is NOT stripped here: that fragment belongs to the
//    state-bound handoff (account-session.js), which strips
//    it itself; this module only ever strips a fragment it
//    fully owns (a resume parameter and no access_token).
//  - Landing: after the session is established and home has
//    been reached, the stash is consumed exactly once and the
//    token redeemed. Where the user lands follows the JOB'S
//    OWN state as the server reports it — a completed scan
//    opens its ranking, a still-running one opens its wait
//    screen, and an irradiation opens its proof or progress
//    screen. Nothing is inferred from the link itself.
//
// Everything here is pure / storage-shaped so the rules are
// testable without a DOM. Storage failures degrade to "no
// resume pending" — the mail link is a convenience, never a
// gate on using the app.

export const RESUME_KEY = 'aiqs-si.resume';

// A link-shaped value: a non-empty string of at most 1024
// characters. The SERVER is the authority on validity (its
// own shape is 16..256 characters, app_api.py jobs_resume) —
// a truncated or otherwise malformed token is still stashed
// and redeemed, so the server's refusal lands the user on
// the "could not open this link" screen instead of silently
// stranding them on home. Only values that cannot have come
// from a mail link at all are dropped here.
function validResumeToken(value) {
  return typeof value === 'string' && value.length >= 1 && value.length <= 1024;
}

// Reads the resume token out of a URL fragment ("#resume=.."),
// or null when the fragment carries none (or only a malformed
// one). Other fragment parameters are not this module's
// business and are ignored.
export function parseResumeToken(hash) {
  if (typeof hash !== 'string' || !hash.startsWith('#')) return null;
  let params;
  try { params = new URLSearchParams(hash.slice(1)); } catch { return null; }
  const values = params.getAll('resume');
  if (values.length !== 1) return null;
  return validResumeToken(values[0]) ? values[0] : null;
}

// Whether a fragment is this module's to strip from the URL:
// it carries a resume parameter and NO access_token (a
// fragment with an access_token belongs to the state-bound
// handoff, which strips it on its own path).
export function resumeFragmentOwned(hash) {
  if (typeof hash !== 'string' || !hash.startsWith('#')) return false;
  let params;
  try { params = new URLSearchParams(hash.slice(1)); } catch { return false; }
  return params.getAll('resume').length > 0
    && params.getAll('access_token').length === 0;
}

export function saveResumeToken(storage, token) {
  if (!validResumeToken(token) || !storage) return false;
  try {
    storage.setItem(RESUME_KEY, token);
    return true;
  } catch {
    return false;
  }
}

// Reads AND removes the stashed token: a landing is consumed
// exactly once, so a later plain visit is never hijacked by a
// stale mail link. A malformed stash reads as null.
export function takeResumeToken(storage) {
  if (!storage) return null;
  let raw = null;
  try {
    raw = storage.getItem(RESUME_KEY);
    storage.removeItem(RESUME_KEY);
  } catch {
    return null;
  }
  return validResumeToken(raw) ? raw : null;
}

// The states the job poller treats as still in flight
// (app.js pollJob): everything else is terminal.
const IN_FLIGHT_STATES = new Set(['queued', 'running', 'unknown']);

// Where a redeemed resume lands, from the redeemed operation
// and the job view's own reported state:
//  - scan: completed → its ranking; any other recognized
//    state → its wait screen (whose polling surfaces a
//    terminal failure through the ordinary path when one is
//    already recorded).
//    A completed scan whose content the 24-hour expiry already
//    removed lands on 'expired' instead (batch 2d, MEDIUM 8):
//    the ranking is gone, and rendering the empty ranking
//    would present a success screen with no result in it.
//    The resume link lives 7 days, the content 24 hours, so
//    this is the COMMON case for a mail opened late.
//  - irradiation: terminal → its proof; in flight → its
//    progress screen.
// An operation or state the client does not recognize earns
// no landing (null) — the caller answers with the generic
// "could not open" screen instead of guessing.
const TERMINAL_STATES = new Set(['completed', 'failed', 'cancelled']);

export function resumeLandingFor({ operation, state, contentRemoved } = {}) {
  if (operation === 'scan') {
    if (state === 'completed') return contentRemoved ? 'expired' : 'ranking';
    if (IN_FLIGHT_STATES.has(state) || TERMINAL_STATES.has(state)) {
      return 'scan-wait';
    }
    return null;
  }
  if (operation === 'irradiation') {
    if (IN_FLIGHT_STATES.has(state)) return 'progress';
    return TERMINAL_STATES.has(state) ? 'proof' : null;
  }
  return null;
}
