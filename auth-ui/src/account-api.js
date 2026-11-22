// Browser client for the authenticated account API (/api/account/*).
// The access token comes from an injected provider and is sent only as
// a Bearer header to this same origin; nothing is persisted here.
// Results mirror the server contract: { kind: 'login' } when there is
// no usable token (none supplied, or the server answered 401),
// { kind: 'error' } when the call itself failed, and
// { kind: 'ok', data } with the server's operation-result dictionary.
export function createAccountClient({ fetch, getToken }) {
  if (typeof fetch !== 'function' || typeof getToken !== 'function') {
    throw new Error('Account client unavailable');
  }
  async function call(path, options = {}) {
    const token = await getToken();
    if (!token) return { kind: 'login' };
    let response;
    try {
      response = await fetch(path, {
        method: options.method || 'GET',
        headers: {
          authorization: 'Bearer ' + token,
          ...(options.body ? { 'content-type': 'application/json' } : {}),
        },
        body: options.body ? JSON.stringify(options.body) : undefined,
      });
    } catch {
      return { kind: 'error' };
    }
    if (response.status === 401) return { kind: 'login' };
    let data = null;
    try { data = await response.json(); } catch { data = null; }
    if (!data || typeof data !== 'object') return { kind: 'error' };
    return { kind: 'ok', status: response.status, data };
  }
  return Object.freeze({
    linkStatus: () => call('/api/account/link'),
    verifyLink: (code) => call('/api/account/link/verify', { method: 'POST', body: { code } }),
    confirmLink: (reservationId) => call('/api/account/link/confirm', { method: 'POST', body: { reservation_id: reservationId } }),
    unlink: () => call('/api/account/link/unlink', { method: 'POST', body: {} }),
    purchase: () => call('/api/account/purchase'),
    // Referral lives on the app API surface (/api/app/referral) and
    // is reused here with the account token; a 404 answer (feature
    // off) comes back as { kind: 'ok', status: 404 } so the page can
    // hide its referral section.
    referral: () => call('/api/app/referral'),
    redeemReferral: (code) => call('/api/app/referral/redeem',
      { method: 'POST', body: { code } }),
    markReferralGiftsSeen: (ids) => call('/api/app/referral/gifts/seen',
      { method: 'POST', body: { ids } }),
  });
}
