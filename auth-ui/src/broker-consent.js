// Upstream-only consent adapter. Host owns login/session; Keycloak owns state/code.
// Not imported by the public SI landing page. Never pass a broker secret here.
export function createBrokerConsent(auth, id, { clientId, callback }) {
  if (!/^[a-zA-Z0-9_-]{1,128}$/.test(id || '') ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(clientId || '')) {
    throw new Error('Invalid broker configuration');
  }
  const target = new URL(callback);
  if (target.protocol !== 'https:' || target.username || target.password || target.search || target.hash) {
    throw new Error('Invalid broker callback');
  }
  let userId = null, pending = false, complete = false;
  function destination(value) {
    const url = new URL(value);
    if (url.origin !== target.origin || url.pathname !== target.pathname ||
        url.username || url.password || url.hash) throw new Error('Unexpected broker callback');
    const params = url.searchParams;
    for (const name of params.keys()) {
      if (!['code', 'state', 'error', 'error_description', 'error_uri', 'iss'].includes(name) ||
          params.getAll(name).length !== 1) throw new Error('Invalid callback parameters');
    }
    if (!params.get('state') || params.has('code') === params.has('error') ||
        !(params.get('code') || params.get('error'))) throw new Error('Invalid callback result');
    return url.href;
  }
  return {
    async load() {
      if (pending || complete) throw new Error('Consent unavailable');
      pending = true; userId = null;
      try {
        const current = await auth.getUser();
        if (current.error || !current.data?.user?.id) return { kind: 'login' };
        const response = await auth.oauth.getAuthorizationDetails(id);
        const details = response.data;
        if (response.error || !details) throw new Error('Authorization unavailable');
        if (details.redirect_url) {
          const url = destination(details.redirect_url);
          complete = true;
          return { kind: 'redirect', url };
        }
        const scopes = typeof details.scope === 'string' ? details.scope.split(/\s+/).filter(Boolean) : [];
        if (details.authorization_id !== id || details.client?.id !== clientId ||
            details.user?.id !== current.data.user.id || details.redirect_uri !== target.href ||
            !scopes.includes('openid') || scopes.some(s => !['openid', 'email', 'profile'].includes(s))) {
          throw new Error('Unexpected upstream authorization');
        }
        userId = current.data.user.id;
        return { kind: 'consent', scopes };
      } finally { pending = false; }
    },
    async decide(allow) {
      if (!userId || pending || complete || typeof allow !== 'boolean') throw new Error('No pending consent');
      pending = true;
      const expected = userId;
      userId = null; // A failed/ambiguous decision requires a fresh details load.
      try {
        const current = await auth.getUser();
        if (current.error || current.data?.user?.id !== expected) throw new Error('Account changed');
        const response = await auth.oauth[allow ? 'approveAuthorization' : 'denyAuthorization'](id, { skipBrowserRedirect: true });
        if (response.error || !response.data?.redirect_url) throw new Error('Consent failed');
        const url = destination(response.data.redirect_url);
        complete = true;
        return url;
      } finally { pending = false; }
    }
  };
}

export async function mountBrokerConsent(element, flow, { onLoginRequired, onRedirect }) {
  element.replaceChildren();
  const message = document.createElement('p');
  message.textContent = '本人確認を読み込んでいます。';
  message.setAttribute('role', 'status');
  element.append(message);
  try {
    const state = await flow.load();
    if (state.kind === 'login') { message.textContent = 'ログインしてから接続を続けてください。'; return onLoginRequired(); }
    if (state.kind === 'redirect') return onRedirect(state.url);
    message.textContent = 'アイクスSIへの本人確認を許可しますか？';
    const scopes = document.createElement('p');
    const labels = { openid: '本人の識別', email: 'メールアドレス', profile: 'プロフィール' };
    scopes.textContent = '共有する情報：' + state.scopes.map(s => labels[s]).join('、');
    element.append(scopes);
    const buttons = [];
    for (const [label, allow] of [['許可する', true], ['許可しない', false]]) {
      const button = document.createElement('button');
      button.type = 'button'; button.textContent = label; buttons.push(button);
      button.addEventListener('click', async () => {
        buttons.forEach(b => { b.disabled = true; });
        try { onRedirect(await flow.decide(allow)); }
        catch { message.textContent = '接続を完了できませんでした。AIから接続をやり直してください。'; }
      });
      element.append(button);
    }
  } catch { message.textContent = '接続を確認できませんでした。AIから接続をやり直してください。'; }
}
