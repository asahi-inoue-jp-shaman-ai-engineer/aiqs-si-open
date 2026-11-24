// Browser login for the standalone app: authorization code + PKCE
// against the SI realm, with the dedicated public client "si-app".
// This is the browser-side token client the account pages always
// left as a deployment decision; the app makes it concrete.
//
// Session discipline (revised 2026-11-22, app-pwa-session): the
// access token lives only in this closure's memory. The refresh
// token is persisted in localStorage (persistentStorage) so one
// login survives browser restarts; on load it is silently exchanged
// for a fresh access token, and getToken refreshes proactively
// before expiry. A server-side rejection (invalid grant) clears the
// persisted session; a network error keeps it for the next attempt.
// Refreshes are single-flight so concurrent callers never race the
// same refresh token. The PKCE verifier/state pair for the in-flight
// redirect stays in sessionStorage, removed the moment the redirect
// returns. A token handed over in the URL fragment by the existing
// connection flow (#access_token=...&state=..., the account pages'
// mechanism) is adopted the same memory-only way and never
// persisted — and only when its state matches the handoff nonce
// this browser armed (account-session.js): an unsolicited fragment
// token is ignored, and a successful adoption clears any persisted
// session so a stale refresh token from another account cannot
// resurrect underneath the handed-over session.
//
// Cross-tab discipline (batch 2d, HIGH 1; atomic record in
// batch 2e, R1): the generation fence above is per-instance,
// but the persisted session is shared by every tab — so a
// refresh in flight in tab 1 for account A could still land
// (and re-persist A's rotated token) after tab 2 had adopted
// account B, and the next load resurrected A. The shared
// session record therefore carries its fence INSIDE itself:
// the refresh token, a FRESH RANDOM revision and a session
// lineage id (sid) are written by ONE setItem, so a token and
// the revision it is bound to can never be read from two
// different generations (the split read/write of the earlier
// two-record design), a token request captures the revision
// when it starts and discards its response if the revision
// moved, and a tab whose in-memory session is bound to an
// older revision drops it instead of acting on it —
// fragment-only sessions included: they hold no refresh token,
// but they are bound to the revision their adoption wrote, and
// another tab's later write supersedes them all the same. A
// stale tab also never clears a shared session that is no
// longer its own.
//
// Batch 2e closes the three sequences the two-record design
// still failed (Astra glass round 2, R1):
// - Adoption is a commit: when a response carries a refresh
//   token, the persist must succeed BEFORE the credentials are
//   kept. A failed commit aborts the adoption outright — the
//   tab does not hold a session the shared record disagrees
//   with while another tab keeps dispatching the old one.
// - Disappearance is invalidation: a tab bound to an
//   established record treats a missing or unreadable record
//   as supersession, never as "still current".
// - Loading binds token, revision and lineage from the SAME
//   record in one read; the separate epoch record remains
//   only as the clear tombstone (a clear has no token to
//   carry its revision) and as the legacy fallback for
//   records written before the revision moved inside.
// Revisions are random strings, only ever compared for
// equality (round 2: a numeric epoch dies at 2^53, where +1
// is a no-op and the fence silently stops moving). The sid
// names the session LINEAGE: a fresh login, a fragment
// adoption and a clear each start a new lineage, while a
// refresh rotation keeps the lineage it rotates — the
// account pages bind to the lineage, so a same-account
// rotation never retires them but any replacement does.
//
// Batch 2g (Astra batch2f diff review, R1 app-side variant):
// the account pages' fence was unified onto the verified
// derivation in batch 2f, but THIS module still parsed the
// shared records itself, leniently — a stored 'null' literal
// or '{}' collapsed into "no record", and a session record
// carrying a lineage but no revision inherited the epoch
// record's revision — so a foreign (or corrupted) session
// record left a fragment token dispatchable under the clear
// tombstone its adoption had written, in the very cases the
// verified derivation refuses. The shared state is now read
// ONLY through verifiedSharedSessionSnapshot
// (account-session.js): one derivation, strictly shaped, for
// both surfaces. A present record the protocol never writes
// is unverifiable — never absence — and the supersession
// fence compares BOTH halves of the binding (revision and
// lineage), so a record cannot borrow another record's half.
//
// Round 2 of the same review closed three more sequences:
// - The token request's fence compares against the revision
//   the SENT token is bound to (passed in from the caller's
//   snapshot), never a fresh re-read: a commit landing
//   between the load and the request used to pair the old
//   token with the new session's revision and pass both
//   checks.
// - Fragment adoption is a committed transition: the shared
//   clear must land BEFORE the handed-over credentials are
//   installed, and a failed clear aborts the adoption.
// - Supersession is an exact revision match with no
//   same-owner exemption: an older tombstone of this very
//   instance must not stand in for a session record that has
//   since disappeared.
import {
  ensureFragmentHandoff, takeFragmentToken, verifiedSharedSessionSnapshot,
} from './account-session.js';

