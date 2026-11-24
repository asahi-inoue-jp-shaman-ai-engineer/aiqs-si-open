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
// Cross-tab discipline (batch 2d, HIGH 1): the generation fence
// above is per-instance, but the persisted session is shared by
// every tab — so a refresh in flight in tab 1 for account A could
// still land (and re-persist A's rotated token) after tab 2 had
// adopted account B, and the next load resurrected A. A shared
// revision record in the same storage now fences the commit:
// every persist or clear of the shared session writes a FRESH
// RANDOM revision (round 2: a numeric epoch dies at 2^53, where
// +1 is a no-op and the fence silently stops moving — a string
// revision is only ever compared for equality), a token request
// captures the revision when it starts and discards its response
// if the revision moved, and a tab whose in-memory session is
// bound to an older revision drops it instead of acting on it —
// fragment-only sessions included: they hold no refresh token,
// but they are bound to the revision their adoption wrote, and
// another tab's later write supersedes them all the same. A
// stale tab also never clears a shared session that is no
// longer its own.
import { ensureFragmentHandoff, takeFragmentToken } from './account-session.js';

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

  function readSharedRecord() {
    if (!persistentStorage) return null;
    try {
      const saved = JSON.parse(persistentStorage.getItem(EPOCH_KEY) || 'null');
      if (saved && typeof saved === 'object') {
        if (typeof saved.rev === 'string' && saved.rev) {
          return { rev: saved.rev, owner: saved.owner };
        }
        // Legacy numeric-epoch records (round 1) still fence by
        // equality: their value becomes the revision string.
        if (typeof saved.epoch === 'number') {
          return { rev: `legacy:${saved.epoch}`, owner: saved.owner };
        }
      }
    } catch { /* corrupted entry: treated as no record */ }
    return null;
  }

  function sharedRevision() {
    const record = readSharedRecord();
    return record ? record.rev : null;
  }

  function advanceSharedRevision(kind) {
    if (!persistentStorage) return null;
    const rev = base64url(crypto.getRandomValues(new Uint8Array(16)));
    try {
      persistentStorage.setItem(EPOCH_KEY, JSON.stringify({
        rev, owner: instanceId, kind, at: Date.now(),
      }));
      boundRevision = rev;
      return rev;
    } catch { /* storage full/blocked: the fence degrades, never throws */ }
    return null;
  }

  function persistRefreshToken() {
    if (!persistentStorage || !refreshToken) return;
    try {
      persistentStorage.setItem(SESSION_KEY, JSON.stringify({
        refreshToken, savedAt: Date.now(),
      }));
      advanceSharedRevision('persist');
    } catch { /* storage full/blocked: session stays memory-only */ }
  }

  function loadPersistedRefreshToken() {
    if (!persistentStorage) return null;
    try {
      const saved = JSON.parse(persistentStorage.getItem(SESSION_KEY) || 'null');
      if (saved && typeof saved.refreshToken === 'string' && saved.refreshToken) {
        return saved.refreshToken;
      }
    } catch { /* corrupted entry: treated as no session */ }
    return null;
  }

  function clearPersistedSession() {
    if (!persistentStorage) return;
    try {
      persistentStorage.removeItem(SESSION_KEY);
      advanceSharedRevision('clear');
    } catch { /* ignore */ }
  }

  // Whether another tab has rotated, replaced, or cleared the
  // shared session this instance's in-memory session belongs
  // to. My own writes never count (the record carries my id).
  // Sessions without a refresh token — fragment handoffs — are
  // covered through their bound revision: their adoption wrote
  // the record, and any later foreign write moves past it.
  function sharedSessionSuperseded() {
    if (!persistentStorage) return false;
    if (!accessToken && !refreshToken) return false;
    const record = readSharedRecord();
    if (!record) return false;
    if (record.owner === instanceId) return false;
    return record.rev !== boundRevision;
  }

  function dropInMemorySession() {
    generation += 1;
    accessToken = null; refreshToken = null; expiresAt = 0;
    boundRevision = null;
  }

  function adopt(data, fallbackLifetimeSeconds = 3600) {
    if (!data || typeof data.access_token !== 'string' || !data.access_token) return false;
    accessToken = data.access_token;
    if (typeof data.refresh_token === 'string' && data.refresh_token) {
      refreshToken = data.refresh_token;
      persistRefreshToken();
    }
    const lifetime = typeof data.expires_in === 'number'
      ? data.expires_in : fallbackLifetimeSeconds;
    expiresAt = Date.now() + lifetime * 1000;
    return true;
  }

  // 'ok' | 'rejected' | 'error' | 'superseded' — a rejection
  // means the server refused the grant (e.g. an expired/revoked
  // refresh token); an error means the exchange never completed
  // (network/parse); 'superseded' means the shared-revision
  // fence discarded the response because another tab moved the
  // shared session while the request was in flight.
  async function tokenRequest(params) {
    const requestedGeneration = generation;
    // The shared-revision fence (batch 2d, HIGH 1): if any tab
    // persisted or cleared the shared session while this request
    // was in flight, the response belongs to a superseded
    // session and is discarded WITHOUT adopting — above all
    // without re-persisting its refresh token over the newer
    // tab's session.
    const requestedRevision = sharedRevision();
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
    if (sharedRevision() !== requestedRevision) return 'superseded';
    if (!response.ok) return 'rejected';
    let data = null;
    try { data = await response.json(); } catch { data = null; }
    if (requestedGeneration !== generation) return 'error';
    if (sharedRevision() !== requestedRevision) return 'superseded';
    return adopt(data) ? 'ok' : 'error';
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
    });
    return outcome === 'ok' && requestedGeneration === generation;
  }

  function takeFragment() {
    const token = takeFragmentToken(location, history, storage);
    if (token) {
      generation += 1;
      accessToken = token;
      refreshToken = null;
      clearPersistedSession();
      // The server remains the authority on this token's validity;
      // a 401 simply returns the app to its login screen.
      expiresAt = Number.MAX_SAFE_INTEGER;
    }
    return Boolean(token);
  }

  async function refresh() {
    if (!refreshToken) {
      refreshToken = loadPersistedRefreshToken();
      // Loading the shared session binds this instance to the
      // revision current at the load: another tab's later write
      // is what supersedes it, not the load itself.
      if (refreshToken) boundRevision = sharedRevision();
    }
    if (!refreshToken) return null;
    const requestedGeneration = generation;
    let sentToken = refreshToken;
    let outcome = await tokenRequest({
      grant_type: 'refresh_token', client_id: CLIENT_ID, refresh_token: sentToken,
    });
    // The session may have been replaced while the request was in
    // flight (fragment handoff, sign-out): the outcome then belongs
    // to the previous account and must not mutate the new session —
    // in particular a stale 'rejected' must not clear it.
    if (requestedGeneration !== generation) return null;
    if (outcome === 'error' || outcome === 'superseded') {
      // A discard by the shared-revision fence most often means
      // another tab rotated the shared session first: its newer
      // persisted token is the live one, so retry once with it
      // instead of reporting a spurious sign-out. A cleared
      // session (no persisted token) is not retried — and a
      // fenced discard with nothing newer to adopt means this
      // instance's session is gone, not merely unrefreshed.
      const persisted = loadPersistedRefreshToken();
      if (persisted && persisted !== sentToken) {
        // Switching to a DIFFERENT session's refresh token
        // retires the old session's access token with it
        // (round 3): credentials and their binding are one
        // unit. Retaining the old access token let a failed
        // retry fall through to serving the SUPERSEDED
        // account's token — the revision check passes because
        // the binding now matches the winner, while the token
        // still belongs to the loser.
        accessToken = null;
        expiresAt = 0;
        refreshToken = persisted;
        boundRevision = sharedRevision();
        sentToken = persisted;
        outcome = await tokenRequest({
          grant_type: 'refresh_token', client_id: CLIENT_ID,
          refresh_token: persisted,
        });
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
