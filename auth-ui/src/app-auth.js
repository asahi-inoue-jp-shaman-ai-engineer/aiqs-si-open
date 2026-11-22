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
// connection flow (#access_token=..., the account pages' mechanism)
// is adopted the same memory-only way and never persisted.
import { takeFragmentToken } from './account-session.js';

const CLIENT_ID = 'si-app';
const PENDING_KEY = 'si-app-pkce-v1';
const SESSION_KEY = 'si-app-session-v1';
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
  const issuer = location.origin + '/realms/si';
  const authorizeUrl = issuer + '/protocol/openid-connect/auth';
  const tokenUrl = issuer + '/protocol/openid-connect/token';
  const redirectUri = location.origin + '/app';
  const resource = location.origin + '/mcp';

  let accessToken = null;
  let refreshToken = null;
  let expiresAt = 0;
  let refreshPromise = null;

  function persistRefreshToken() {
    if (!persistentStorage || !refreshToken) return;
    try {
      persistentStorage.setItem(SESSION_KEY, JSON.stringify({
        refreshToken, savedAt: Date.now(),
      }));
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
    try { persistentStorage.removeItem(SESSION_KEY); } catch { /* ignore */ }
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

  // 'ok' | 'rejected' | 'error' — a rejection means the server
  // refused the grant (e.g. an expired/revoked refresh token);
  // an error means the exchange never completed (network/parse).
  async function tokenRequest(params) {
    let response;
    try {
      response = await fetch(tokenUrl, {
        method: 'POST',
        headers: { 'content-type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams(params).toString(),
        credentials: 'omit', cache: 'no-store', redirect: 'error',
      });
    } catch { return 'error'; }
    if (!response.ok) return 'rejected';
    let data = null;
    try { data = await response.json(); } catch { data = null; }
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
    return (await tokenRequest({
      grant_type: 'authorization_code', client_id: CLIENT_ID, code,
      redirect_uri: redirectUri, code_verifier: pending.verifier,
    })) === 'ok';
  }

  function takeFragment() {
    const token = takeFragmentToken(location, history);
    if (token) {
      accessToken = token;
      refreshToken = null;
      // The server remains the authority on this token's validity;
      // a 401 simply returns the app to its login screen.
      expiresAt = Number.MAX_SAFE_INTEGER;
    }
    return Boolean(token);
  }

  async function refresh() {
    if (!refreshToken) refreshToken = loadPersistedRefreshToken();
    if (!refreshToken) return null;
    const outcome = await tokenRequest({
      grant_type: 'refresh_token', client_id: CLIENT_ID, refresh_token: refreshToken,
    });
    if (outcome === 'ok') return accessToken;
    if (outcome === 'rejected') {
      accessToken = null; refreshToken = null; expiresAt = 0;
      clearPersistedSession();
    }
    // 'error': keep the persisted session; the next call retries.
    return null;
  }

  async function getToken() {
    if (accessToken && Date.now() < expiresAt - REFRESH_MARGIN_MS) return accessToken;
    if (refreshToken || loadPersistedRefreshToken()) {
      if (!refreshPromise) {
        refreshPromise = refresh().finally(() => { refreshPromise = null; });
      }
      const refreshed = await refreshPromise;
      if (refreshed) return refreshed;
    }
    return accessToken && Date.now() < expiresAt ? accessToken : null;
  }

  function hasPersistedSession() {
    return Boolean(refreshToken || loadPersistedRefreshToken());
  }

  function clearSession() {
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
    completeRedirect, takeFragment, getToken, beginLogin,
    hasPersistedSession, clearSession,
  });
}
