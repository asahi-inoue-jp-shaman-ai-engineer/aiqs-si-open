// State-bound access-token handoff for the account pages.
//
// A token may arrive in the URL fragment (#access_token=...&state=...)
// when the user's own connection flow sends the browser to an account
// page. A bare fragment token is NEVER adopted (Astra final round
// #1): adoption requires the fragment's state to match a pending
// handoff nonce that this browser armed earlier in the same tab
// session. The nonce is a random value kept only in sessionStorage;
// a crafted link cannot know it, so an unsolicited token — for
// example an attacker's, which would silently turn the victim's
// browser into the attacker's account — is ignored. The nonce is
// single-use: a successful adoption consumes it, so the same link
// cannot be replayed.
//
// The token itself is read exactly once and kept only in this
// closure — never in web storage, never in a cookie. Any fragment
// carrying an access_token is stripped from the URL immediately,
// adopted or not: bearer tokens do not sit in the address bar.
const HANDOFF_KEY = 'si-fragment-handoff-v1';

function defaultStorage() {
  try { return globalThis.sessionStorage ?? null; } catch { return null; }
}

function randomNonce() {
  const crypto = globalThis.crypto;
  if (!crypto?.getRandomValues) return null;
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  let hex = '';
  for (const byte of bytes) hex += byte.toString(16).padStart(2, '0');
  return hex;
}

// Arm a fresh handoff nonce and return it. The connection flow is
// expected to echo it back as the fragment's state parameter, the
// standard OAuth state discipline. Arming is safe at page load: the
// nonce is generated in this browser and never leaves it except as
// the echoed state of a flow this browser started toward.
export function armFragmentHandoff(storage) {
  const store = storage ?? defaultStorage();
  if (!store) return null;
  const nonce = randomNonce();
  if (!nonce) return null;
  try { store.setItem(HANDOFF_KEY, nonce); } catch { return null; }
  return nonce;
}

// The pending nonce, arming one if this tab session has none yet.
export function ensureFragmentHandoff(storage) {
  const store = storage ?? defaultStorage();
  if (!store) return null;
  let existing = null;
  try { existing = store.getItem(HANDOFF_KEY); } catch { return null; }
  if (typeof existing === 'string' && existing) return existing;
  return armFragmentHandoff(store);
}

export function takeFragmentToken(location, history, storage) {
  const hash = typeof location?.hash === 'string' ? location.hash : '';
  if (!hash.startsWith('#')) return null;
  const params = new URLSearchParams(hash.slice(1));
  const values = params.getAll('access_token');
  if (values.length === 0) return null;
  history.replaceState(null, '', location.pathname + location.search);
  if (values.length !== 1 || !values[0] || values[0].length > 8192) return null;
  // The no-crypto guarantee is enforced at adoption too, not only
  // when arming: a context that cannot produce randomness must
  // never adopt a handed-over token.
  if (!randomNonce()) return null;
  const store = storage ?? defaultStorage();
  if (!store) return null;
  let pending = null;
  try { pending = store.getItem(HANDOFF_KEY); } catch { return null; }
  const states = params.getAll('state');
  if (!pending || states.length !== 1 || states[0] !== pending) return null;
  // Single-use is a hard condition, not best effort: if the nonce
  // cannot be consumed, the token is not adopted (it would
  // otherwise stay replayable).
  try { store.removeItem(HANDOFF_KEY); } catch { return null; }
  return values[0];
}

export function createTokenProvider(location, history, storage) {
  ensureFragmentHandoff(storage);
  const token = takeFragmentToken(location, history, storage);
  return async () => token;
}

// Carrying the session across the account pages' own navigation.
// The token lives only in this closure, so a plain link from one
// account page to another (a full document load) used to drop it
// and the destination showed its login guidance despite the user
// being signed in. The carry stays inside the state-bound handoff
// design above: account-page links get the token in their URL
// fragment TOGETHER WITH the pending handoff nonce as state, and
// the destination adopts it only when that state matches its own
// armed nonce (the same tab session, so it does). A link is NEVER
// rewritten with a bare token: without a state to bind, the href
// is left exactly as it was. A fragment is never sent to the
// server, and the destination strips it from the URL on arrival.
// Only /account paths are rewritten: those are the pages that
// consume a fragment token, and nowhere else gets the token in
// a URL.
export function hrefWithFragmentToken(href, token, state) {
  if (!token || !state || typeof href !== 'string') return href;
  if (!href.startsWith('/account')) return href;
  const base = href.split('#')[0];
  return `${base}#access_token=${encodeURIComponent(token)}&state=${encodeURIComponent(state)}`;
}

// Keep carried links usable across document restoration (delta
// review R4). The initial carry rewrites anchors once, at page
// initialization — but when the browser restores THIS document
// from its back/forward cache, initialization does not run
// again, and by then the nonce baked into the anchors has been
// consumed by the destination page: following the link again
// would arrive with a dead state and show login guidance. Two
// refresh points fix that without weakening single-use:
//   - a pageshow with persisted=true (the restoration signal)
//     re-runs the carry, rewriting every anchor with the
//     CURRENT pending nonce (a fresh one is armed if the old
//     was consumed — the destination arms/matches in the same
//     tab session, so the new state binds exactly like the
//     first carry did);
//   - a capture-phase click on an account anchor rewrites THAT
//     anchor just before it is followed, using the token this
//     installer already resolved and the current pending nonce.
// A consumed nonce is never replayed: every refresh reads the
// pending nonce fresh from storage via carryTokenToAccountLinks
// / ensureFragmentHandoff.
export function installCarriedLinkRefresh({ target, document, tokenProvider, storage } = {}) {
  if (!document?.querySelectorAll || typeof tokenProvider !== 'function') return null;
  let cachedToken = null;
  const refresh = async () => {
    const token = await tokenProvider();
    if (!token) return 0;
    cachedToken = token;
    return carryTokenToAccountLinks(document, token, storage);
  };
  if (typeof target?.addEventListener === 'function') {
    target.addEventListener('pageshow', (event) => {
      if (event?.persisted) void refresh();
    });
  }
  if (typeof document.addEventListener === 'function') {
    document.addEventListener('click', (event) => {
      if (!cachedToken) return;
      const anchor = typeof event?.target?.closest === 'function'
        ? event.target.closest('a[href^="/account"]') : null;
      if (!anchor) return;
      const state = ensureFragmentHandoff(storage);
      if (!state) return;
      const current = anchor.getAttribute('href');
      const next = hrefWithFragmentToken(current, cachedToken, state);
      if (next !== current) anchor.setAttribute('href', next);
    }, true);
  }
  return refresh;
}

export function carryTokenToAccountLinks(document, token, storage) {
  if (!token || !document?.querySelectorAll) return 0;
  const state = ensureFragmentHandoff(storage);
  if (!state) return 0;
  let rewritten = 0;
  for (const anchor of document.querySelectorAll('a[href^="/account"]')) {
    const current = anchor.getAttribute('href');
    const next = hrefWithFragmentToken(current, token, state);
    if (next !== current) {
      anchor.setAttribute('href', next);
      rewritten += 1;
    }
  }
  return rewritten;
}
