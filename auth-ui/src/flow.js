// Only provider-returned destinations are accepted; never use a URL query redirect.
export function safeRedirect(value) {
  const url = new URL(value);
  if (url.username || url.password || !(url.protocol === 'https:' ||
      (url.protocol === 'http:' && ['127.0.0.1', '[::1]', 'localhost'].includes(url.hostname)))) {
    throw new Error('Unsafe redirect');
  }
  return url.href;
}

export function authorizationId(search) {
  const values = new URLSearchParams(search).getAll('authorization_id');
  if (!values.length) return null;
  if (values.length !== 1 || !/^[a-zA-Z0-9_-]{1,128}$/.test(values[0])) throw new Error('Invalid authorization');
  return values[0];
}

export function callbackUrl(origin, id) {
  const url = new URL('/oauth/consent', origin);
  if (id) url.searchParams.set('authorization_id', id);
  return url.href;
}

export function createFlow(auth, id) {
  let ready = false;
  let pending = false;
  let complete = false;
  return {
    async load() {
      ready = false;
      const { data, error } = await auth.getUser();
      if (error || !data?.user) return { kind: 'login' };
      if (!id) return { kind: 'connected' };
      const result = await auth.oauth.getAuthorizationDetails(id);
      if (result.error || !result.data) throw new Error('Authorization unavailable');
      const details = result.data;
      if (details.redirect_url) return { kind: 'redirect', url: safeRedirect(details.redirect_url) };
      if (details.authorization_id !== id || !details.client?.name ||
          details.user?.id !== data.user.id || typeof details.scope !== 'string') {
        throw new Error('Invalid authorization details');
      }
      const destination = new URL(safeRedirect(details.redirect_uri)).origin;
      ready = true;
      return { kind: 'consent', name: details.client.name,
        scopes: details.scope.split(/\s+/).filter(Boolean), destination };
    },
    async decide(allow) {
      if (!ready || pending || complete || typeof allow !== 'boolean') throw new Error('No pending consent');
      pending = true;
      try {
        const result = await auth.oauth[allow ? 'approveAuthorization' : 'denyAuthorization'](
          id, { skipBrowserRedirect: true });
        if (result.error || !result.data?.redirect_url) throw new Error('Consent failed');
        const url = safeRedirect(result.data.redirect_url);
        complete = true;
        return url;
      } finally { pending = false; }
    }
  };
}