const CLIENT_ID = 'si-app';
const PENDING_KEY = 'si-app-pkce-v1';
const SESSION_KEY = 'si-app-session-v1';
const EPOCH_KEY = 'si-app-session-epoch-v1';
const REFRESH_MARGIN_MS = 60000;

function base64url(bytes) {
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function createAppAuth({
  fetch, location, history, storage, persistentStorage = null, crypto,
}) {
  if (typeof fetch !== 'function' || !location || !history || !storage || !crypto?.subtle) {
    throw new Error('App auth unavailable');
  }
  ensureFragmentHandoff(storage);
  const issuer = location.origin + '/realms/si';
  const authorizeUrl = issuer + '/protocol/openid-connect/auth';
  const tokenUrl = issuer + '/protocol/openid-connect/token';
  const redirectUri = location.origin + '/app';
  const resource = location.origin + '/mcp';

  let accessToken = null;
  let refreshToken = null;
  let expiresAt = 0;
  let refreshPromise = null;
  // Authentication generation: bumped whenever the session is
  // replaced outside the token endpoint (fragment adoption, session
  // clearing). A token response that was requested by an older
  // generation is discarded on arrival — otherwise a slow refresh
  // for the PREVIOUS account could land after a handoff and
  // silently resurrect that account (tokens and persisted refresh
  // token alike).
  let generation = 0;
  // This instance's identity in the shared revision record:
  // writes carrying another instance's id are how a tab learns
  // that a DIFFERENT tab rotated, replaced, or cleared the
  // shared session underneath it.
  const instanceId = base64url(crypto.getRandomValues(new Uint8Array(8)));
  // The revision this instance's in-memory session is bound to:
  // the revision its own latest write produced, or the revision
  // current when it loaded the persisted session. Another
  // instance's write moves the shared revision past it.
  let boundRevision = null;
  // The lineage (sid) of this instance's session: preserved
  // across rotations, replaced by every fresh adoption, clear
  // or fragment handoff. The account pages bind to it.
  let boundSid = null;

  function freshRevision() {
    return base64url(crypto.getRandomValues(new Uint8Array(16)));
  }

  // The shared session, read ONLY through the verified
  // derivation (batch 2g, R1): verifiedSharedSessionSnapshot
  // (account-session.js) reads the persisted session record
  // (SESSION_KEY) and the epoch record (EPOCH_KEY) under the
  // same strict shape rules the account pages validate a
  // carry against, so the two surfaces can never classify the
  // same stored bytes differently. The persisted session
  // record is one JSON value holding the refresh token
  // together with the revision and lineage it is bound to;
  // records written before batch 2e hold only {refreshToken,
  // savedAt} and defer their revision to the epoch record —
  // the snapshot applies exactly that deferral, and no other:
  // a record carrying a lineage reports its OWN revision,
  // never the epoch's. A present record the protocol never
  // writes (a stored 'null' literal, '{}', a mistyped field)
  // is UNVERIFIABLE here — not absence.
  function sharedSnapshot() {
    if (!persistentStorage) return { state: 'absent' };
    return verifiedSharedSessionSnapshot(persistentStorage);
  }

  // One consistent snapshot of the shared session: token,
  // revision and lineage from the SAME record. Null when no
  // valid session record exists — confirmed absence,
  // tombstone-only, or unverifiable (a loader simply has
  // nothing it may use; the fence distinguishes the three).
  function readSharedSession() {
    const snap = sharedSnapshot();
    if (snap.state !== 'record' || !snap.refreshToken) return null;
    return { refreshToken: snap.refreshToken, rev: snap.rev, sid: snap.sid };
  }

  // The identity the token-request fence compares against
  // (batch 2g round 2, R1): the WHOLE snapshot — state,
  // revision, lineage AND the refresh token itself — not the
  // revision alone. The revision-only fence was blind to
  // replacements that keep the revision: an unnamed (legacy)
  // record has revision null, and a foreign record carrying a
  // lineage but no revision of its own also reports null, so
  // a response for the old session committed OVER the
  // replacement. An unverifiable state is fingerprinted by
  // the raw stored bytes (read for comparison only, never
  // parsed): a request that started against those exact bytes
  // may still land its own commit over them — a fresh code
  // exchange healing a corrupt record — while ANY change to
  // the bytes, to a record, or to absence fails the fence.
  let unverifiableReadCounter = 0;
  function rawStoredValue(key) {
    try { return persistentStorage.getItem(key); }
    catch { return undefined; }
  }
  function snapshotIdentity(snap) {
    if (snap.state === 'record') {
      return JSON.stringify(['record', snap.rev, snap.sid, snap.refreshToken]);
    }
    if (snap.state === 'absent') return '["absent"]';
    const rawSession = rawStoredValue(SESSION_KEY);
    const rawEpoch = rawStoredValue(EPOCH_KEY);
    if (rawSession === undefined || rawEpoch === undefined) {
      // The store cannot even be read back: no two reads may
      // be presumed equal, so this identity matches nothing,
      // not even itself.
      unverifiableReadCounter += 1;
      return JSON.stringify(['unreadable', unverifiableReadCounter]);
    }
    return JSON.stringify(['unverifiable',
      rawSession === null ? null : String(rawSession),
      rawEpoch === null ? null : String(rawEpoch)]);
  }
  function sharedIdentity() {
    return snapshotIdentity(sharedSnapshot());
  }
  // The identity of the record THIS instance's in-memory
  // session claims: its binding and its refresh token, in the
  // same shape snapshotIdentity reports for a record. A
  // request sent under it may commit only while the shared
  // record still IS that identity.
  function boundIdentity() {
    return JSON.stringify(['record', boundRevision, boundSid, refreshToken]);
  }

  // THE commit (batch 2e, R1): token, revision and lineage in
  // ONE setItem. Returns the new revision, or null when the
  // write failed — callers that establish a session must treat
  // null as an aborted adoption, never as a degraded success.
  function persistRefreshToken(token, sid) {
    if (!persistentStorage || !token) return null;
    const rev = freshRevision();
    let landed = false;
    try {
      persistentStorage.setItem(SESSION_KEY, JSON.stringify({
        refreshToken: token, rev, owner: instanceId, sid,
        savedAt: Date.now(),
      }));
      landed = true;
    } catch { /* storage full/blocked: the commit did not happen */ }
    if (!landed) return null;
    boundRevision = rev;
    boundSid = sid;
    return rev;
  }

  function loadSharedSession() {
    return readSharedSession();
  }

  function loadPersistedRefreshToken() {
    const session = readSharedSession();
    return session ? session.refreshToken : null;
  }

  // Tri-state probe of the session record (batch 2e round 3,
  // R1 HIGH-2): 'present' (anything stored, parseable or not),
  // 'absent' (confirmed: the read returned null), 'unreadable'
  // (the read threw). The snapshot derivation collapses the
  // last two into "no usable record", which is fine for
  // loading but NOT for adoption: an unreadable record may
  // still be there, speaking for the old account in every
  // other tab.
  function probeSessionRecord() {
    if (!persistentStorage) return 'absent';
    let raw;
    try { raw = persistentStorage.getItem(SESSION_KEY); }
    catch { return 'unreadable'; }
    return raw === null || raw === undefined ? 'absent' : 'present';
  }

  // Returns whether the clear COMMITTED (batch 2e round 2/3,
  // R1): a fresh tombstone landed AND the session record is
  // confirmed absent after the removal attempt. Callers that
  // establish a session on top of a clear — fragment adoption
  // above all — must know the clear actually committed: a
  // record that survived (or could not be verified gone) still
  // speaks for the old account in every other tab, and an
  // UNCHANGED old tombstone cannot bind new credentials either
  // (the account pages bound to it would keep serving the old
  // account while this tab serves the new one). The tombstone
  // carries the clear's revision and a NEW lineage; the binding
  // below uses the revision captured synchronously at the
  // write, never a re-read. With no persistent storage at all
  // there is nothing to commit and nothing to diverge from:
  // memory-only, true.
  //
  // A FAILED clear retires the in-memory session (batch 2e
  // round 4, R1): credentials and their binding are one unit.
  // The round-3 code instead rebound the instance to whatever
  // epoch survived — when that survivor was ANOTHER account's
  // tombstone, the revision fence then ACCEPTED this tab's
  // retained credentials as current under the foreign lineage,
  // and a failed fragment adoption left the old account
  // legitimately served alongside the new one. Retained
  // credentials are never rebound to a surviving foreign epoch:
  // the session is dropped, generation and all, and the caller
  // sees only the failure.
  function clearPersistedSession() {
    if (!persistentStorage) return true;
    try { persistentStorage.removeItem(SESSION_KEY); } catch { /* the probe below decides */ }
    const rev = freshRevision();
    const sid = freshRevision();
    let landed = false;
    try {
      persistentStorage.setItem(EPOCH_KEY, JSON.stringify({
        rev, owner: instanceId, kind: 'clear', sid, at: Date.now(),
      }));
      landed = true;
    } catch { /* no fresh tombstone: the clear did not commit */ }
    if (!landed) {
      dropInMemorySession();
      return false;
    }
    boundRevision = rev;
    boundSid = sid;
    if (probeSessionRecord() !== 'absent') {
      dropInMemorySession();
      return false;
    }
    return true;
  }

  // Whether the shared session has rotated, replaced, or
  // cleared the session this instance's in-memory session
  // belongs to. The test is an EXACT match on BOTH halves of
  // this instance's binding — revision and lineage — against
  // the verified snapshot (batch 2e round 2, R1; batch 2g):
  // there is no same-owner exemption — an older tombstone
  // written by THIS instance (a clear that predates the
  // current session) must not stand in for a session record
  // that has since disappeared. My own writes never count as
  // supersession because each one moves the binding to its
  // own revision and lineage in the same synchronous step
  // (persistRefreshToken / clearPersistedSession). Sessions
  // without a refresh token — fragment handoffs — are covered
  // through their binding: their adoption wrote the tombstone,
  // and any later write moves past it. The lineage half is
  // what the revision-only test missed (batch 2g): a foreign
  // session record carrying a lineage but no revision of its
  // own borrowed the tombstone's revision and passed — the
  // pair cannot be borrowed, because the snapshot reports a
  // record's lineage only from that record itself. A state
  // that is present but UNVERIFIABLE is supersession outright
  // (batch 2g, R1): retained credentials are never served
  // under a state that cannot be read back. A record that
  // existed for this binding and is now missing counts as
  // supersession too (batch 2e, R1): disappearance is
  // invalidation, never silent currency — an unbound session
  // (an access-token-only adoption that never touched the
  // shared record) has no established record to lose.
  function sharedSessionSuperseded() {
    if (!persistentStorage) return false;
    if (!accessToken && !refreshToken) return false;
    const snap = sharedSnapshot();
    if (snap.state === 'unverifiable') return true;
    if (snap.state === 'absent') {
      return boundRevision !== null || boundSid !== null;
    }
    // The token itself is part of the comparison (batch 2g
    // round 2): two UNNAMED records share the (null, null)
    // binding, so binding alone cannot tell this instance's
    // legacy session from a foreign legacy record that
    // replaced it — the token value can, and this instance
    // holds it. For named records the clause never fires on
    // protocol writes (token and revision move together in
    // one commit); it fires exactly on foreign replacement.
    return snap.rev !== boundRevision || snap.sid !== boundSid
      || (refreshToken !== null && snap.refreshToken !== refreshToken);
  }

  function dropInMemorySession() {
    generation += 1;
    accessToken = null; refreshToken = null; expiresAt = 0;
    boundRevision = null;
    boundSid = null;
  }

  // Adoption is a commit (batch 2e, R1): when the response
  // carries a refresh token, the shared record is written
  // FIRST and the credentials are kept only when the write
  // landed. A failed commit aborts the adoption — this tab
  // must not hold account B in memory while the shared record
  // still says account A and another tab keeps dispatching A.
  // `lineage` is 'new' for a fresh session (code exchange) —
  // a new sid — and 'same' for a rotation of the session this
  // instance already holds, which keeps its sid.
  function adopt(data, { lineage = 'new' } = {}, fallbackLifetimeSeconds = 3600) {
    if (!data || typeof data.access_token !== 'string' || !data.access_token) return false;
    if (typeof data.refresh_token === 'string' && data.refresh_token) {
      const sid = lineage === 'same' && boundSid ? boundSid : freshRevision();
      // The commit is required only when a shared record
      // exists to disagree with: without persistent storage
      // the session is memory-only by design and there is no
      // other tab to diverge from.
      if (persistentStorage && !persistRefreshToken(data.refresh_token, sid)) {
        return false;
      }
      refreshToken = data.refresh_token;
    } else if (lineage === 'new') {
      // An access-only response to a FRESH authorization
      // (batch 2g round 2, R1): the access token belongs to
      // the new authorization, but without its refresh token
      // there is no new lineage to bind it to — pairing it
      // with the RETAINED session's refresh token and binding
      // would serve the new account's token under the old
      // account's lineage. Refuse the adoption outright; the
      // caller reports failure and the old session (if any)
      // stands untouched.
      return false;
    } else if (persistentStorage && refreshToken) {
      // Access-only ROTATION (lineage 'same'): the access
      // token was minted by the refresh token that was sent,
      // so the pairing is sound — but when the shared record
      // is still an UNNAMED legacy record holding exactly
      // that token, name it now (batch 2g round 2): persist
      // the same token in the atomic shape, giving the
      // session the revision and lineage every fence compares.
      // Leaving it unnamed would keep two different legacy
      // sessions indistinguishable to the binding checks. A
      // failed naming commit aborts the adoption like any
      // other failed commit.
      const snap = sharedSnapshot();
      if (snap.state === 'record' && snap.rev === null && snap.sid === null
        && snap.refreshToken === refreshToken) {
        if (!persistRefreshToken(refreshToken, freshRevision())) return false;
      }
    }
    accessToken = data.access_token;
    const lifetime = typeof data.expires_in === 'number'
      ? data.expires_in : fallbackLifetimeSeconds;
    expiresAt = Date.now() + lifetime * 1000;
    return true;
  }

  // 'ok' | 'rejected' | 'error' | 'superseded' — a rejection
  // means the server refused the grant (e.g. an expired/revoked
  // refresh token); an error means the exchange never completed
  // (network/parse); 'superseded' means the shared-state
  // fence discarded the response because another tab moved the
  // shared session while the request was in flight.
  async function tokenRequest(params, { lineage = 'new', expectedIdentity } = {}) {
    const requestedGeneration = generation;
    // The shared-state fence (batch 2d, HIGH 1; batch 2g
    // round 2): if any tab persisted or cleared the shared
    // session while this request was in flight, the response
    // belongs to a superseded session and is discarded WITHOUT
    // adopting — above all without re-persisting its refresh
    // token over the newer tab's session. The expected identity
    // is the one the SENT credentials are bound to, supplied by
    // the caller from its snapshot (batch 2e round 2, R1):
    // re-reading the current state here instead would let a
    // commit that landed between the caller's load and this
    // request pair the OLD token with the NEW session's
    // identity — both checks would pass against the new state
    // and the old session's rotation would overwrite the new
    // one. The comparison covers the whole snapshot identity
    // (state, revision, lineage, token), not the revision
    // alone: replacements that keep the revision — an unnamed
    // legacy record swapped for another, a foreign record
    // borrowing the tombstone's null revision — move the
    // identity and are discarded. The final check and adopt()'s
    // persist run in one synchronous block after the last
    // await, so a foreign commit lands either before the check
    // (detected) or after the persist (its write wins and this
    // instance's binding then mismatches — supersession, the
    // correct outcome).
    const requestedIdentity = expectedIdentity;
    // Without persistent storage the session is memory-only
    // by design: there is no shared state another tab could
    // move, so the fence trivially holds.
    const fenceHolds = () => !persistentStorage
      || sharedIdentity() === requestedIdentity;
    let response;
    try {
      response = await fetch(tokenUrl, {
        method: 'POST',
        headers: { 'content-type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams(params).toString(),
        credentials: 'omit', cache: 'no-store', redirect: 'error',
      });
    } catch { return 'error'; }
    if (requestedGeneration !== generation) return 'error';
    if (!fenceHolds()) return 'superseded';
    if (!response.ok) return 'rejected';
    let data = null;
    try { data = await response.json(); } catch { data = null; }
    if (requestedGeneration !== generation) return 'error';
    if (!fenceHolds()) return 'superseded';
    return adopt(data, { lineage }) ? 'ok' : 'error';
  }

  async function completeRedirect() {
    const params = new URLSearchParams(location.search);
    const code = params.get('code');
    const state = params.get('state');
    if (!code && !params.has('error')) return false;
    let pending = null;
    try { pending = JSON.parse(storage.getItem(PENDING_KEY) || 'null'); } catch { pending = null; }
    storage.removeItem(PENDING_KEY);
    history.replaceState(null, '', '/app');
    if (!code || !pending || pending.state !== state || typeof pending.verifier !== 'string') {
      return false;
    }
    const requestedGeneration = generation;
    const outcome = await tokenRequest({
      grant_type: 'authorization_code', client_id: CLIENT_ID, code,
      redirect_uri: redirectUri, code_verifier: pending.verifier,
    }, { expectedIdentity: sharedIdentity() });
    return outcome === 'ok' && requestedGeneration === generation;
  }

  function takeFragment() {
    const token = takeFragmentToken(location, history, storage, persistentStorage);
    if (!token) return false;
    // Fragment adoption is a committed transition (batch 2e
    // round 2, R1): the shared session is cleared FIRST, and
    // the handed-over credentials are installed only when the
    // clear actually landed. Installing them over a record
    // that survived a failed removal would serve the new
    // account here while every other surface — the account
    // pages bound to the surviving record — keeps serving the
    // old one. A failed clear also retires whatever session
    // this instance already held (round 4: credentials and
    // their binding are one unit; the old account is never
    // left served under a foreign tombstone's revision). With
    // no persistent storage at all the session is memory-only
    // by design and the clear trivially holds.
    if (!clearPersistedSession()) return false;
    generation += 1;
    accessToken = token;
    refreshToken = null;
    // The server remains the authority on this token's validity;
    // a 401 simply returns the app to its login screen.
    expiresAt = Number.MAX_SAFE_INTEGER;
    return true;
  }

  async function refresh() {
    if (!refreshToken) {
      // Loading binds token, revision and lineage from the
      // SAME record in one read (batch 2e, R1): the split read
      // — token from one record, revision from another, with
      // another tab's adoption in between — bound the old
      // token to the new session's revision and let it be
      // re-persisted over the newer session.
      const snapshot = loadSharedSession();
      if (snapshot) {
        refreshToken = snapshot.refreshToken;
        boundRevision = snapshot.rev;
        boundSid = snapshot.sid;
      }
    }
    if (!refreshToken) return null;
    const requestedGeneration = generation;
    let sentToken = refreshToken;
    let outcome = await tokenRequest({
      grant_type: 'refresh_token', client_id: CLIENT_ID, refresh_token: sentToken,
    }, { lineage: 'same', expectedIdentity: boundIdentity() });
    // The session may have been replaced while the request was in
    // flight (fragment handoff, sign-out): the outcome then belongs
    // to the previous account and must not mutate the new session —
    // in particular a stale 'rejected' must not clear it.
    if (requestedGeneration !== generation) return null;
    if (outcome === 'error' || outcome === 'superseded') {
      // A discard by the shared-state fence most often means
      // another tab rotated the shared session first: its newer
      // persisted token is the live one, so retry once with it
      // instead of reporting a spurious sign-out. A cleared
      // session (no persisted token) is not retried — and a
      // fenced discard with nothing newer to adopt means this
      // instance's session is gone, not merely unrefreshed.
      const snapshot = loadSharedSession();
      const persisted = snapshot ? snapshot.refreshToken : null;
      if (persisted && persisted !== sentToken) {
        // Switching to a DIFFERENT session's refresh token
        // retires the old session's access token with it
        // (round 3): credentials and their binding are one
        // unit. Retaining the old access token let a failed
        // retry fall through to serving the SUPERSEDED
        // account's token — the revision check passes because
        // the binding now matches the winner, while the token
        // still belongs to the loser. Token, revision and
        // lineage all come from the one snapshot (batch 2e).
        accessToken = null;
        expiresAt = 0;
        refreshToken = persisted;
        boundRevision = snapshot.rev;
        boundSid = snapshot.sid;
        sentToken = persisted;
        outcome = await tokenRequest({
          grant_type: 'refresh_token', client_id: CLIENT_ID,
          refresh_token: persisted,
        }, { lineage: 'same', expectedIdentity: boundIdentity() });
        if (requestedGeneration !== generation) return null;
      } else if (outcome === 'superseded') {
        dropInMemorySession();
        return null;
      }
    }
    if (outcome === 'ok') return accessToken;
    if (outcome === 'rejected') {
      // Clear the shared session only while it is still THIS
      // persisted session: another tab may already have replaced
      // it with its own, and a stale rejection must not wipe
      // that. An ABSENT persisted token is not ownership either
      // (Antigravity glass audit, finding 6): a fragment-only
      // session in another tab holds no SESSION_KEY and is
      // fenced solely by the shared revision, so advancing the
      // revision here would force that tab out.
      const stillOwnsShared = !sharedSessionSuperseded()
        && loadPersistedRefreshToken() === sentToken;
      accessToken = null; refreshToken = null; expiresAt = 0;
      boundRevision = null;
      boundSid = null;
      if (stillOwnsShared) {
        clearPersistedSession();
      }
    }
    // 'error': keep the persisted session; the next call retries.
    return null;
  }

  async function getToken() {
    // Cross-tab sync (batch 2d, HIGH 1): if another tab rotated,
    // replaced, or cleared the shared session, this tab's
    // in-memory credentials belong to a superseded session —
    // drop them and let the persisted state (if any) drive the
    // refresh below, instead of acting on the stale account.
    if (sharedSessionSuperseded()) dropInMemorySession();
    if (accessToken && Date.now() < expiresAt - REFRESH_MARGIN_MS) return accessToken;
    if (refreshToken || loadPersistedRefreshToken()) {
      if (!refreshPromise) {
        refreshPromise = refresh().finally(() => { refreshPromise = null; });
      }
      await refreshPromise;
      // The supersession check runs after EVERY awaited
      // refresh, on the success path too (round 4): a refresh
      // can pass both of its own fences and adopt, and another
      // tab can still replace the shared session in the
      // microtask gap before this caller resumes — returning
      // the captured result would serve the superseded account.
      // What is served is the CURRENT in-memory token, and only
      // while the session it belongs to is still the shared one.
      if (sharedSessionSuperseded()) {
        dropInMemorySession();
        return null;
      }
    }
    return accessToken && Date.now() < expiresAt ? accessToken : null;
  }

  // Dispatch an authenticated request under the CURRENT
  // session (round 5): the caller hands over a dispatch
  // callback that receives the token (or null) and must invoke
  // the request synchronously. A bearer string obtained from
  // getToken() crosses promise boundaries before its consumer
  // dispatches, and a session replacement scheduled through
  // any task/microtask boundary could land in that gap and be
  // answered with the superseded account's token. Here the
  // final supersession fence, the selection of the current
  // token, and the dispatch invocation happen in ONE
  // synchronous block after all awaited refresh work: a
  // replacement scheduled through any task or microtask
  // boundary lands either before the fence (honored — the
  // session is dropped and the dispatch receives null) or
  // after the request has been dispatched under the session
  // that was current at the fence. Scoped limitation, stated
  // plainly: a foreign commit overlapping the fence-to-fetch
  // interval itself — a write completing concurrently from
  // another tab's thread — can still permit dispatch with
  // the superseded token; excluding that needs a cross-tab
  // lock protocol over every commit path, which this design
  // does not adopt (see docs/implementation-notes.md,
  // batch 2d).
  async function withCurrentToken(dispatch) {
    await getToken();
    if (sharedSessionSuperseded()) {
      dropInMemorySession();
      return dispatch(null);
    }
    return dispatch(accessToken && Date.now() < expiresAt ? accessToken : null);
  }

  function hasPersistedSession() {
    return Boolean(refreshToken || loadPersistedRefreshToken());
  }

  function clearSession() {
    generation += 1;
    accessToken = null; refreshToken = null; expiresAt = 0;
    boundSid = null;
    clearPersistedSession();
  }

  async function beginLogin() {
    const verifier = base64url(crypto.getRandomValues(new Uint8Array(32)));
    const state = base64url(crypto.getRandomValues(new Uint8Array(16)));
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
    const challenge = base64url(new Uint8Array(digest));
    storage.setItem(PENDING_KEY, JSON.stringify({ verifier, state }));
    const params = new URLSearchParams({
      client_id: CLIENT_ID, response_type: 'code', scope: 'aiqs:use',
      redirect_uri: redirectUri, state, code_challenge: challenge,
      code_challenge_method: 'S256', resource,
    });
    location.assign(authorizeUrl + '?' + params.toString());
  }

  return Object.freeze({
    completeRedirect, takeFragment, getToken, withCurrentToken, beginLogin,
    hasPersistedSession, clearSession,
  });
}
