// Memory-only access-token handoff for the account pages.
// A token may arrive in the URL fragment (#access_token=...) when the
// user's own connection flow sends the browser to an account page. It
// is read exactly once, removed from the URL immediately, and kept
// only in this closure — never in web storage, never in a cookie.
// Without a token the provider yields null and the account pages show
// their login guidance instead of calling the API.
export function takeFragmentToken(location, history) {
  const hash = typeof location?.hash === 'string' ? location.hash : '';
  if (!hash.startsWith('#')) return null;
  const values = new URLSearchParams(hash.slice(1)).getAll('access_token');
  if (values.length !== 1 || !values[0] || values[0].length > 8192) return null;
  history.replaceState(null, '', location.pathname + location.search);
  return values[0];
}

export function createTokenProvider(location, history) {
  const token = takeFragmentToken(location, history);
  return async () => token;
}
