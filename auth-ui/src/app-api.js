// Browser client for the standalone app API (/api/app/*).
// The access token comes from an injected provider and is sent only
// as a Bearer header to this same origin; nothing is persisted here.
// Results mirror the account client contract: { kind: 'login' } when
// there is no usable token (none supplied, or the server answered
// 401), { kind: 'error' } when the call itself failed, and
// { kind: 'ok', status, data } with the server's response dictionary
// — including its fixed "error" code on 400s, which the app maps to
// its template screens.
export function createAppClient({ fetch, getToken }) {
  if (typeof fetch !== 'function' || typeof getToken !== 'function') {
    throw new Error('App client unavailable');
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
    session: () => call('/api/app/session'),
    targets: () => call('/api/app/targets'),
    registerTarget: (body) => call('/api/app/targets', { method: 'POST', body }),
    renameTarget: (targetId, name) => call('/api/app/targets/rename',
      { method: 'POST', body: { target_id: targetId, name } }),
    deleteTarget: (targetId) => call('/api/app/targets/delete',
      { method: 'POST', body: { target_id: targetId } }),
    scan: (body) => call('/api/app/jobs/scan', { method: 'POST', body }),
    prepareIrradiation: (scanId, count) => call('/api/app/jobs/irradiate/prepare',
      { method: 'POST', body: { scan_id: scanId, count } }),
    irradiate: (confirmationId, requestKey) => call('/api/app/jobs/irradiate',
      { method: 'POST', body: { confirmation_id: confirmationId, request_key: requestKey } }),
    prepareReading: (scanId) => call(
      '/api/app/jobs/' + encodeURIComponent(scanId) + '/reading/prepare',
      { method: 'POST', body: {} }),
    createReading: (confirmationId, requestKey) => call('/api/app/readings',
      { method: 'POST', body: { confirmation_id: confirmationId, request_key: requestKey } }),
    reading: (readingId) => call('/api/app/readings/' + encodeURIComponent(readingId)),
    job: (jobId) => call('/api/app/jobs/' + encodeURIComponent(jobId)),
    cancelJob: (jobId) => call('/api/app/jobs/' + encodeURIComponent(jobId) + '/cancel',
      { method: 'POST', body: {} }),
    history: () => call('/api/app/history'),
    referral: () => call('/api/app/referral'),
    redeemReferral: (code) => call('/api/app/referral/redeem',
      { method: 'POST', body: { code } }),
    markReferralGiftsSeen: (ids) => call('/api/app/referral/gifts/seen',
      { method: 'POST', body: { ids } }),
  });
}
