// Browser client for the authenticated account API (/api/account/*).
// The access token comes from an injected provider and is sent only as
// a Bearer header to this same origin; nothing is persisted here.
// Results mirror the server contract: { kind: 'login' } when there is
// no usable token (none supplied, or the server answered 401),
// { kind: 'error' } when the call itself failed, and
// { kind: 'ok', data } with the server's operation-result dictionary.
//
// Calls marked requireSuccess treat any non-2xx answer as
// { kind: 'error' } — even one carrying a JSON body. The admission
// middleware answers overload with 503/429 JSON ({"error":
// "server_busy"} / {"error": "rate_limited"}); the /api/account/*
// endpoints themselves always answer operation results with 200,
// so for those calls a non-2xx JSON body is a failure, not data.
// Without this, a busy answer to linkStatus()/purchase() was
// rendered as the feature's own state ("準備中" / "未設定") — the
// exact misreading the app client fixed for its target list. The
// referral calls keep the legacy contract on purpose: their 404
// (feature off) and their status-coded error bodies (400/403/409
// with a fixed referral error code) ARE data the page maps to
// specific messages.
export function createAccountClient({ fetch, getToken, withToken }) {
  if (typeof fetch !== 'function' || typeof getToken !== 'function') {
    throw new Error('Account client unavailable');
  }
  // withCurrentToken routing, as in the app client (batch 2d,
  // HIGH 1 round 5): fence, selection and dispatch in one
  // synchronous block when the auth layer offers it.
  async function call(path, options = {}) {
    if (typeof withToken === 'function') {
      return withToken((token) => dispatch(token, path, options));
    }
    const token = await getToken();
    return dispatch(token, path, options);
  }
  async function dispatch(token, path, options = {}) {
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
    if (options.requireSuccess && (response.status < 200 || response.status >= 300)) {
      return { kind: 'error', status: response.status };
    }
    return { kind: 'ok', status: response.status, data };
  }
  return Object.freeze({
    linkStatus: () => call('/api/account/link', { requireSuccess: true }),
    verifyLink: (code) => call('/api/account/link/verify', { method: 'POST', body: { code }, requireSuccess: true }),
    confirmLink: (reservationId) => call('/api/account/link/confirm', { method: 'POST', body: { reservation_id: reservationId }, requireSuccess: true }),
    unlink: () => call('/api/account/link/unlink', { method: 'POST', body: {}, requireSuccess: true }),
    purchase: () => call('/api/account/purchase', { requireSuccess: true }),
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
